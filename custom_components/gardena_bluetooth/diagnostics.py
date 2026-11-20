"""Diagnostics support for Gardena Bluetooth.

Development aid (phase 1): dumps the full GATT table of the device and the raw
value of every readable characteristic, and decodes the classic Water Computer
schedule services (98bd0cN0). Read-only: nothing is ever written to the device.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .coordinator import GardenaBluetoothConfigEntry

READ_TIMEOUT = 10

TO_REDACT = {CONF_ADDRESS, "address"}

# Standard Device Information "Serial Number String" characteristic.
SERIAL_NUMBER_UUID = "00002a25-0000-1000-8000-00805f9b34fb"

# Classic Water Computer schedule services: 98bd0c10, 98bd0c20 ... 98bd0c90.
# The Aqua Contour also uses 98bd0c10, but with characteristics 98bd0c11..1f
# holding 11-byte records; the decoder below only labels offsets 1-6.
CLASSIC_SCHEDULE_SERVICE = re.compile(
    r"^98bd0c([1-9])0-0b0e-421a-84e5-ddbf75dc6de4$"
)

WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def _le_int(data: bytes) -> int:
    return int.from_bytes(data, "little", signed=False)


def _decode_schedule_char(offset: int, data: bytes) -> dict[str, Any]:
    """Decode one characteristic of a classic schedule service, best effort."""
    value = _le_int(data) if data else None
    if offset == 1 and value is not None:
        hours, rest = divmod(value, 3600)
        minutes, seconds = divmod(rest, 60)
        return {
            "field": "start_time",
            "seconds": value,
            "time": f"{hours:02d}:{minutes:02d}:{seconds:02d}",
        }
    if offset == 2:
        return {"field": "duration", "seconds": value}
    if offset == 3 and value is not None:
        return {
            "field": "weekdays",
            "mask": f"0x{value:02x}",
            "days": [day for bit, day in enumerate(WEEKDAYS) if value >> bit & 1],
        }
    if offset == 4 and value is not None:
        return {
            "field": "valve_link",
            "service": f"0x{value:04x}",
            "is_valve_0f10": value == 0x0F10,
        }
    if offset == 5:
        return {"field": "active", "value": bool(value)}
    if offset == 6 and value is not None:
        return {
            "field": "sensor_link",
            "service": f"0x{value:04x}",
            "uses_sensor": value != 0,
        }
    return {"field": "unknown"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GardenaBluetoothConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    result: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "known_characteristics": sorted(coordinator.characteristics),
            "cached_values": {
                key: repr(value) for key, value in sorted(coordinator.data.items())
            },
        },
    }

    gatt: list[dict[str, Any]] = []
    raw_reads: dict[str, dict[str, Any]] = {}
    schedules: dict[str, dict[str, Any]] = {}

    try:
        # Reuse the integration's cached connection. The device only accepts one
        # connection, so a second BleakClient must not be opened. The cached
        # connection is guarded by a non re-entrant lock: inside this block only
        # the raw BleakClient may be used, never coordinator.client methods.
        async with coordinator.client._client() as client:  # noqa: SLF001
            for service in client.services:
                service_info: dict[str, Any] = {
                    "uuid": service.uuid,
                    "handle": service.handle,
                    "description": service.description,
                    "characteristics": [],
                }
                schedule_match = CLASSIC_SCHEDULE_SERVICE.match(service.uuid)
                schedule_info: dict[str, Any] | None = None
                if schedule_match:
                    schedule_info = {"slot": int(schedule_match.group(1))}
                    schedules[service.uuid] = schedule_info

                for char in service.characteristics:
                    service_info["characteristics"].append(
                        {
                            "uuid": char.uuid,
                            "handle": char.handle,
                            "properties": list(char.properties),
                            "descriptors": [
                                {"uuid": desc.uuid, "handle": desc.handle}
                                for desc in char.descriptors
                            ],
                        }
                    )

                    if "read" not in char.properties:
                        continue

                    key = f"{service.uuid}/{char.uuid}"
                    try:
                        async with asyncio.timeout(READ_TIMEOUT):
                            data = bytes(await client.read_gatt_char(char))
                    except Exception as err:  # noqa: BLE001
                        raw_reads[key] = {"error": f"{type(err).__name__}: {err}"}
                        continue

                    if char.uuid == SERIAL_NUMBER_UUID:
                        raw_reads[key] = {"hex": "**REDACTED**", "length": len(data)}
                        continue

                    raw_reads[key] = {"hex": data.hex(" "), "length": len(data)}

                    if schedule_info is not None:
                        offset = int(char.uuid[7], 16)
                        schedule_info[char.uuid] = {
                            "hex": data.hex(" "),
                            **_decode_schedule_char(offset, data),
                        }

                gatt.append(service_info)
    except Exception as err:  # noqa: BLE001
        result["error"] = f"{type(err).__name__}: {err}"

    result["gatt"] = gatt
    result["raw_reads"] = raw_reads
    result["classic_schedules"] = schedules
    return result
