"""Debug action for classic Water Computer schedules (not for upstream).

debug_read_schedule reads and decodes one schedule slot, limited to a classic
Water Computer (product type WATER_COMPUTER, valve service 98bd0f10 present,
no hybrid service). Writing moved to the real actions set_schedule and
clear_schedule (classic.py) in phase 3.

Encoding, confirmed by the official app (WriteCharacteristic.Schedule.*) and by
reading the device:

  +1 start time   uint32 LE, seconds since local midnight
  +2 duration     uint32 LE, seconds
  +3 weekdays     uint8, bit 0 = Monday ... bit 6 = Sunday
  +4 valve link   2 bytes, 10 0F (valve service 0x0f10)
  +5 active       uint8, 0 or 1
  +6 sensor link  2 bytes, 10 00 with sensor, 00 00 without

Write order: deactivate first, write the fields, activate last, so the device
never holds an active schedule with half-written fields.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bleak import BleakClient
from gardena_bluetooth.parse import ProductType
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv

from .const import CONF_PRODUCT_TYPE, DOMAIN
from .diagnostics import _decode_schedule_char

LOGGER = logging.getLogger(__name__)

IO_TIMEOUT = 10
BASE = "-0b0e-421a-84e5-ddbf75dc6de4"
VALVE_SERVICE = f"98bd0f10{BASE}"
HYBRID_PREFIXES = ("98bda000", "98bda100")

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
VALVE_LINK = bytes((0x10, 0x0F))
SENSOR_LINK = bytes((0x10, 0x00))
NO_LINK = bytes(2)

ATTR_ENTRY = "config_entry_id"
ATTR_SLOT = "slot"
ATTR_START = "start_time"
ATTR_DURATION = "duration"
ATTR_WEEKDAYS = "weekdays"
ATTR_ACTIVE = "active"
ATTR_SENSOR = "use_sensor"

BASE_SCHEMA = {
    vol.Optional(ATTR_ENTRY): cv.string,
    vol.Required(ATTR_SLOT): vol.All(vol.Coerce(int), vol.Range(min=1, max=9)),
}
READ_SCHEMA = vol.Schema(BASE_SCHEMA)


def _uuid(slot: int, offset: int) -> str:
    return f"98bd0c{slot}{offset}{BASE}"


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "little")


def _get_coordinator(hass: HomeAssistant, call: ServiceCall):
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(ATTR_ENTRY):
        entries = [entry for entry in entries if entry.entry_id == entry_id]
    if len(entries) != 1:
        raise ServiceValidationError(
            f"Expected exactly one loaded Gardena entry, found {len(entries)}; "
            f"pass {ATTR_ENTRY}"
        )
    entry = entries[0]
    if entry.data.get(CONF_PRODUCT_TYPE) != ProductType.WATER_COMPUTER.name:
        raise ServiceValidationError("Only the classic Water Computer is supported")
    return entry.runtime_data


def _check_classic(client: BleakClient, slot: int) -> None:
    uuids = {service.uuid for service in client.services}
    if VALVE_SERVICE not in uuids or any(
        uuid.startswith(HYBRID_PREFIXES) for uuid in uuids
    ):
        raise ServiceValidationError("Device is not a classic Water Computer")
    if f"98bd0c{slot}0{BASE}" not in uuids:
        raise ServiceValidationError(f"Schedule slot {slot} not present on device")


async def _read_slot(client: BleakClient, slot: int) -> dict[str, Any]:
    result: dict[str, Any] = {"slot": slot}
    for offset in range(1, 7):
        async with asyncio.timeout(IO_TIMEOUT):
            data = bytes(await client.read_gatt_char(_uuid(slot, offset)))
        result[f"+{offset}"] = {
            "hex": data.hex(" "),
            **_decode_schedule_char(offset, data),
        }
    return result


async def _write(client: BleakClient, slot: int, offset: int, data: bytes) -> None:
    LOGGER.info("Schedule %s +%s <- %s", slot, offset, data.hex(" "))
    async with asyncio.timeout(IO_TIMEOUT):
        await client.write_gatt_char(_uuid(slot, offset), data, response=True)


async def _with_client(hass: HomeAssistant, call: ServiceCall, func) -> Any:
    coordinator = _get_coordinator(hass, call)
    slot = call.data[ATTR_SLOT]
    try:
        # Raw BleakClient only: the cached connection lock is not re-entrant.
        async with coordinator.client._client() as client:  # noqa: SLF001
            _check_classic(client, slot)
            return await func(client, slot)
    except ServiceValidationError:
        raise
    except Exception as err:
        raise HomeAssistantError(f"{type(err).__name__}: {err}") from err


async def _async_read(call: ServiceCall) -> ServiceResponse:
    return await _with_client(call.hass, call, _read_slot)


def async_setup_debug_services(hass: HomeAssistant) -> None:
    """Register the debug actions."""
    hass.services.async_register(
        DOMAIN,
        "debug_read_schedule",
        _async_read,
        schema=READ_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
