"""Debug action for classic Water Computer schedules (not for upstream).

debug_read_schedule reads one schedule slot straight from the device and
returns every raw characteristic with its decoding. It only works on a
classic Water Computer (see classic_protocol.is_classic). Writing is done by
the real actions set_schedule and clear_schedule (classic.py).
"""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .classic import ATTR_ENTRY, ATTR_SLOT, coordinator_for_call, error
from .classic_protocol import SLOT_OFFSETS, char_uuid, describe_slot_char
from .const import DOMAIN

IO_TIMEOUT = 10

READ_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY): cv.string,
        vol.Required(ATTR_SLOT): vol.All(vol.Coerce(int), vol.Range(min=1, max=9)),
    }
)


async def _async_read(call: ServiceCall) -> ServiceResponse:
    coordinator = coordinator_for_call(call)
    slot: int = call.data[ATTR_SLOT]
    if slot not in coordinator.classic_slots:
        raise error("slot_unavailable", slot=slot)
    result: dict[str, Any] = {"slot": slot}
    try:
        # Raw BleakClient only: the cached connection lock is not re-entrant.
        async with coordinator.client._client() as client:  # noqa: SLF001
            for offset in SLOT_OFFSETS:
                async with asyncio.timeout(IO_TIMEOUT):
                    data = bytes(await client.read_gatt_char(char_uuid(slot, offset)))
                result[f"+{offset}"] = {
                    "hex": data.hex(" "),
                    **describe_slot_char(offset, data),
                }
    except Exception as err:
        raise error("schedule_read_failed", HomeAssistantError, slot=slot) from err
    return result


def async_setup_debug_services(hass: HomeAssistant) -> None:
    """Register the debug action."""
    hass.services.async_register(
        DOMAIN,
        "debug_read_schedule",
        _async_read,
        schema=READ_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
