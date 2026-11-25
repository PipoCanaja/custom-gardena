"""Provides the DataUpdateCoordinator."""

from datetime import timedelta
import logging
from typing import Any, override

from gardena_bluetooth.client import Client
from gardena_bluetooth.const import AquaContour, DeviceConfiguration, DeviceInformation
from gardena_bluetooth.exceptions import (
    CharacteristicNoAccess,
    CharacteristicNotFound,
    CommunicationFailure,
    GardenaBluetoothException,
)
from gardena_bluetooth.parse import (
    Characteristic,
    CharacteristicBytes,
    CharacteristicSegmented,
    CharacteristicTime,
    CharacteristicType,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .classic_protocol import (
    HISTORY_UUIDS,
    VALVE_STATE_UUID,
    classic_slots,
    is_classic,
    slot_uuids,
)
from .const import CONF_PRODUCT_TYPE, DOMAIN

SCAN_INTERVAL = timedelta(seconds=60)
SEGMENTED_SCAN_INTERVAL = timedelta(minutes=30)
SEGMENTED_SCAN_COUNT = int(SEGMENTED_SCAN_INTERVAL / SCAN_INTERVAL)
LOGGER = logging.getLogger(__name__)

type GardenaBluetoothConfigEntry = ConfigEntry[GardenaBluetoothCoordinator]


class DeviceUnavailable(HomeAssistantError):
    """Raised if device can't be found."""


class GardenaBluetoothCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching data."""

    config_entry: GardenaBluetoothConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: GardenaBluetoothConfigEntry,
        logger: logging.Logger,
        client: Client,
        address: str,
    ) -> None:
        """Initialize global data updater."""
        super().__init__(
            hass=hass,
            logger=logger,
            config_entry=config_entry,
            name="Gardena Bluetooth Data Update Coordinator",
            update_interval=SCAN_INTERVAL,
        )
        self.address = address
        self.data = {}
        self.client = client
        self.characteristics: dict[str, Characteristic] = {}
        self._update_count = 0
        # custom-gardena phase 3: classic Water Computer only.
        self.classic = False
        self.classic_slots: list[int] = []
        self.slow_ids: set[str] = set()
        self._force_slow = False
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            name=config_entry.title,
        )

    @override
    async def async_shutdown(self) -> None:
        """Shutdown coordinator and any connection."""
        await super().async_shutdown()
        await self.client.disconnect()

    @override
    async def _async_setup(self) -> None:
        """Set up the coordinator and read initial device metadata."""
        try:
            chars = await self.client.get_all_characteristics()

            sw_version = await self.client.read_char(
                DeviceInformation.firmware_version, None
            )
            manufacturer = await self.client.read_char(
                DeviceInformation.manufacturer_name, None
            )
            model = await self.client.read_char(DeviceInformation.model_number, None)

            name = self.config_entry.title
            name = await self.client.read_char(
                DeviceConfiguration.custom_device_name, name
            )
            name = await self.client.read_char(AquaContour.custom_device_name, name)

            await self._update_timestamp(DeviceConfiguration.unix_timestamp)
            await self._update_timestamp(AquaContour.unix_timestamp)

            await self._setup_classic(chars)
            self.characteristics = chars
            self.device_info = DeviceInfo(
                identifiers={(DOMAIN, self.address)},
                connections={(dr.CONNECTION_BLUETOOTH, self.address)},
                name=name,
                sw_version=sw_version,
                manufacturer=manufacturer,
                model=model,
            )
        except (TimeoutError, CommunicationFailure, DeviceUnavailable) as exception:
            raise UpdateFailed(
                f"Unable to set up Gardena Bluetooth device due to {exception}"
            ) from exception

    async def _setup_classic(self, chars: dict[str, Characteristic]) -> None:
        """Detect a classic Water Computer and prepare its raw characteristics.

        Schedules and history are read as raw bytes (decoded in
        classic_protocol) and only every SEGMENTED_SCAN_INTERVAL, or right
        after a write or a change of the valve state. Nothing changes for
        other products: slow_ids stays empty.
        """
        # Service and characteristic UUIDs: the hybrid model is recognised by
        # its services. Raw client inside: no coordinator.client call here.
        async with self.client._client() as client:  # noqa: SLF001
            uuids = {service.uuid for service in client.services} | {
                char.uuid
                for service in client.services
                for char in service.characteristics
            }
        product_type = self.config_entry.data.get(CONF_PRODUCT_TYPE)
        self.classic = is_classic(product_type, uuids)
        if not self.classic:
            return
        self.classic_slots = classic_slots(uuids)
        slow = [uuid for slot in self.classic_slots for uuid in slot_uuids(slot)]
        slow += [uuid for uuid in HISTORY_UUIDS if uuid in uuids]
        for uuid in slow:
            chars[uuid] = CharacteristicBytes(uuid)
        self.slow_ids = set(slow)
        LOGGER.debug("Classic Water Computer, schedule slots %s", self.classic_slots)

    async def async_refresh_all(self) -> None:
        """Refresh now, including the slowly polled characteristics."""
        self._force_slow = True
        await self.async_refresh()

    async def _update_timestamp(self, char: CharacteristicTime) -> None:
        try:
            await self.client.update_timestamp(char, dt_util.now())
        except CharacteristicNotFound:
            pass
        except CharacteristicNoAccess:
            LOGGER.debug("No access to update internal time")

    @override
    async def _async_update_data(self) -> dict[str, Any]:
        """Poll the device."""
        # A context may name a characteristic this device lacks, so filter up front.
        unique_ids: set[str] = {
            unique_id
            for context in self.async_contexts()
            for unique_id in context
            if unique_id in self.characteristics
        }
        if not unique_ids:
            return {}

        # Segmented characteristics carry data that only changes when it is
        # taught to the device again, so they are read every nth poll only.
        skip_segmented = bool(self._update_count % SEGMENTED_SCAN_COUNT)
        self._update_count += 1
        skip_slow = skip_segmented and not self._force_slow
        self._force_slow = False

        data: dict[str, Any] = {}
        for unique_id in unique_ids:
            char = self.characteristics[unique_id]
            if (
                isinstance(char, CharacteristicSegmented)
                and skip_segmented
                and unique_id in self.data
            ):
                data[unique_id] = self.data[unique_id]
                continue
            if unique_id in self.slow_ids and skip_slow and unique_id in self.data:
                data[unique_id] = self.data[unique_id]
                continue

            try:
                data[unique_id] = await self.client.read_char(char)
            except CharacteristicNoAccess as exception:
                LOGGER.debug(
                    "Unable to get data for %s due to %s", unique_id, exception
                )
            except (GardenaBluetoothException, DeviceUnavailable) as exception:
                raise UpdateFailed(
                    f"Unable to update data for {unique_id} due to {exception}"
                ) from exception

        # A watering started or ended: read history and schedules next poll.
        if self.slow_ids and VALVE_STATE_UUID in data:
            previous = self.data.get(VALVE_STATE_UUID)
            if previous is not None and previous != data[VALVE_STATE_UUID]:
                self._force_slow = True
        return data

    def get_cached(
        self, char: Characteristic[CharacteristicType]
    ) -> CharacteristicType | None:
        """Read cached characteristic."""
        return self.data.get(char.unique_id)

    async def write(
        self, char: Characteristic[CharacteristicType], value: CharacteristicType
    ) -> None:
        """Write characteristic to device."""
        try:
            await self.client.write_char(char, value)
        except (GardenaBluetoothException, DeviceUnavailable) as exception:
            raise HomeAssistantError(
                f"Unable to write characteristic {char} dur to {exception}"
            ) from exception

        self.data[char.unique_id] = value
        await self.async_refresh()
