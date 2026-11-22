"""Pairing through the Bluetooth proxy for the classic Water Computer.

Experimental. Observed on a classic Water Computer (model 0, variant 1): over an
unencrypted link the device only exposes the generic services (1800, 1801,
180a) and the TI OAD service (f000ffc0); the Gardena services (98bd...) are not
listed. The official app bonds the device. This connection asks the proxy to
pair right after the link is established, before services are discovered, and
rediscovers services without using the cache.

Only devices whose last advertisement identifies a classic Water Computer use
this path. Every other device, and any failure of the pairing attempt, goes
through the unchanged CachedConnection._connect.
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
    raw = info.manufacturer_data.get(ManufacturerData.company)
    if not raw:
        return False
    data = ManufacturerData.decode(raw)
    return data.group == ProductGroup.WATER_CONTROL and data.model in CLASSIC_MODELS


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

    async def _connect(self) -> BleakClient:
        if not is_classic_water_computer(self._hass, self._address):
            return await super()._connect()

        device = self._lookup()
        LOGGER.debug("Connecting to %s with pairing (classic Water Computer)", device.address)
        try:
            client = await establish_connection(
                BleakClient,
                device,
                "Gardena Bluetooth",
                use_services_cache=False,
                max_attempts=self._max_attempts,
                pair=True,
            )
        except (BleakError, NotImplementedError) as err:
            LOGGER.warning(
                "Pairing with %s through the proxy failed (%s: %s); "
                "falling back to an unpaired connection",
                device.address,
                type(err).__name__,
                err,
            )
            return await super()._connect()

        services = sorted(service.uuid for service in client.services)
        if any(uuid.startswith(GARDENA_SERVICE_PREFIX) for uuid in services):
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
