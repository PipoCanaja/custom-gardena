"""Pairing through the Bluetooth proxy for the classic Water Computer.

Experimental. Observed on a classic Water Computer (model 0, variant 1): over an
unencrypted link the device only exposes the generic services (1800, 1801,
180a) and the TI OAD service (f000ffc0); the Gardena services (98bd...) are not
listed. The official app bonds the device. This connection asks the proxy to
pair right after the link is established, before services are discovered, and
rediscovers services without using the cache.

Only devices whose last advertisement identifies a classic Water Computer use
this path. Every other device, and a proxy that cannot pair, goes through the
unchanged CachedConnection._connect.
"""

from __future__ import annotations

from collections.abc import Callable
import logging

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from gardena_bluetooth.client import CachedConnection
from gardena_bluetooth.parse import (
    ManufacturerData,
    ProductGroup,
    ProductModelWaterControl,
)

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

LOGGER = logging.getLogger(__name__)

GARDENA_SERVICE_PREFIX = "98bd"
CLASSIC_MODELS = (ProductModelWaterControl.CLASSIC1, ProductModelWaterControl.CLASSIC2)

# One retry lets Home Assistant try another proxy after a failure. Kept low on
# purpose: repeated connection attempts in a short time have been seen to leave
# the device refusing every connection until its batteries were removed.
CONNECT_ATTEMPTS = 2


def is_classic_water_computer(hass: HomeAssistant, address: str) -> bool:
    """Return True if the last advertisement identifies a classic Water Computer."""
    info = bluetooth.async_last_service_info(hass, address, connectable=True)
    if info is None:
        return False
    # A real Gardena advertisement carries only the Gardena manufacturer data.
    # Some proxies report merged advertisements of unrelated devices that also
    # contain a Gardena block; ignore those.
    if set(info.manufacturer_data) != {ManufacturerData.company}:
        return False
    raw = info.manufacturer_data[ManufacturerData.company]
    data = ManufacturerData.decode(raw)
    return data.group == ProductGroup.WATER_CONTROL and data.model in CLASSIC_MODELS


def _has_gardena_services(client: BleakClient) -> bool:
    return any(
        service.uuid.startswith(GARDENA_SERVICE_PREFIX) for service in client.services
    )


def _service_list(client: BleakClient) -> list[str]:
    return sorted(service.uuid for service in client.services)


class ClassicPairingConnection(CachedConnection):
    """Cached connection that pairs a classic Water Computer through the proxy."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        disconnect_delay: float,
        device_lookup: Callable[[], BLEDevice],
    ) -> None:
        """Initialize the connection."""
        super().__init__(disconnect_delay, device_lookup)
        self._hass = hass
        self._address = address
        self._proxy_cache_cleared = False

    async def _connect(self) -> BleakClient:
        if not is_classic_water_computer(self._hass, self._address):
            return await super()._connect()

        device = self._lookup()
        LOGGER.debug("Connecting to %s with pairing (classic Water Computer)", device.address)
        try:
            client = await self._connect_paired(device)
            if not _has_gardena_services(client) and not self._proxy_cache_cleared:
                # The proxy keeps its own copy of the GATT table. Clear it once
                # per Home Assistant run and rediscover on a new connection.
                self._proxy_cache_cleared = True
                LOGGER.warning(
                    "Gardena services hidden on %s (%s); clearing the proxy GATT "
                    "cache and reconnecting",
                    device.address,
                    _service_list(client),
                )
                try:
                    cleared = await client.clear_cache()
                except (BleakError, NotImplementedError) as err:
                    cleared = f"{type(err).__name__}: {err}"
                LOGGER.warning("Proxy GATT cache clear result: %s", cleared)
                await client.disconnect()
                client = await self._connect_paired(self._lookup())
        except NotImplementedError as err:
            # The proxy firmware cannot pair: keep the original behaviour.
            LOGGER.warning(
                "Proxy cannot pair with %s (%s); using an unpaired connection",
                device.address,
                err,
            )
            return await super()._connect()
        except BleakError as err:
            # No unpaired fallback: without pairing the device hides its Gardena
            # services, and an extra attempt only adds load on the device. The
            # coordinator retries at its normal interval.
            LOGGER.warning(
                "Paired connection to %s failed (%s: %s)",
                device.address,
                type(err).__name__,
                err,
            )
            raise

        services = _service_list(client)
        if _has_gardena_services(client):
            LOGGER.info(
                "Paired with %s, Gardena services visible: %s", device.address, services
            )
        else:
            LOGGER.warning(
                "Paired with %s but Gardena services are still hidden: %s",
                device.address,
                services,
            )
        self._client = client
        return client

    async def _connect_paired(self, device: BLEDevice) -> BleakClient:
        return await establish_connection(
            BleakClient,
            device,
            "Gardena Bluetooth",
            use_services_cache=False,
            max_attempts=max(self._max_attempts, CONNECT_ATTEMPTS),
            pair=True,
        )
