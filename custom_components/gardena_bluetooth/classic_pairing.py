"""Recovery path for the classic Water Computer when its services are hidden.

Experimental. Observed on a classic Water Computer (model 0, variant 1): after
some connections the proxy only listed the generic services (1800, 1801, 180a)
and the TI OAD service (f000ffc0), without the Gardena services (98bd...).
Clearing the proxy GATT cache and reconnecting with pairing brought them back.

Pairing on every connection (v0.0.2 to v0.0.5) was followed twice by the device
ignoring all connection requests (proxy disconnect reason 0x3e) until its
batteries were removed. Since v0.0.6 the normal connection is the unchanged
CachedConnection._connect, as in the official integration. Only when the
Gardena services are missing does this class clear the proxy cache and
reconnect with pairing, once per Home Assistant run.

Only devices whose last advertisement identifies a classic Water Computer use
the recovery path. Every other device goes through CachedConnection._connect
with no change.
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
        client = await super()._connect()
        if not is_classic_water_computer(self._hass, self._address):
            return client
        if _has_gardena_services(client):
            return client

        LOGGER.warning(
            "Gardena services hidden on %s (%s)", self._address, _service_list(client)
        )
        if self._proxy_cache_cleared:
            # Already tried once in this run: do not insist, keep the plain client.
            LOGGER.warning(
                "Proxy cache already cleared and pairing already tried for %s in "
                "this run; not retrying",
                self._address,
            )
            return client

        # The proxy keeps its own copy of the GATT table. Clear it once per Home
        # Assistant run, then reconnect with pairing and a fresh discovery.
        self._proxy_cache_cleared = True
        try:
            cleared = await client.clear_cache()
        except (BleakError, NotImplementedError) as err:
            cleared = f"{type(err).__name__}: {err}"
        LOGGER.warning("Proxy GATT cache clear result: %s", cleared)
        self._client = None
        await client.disconnect()

        device = self._lookup()
        LOGGER.warning("Reconnecting to %s with pairing", device.address)
        try:
            client = await establish_connection(
                BleakClient,
                device,
                "Gardena Bluetooth",
                use_services_cache=False,
                max_attempts=self._max_attempts,
                pair=True,
            )
        except NotImplementedError as err:
            LOGGER.warning("Proxy cannot pair with %s (%s)", device.address, err)
            client = await super()._connect()
        except BleakError as err:
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
