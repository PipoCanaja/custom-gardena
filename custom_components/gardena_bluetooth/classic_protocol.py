"""Protocol helpers for the classic Water Computer (custom-gardena, phase 3).

No Home Assistant import here, so the logic can be unit tested on its own.

Everything below was checked against the official app (decompiled) and against
the device itself (firmware 1.7.23.29):

Schedules, services 98bd0cN0 (N = slot), characteristics 98bd0cN1..6:
  +1 start time   uint32 LE, seconds since local midnight
  +2 duration     uint32 LE, seconds
  +3 weekdays     uint8, bit 0 = Monday ... bit 6 = Sunday
  +4 valve link   2 bytes, 10 0F (valve service 0x0f10)
  +5 active       uint8, 0 or 1
  +6 sensor link  2 bytes, 10 00 with sensor, 00 00 without
The app always writes: active=0, then +1 +2 +3 +6 +4, then active=1.
This module writes the same sequence; the valve link is 10 0F for a configured
slot (duration and days set) and 00 00 for an empty one, like a cleared slot.

Seasonal adjust 98bd0b12: percentage of the scheduled duration that is applied
(100 = full duration, 0 = every schedule runs for 0 s). The app shows
"reduction" = 100 - raw.

Activation reason 98bd0f15: only meaningful while the valve is open. The app
treats 0 as a manual watering and anything else as a scheduled one. The value
is kept after the valve closes.

Watering history 98bd0d11..14:
  0d11 end timestamps (uint32 LE each, local time stored as if it were UTC)
  0d12 number of entries
  0d13 one byte per entry: how the watering ended (exact values, not a mask)
  0d14 durations (uint32 LE each, seconds actually watered)
Status as in the app: reason 0 -> watered; otherwise duration > 0 ->
partially watered, duration 0 -> skipped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, UTC

BASE = "-0b0e-421a-84e5-ddbf75dc6de4"
PRODUCT_WATER_COMPUTER = "WATER_COMPUTER"
VALVE_STATE_UUID = f"98bd0f11{BASE}"
HYBRID_PREFIXES = ("98bda0", "98bda1")

SEASONAL_UUID = f"98bd0b12{BASE}"
ACTIVATION_REASON_UUID = f"98bd0f15{BASE}"
HISTORY_TIMESTAMPS_UUID = f"98bd0d11{BASE}"
HISTORY_COUNT_UUID = f"98bd0d12{BASE}"
HISTORY_REASONS_UUID = f"98bd0d13{BASE}"
HISTORY_DURATIONS_UUID = f"98bd0d14{BASE}"
HISTORY_UUIDS = (
    HISTORY_TIMESTAMPS_UUID,
    HISTORY_COUNT_UUID,
    HISTORY_REASONS_UUID,
    HISTORY_DURATIONS_UUID,
)

MAX_SLOTS = 9
OFFSET_START = 1
OFFSET_DURATION = 2
OFFSET_WEEKDAYS = 3
OFFSET_VALVE_LINK = 4
OFFSET_ACTIVE = 5
OFFSET_SENSOR_LINK = 6
SLOT_OFFSETS = range(1, 7)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
VALVE_LINK = bytes((0x10, 0x0F))
SENSOR_LINK = bytes((0x10, 0x00))
NO_LINK = bytes(2)
MAX_DURATION = 24 * 3600 - 1

SKIP_REASONS: Mapping[int, str] = {
    1: "sensor",
    2: "manual",
    4: "battery",
    8: "watering_pause",
    16: "valve_detached",
    32: "hard_fault",
    64: "dry_run",
    128: "temperature",
}
HISTORY_STATUSES = ("watered", "partially_watered", "skipped")
ACTIVATION_REASONS = ("none", "manual", "schedule")


def char_uuid(slot: int, offset: int) -> str:
    """UUID of one characteristic of a classic schedule slot."""
    return f"98bd0c{slot:x}{offset:x}{BASE}"


def service_uuid(slot: int) -> str:
    """UUID of a classic schedule service."""
    return char_uuid(slot, 0)


def slot_uuids(slot: int) -> list[str]:
    """UUIDs of the six characteristics of a slot."""
    return [char_uuid(slot, offset) for offset in SLOT_OFFSETS]


def is_classic(product_type: str | None, uuids: Iterable[str]) -> bool:
    """Return True for a classic Water Computer (not the hybrid, not others).

    `uuids` holds the service and characteristic UUIDs of the device. Product
    type WATER_COMPUTER also covers the hybrid model, hence the service checks.
    """
    uuids = set(uuids)
    return (
        product_type == PRODUCT_WATER_COMPUTER
        and VALVE_STATE_UUID in uuids
        and char_uuid(1, OFFSET_START) in uuids
        and not any(uuid.startswith(HYBRID_PREFIXES) for uuid in uuids)
    )


def classic_slots(char_uuids: Iterable[str]) -> list[int]:
    """Slots whose six characteristics are all present."""
    uuids = set(char_uuids)
    return [
        slot
        for slot in range(1, MAX_SLOTS + 1)
        if all(uuid in uuids for uuid in slot_uuids(slot))
    ]


def _u32(data: bytes) -> int:
    return int.from_bytes(data[:4], "little", signed=False)


def encode_u32(value: int) -> bytes:
    """Encode an unsigned 32-bit little endian value."""
    return int(value).to_bytes(4, "little", signed=False)


def time_to_seconds(value: time) -> int:
    """Seconds since midnight."""
    return value.hour * 3600 + value.minute * 60 + value.second


def seconds_to_time(value: int) -> time:
    """Time of day from seconds since midnight (wraps at 24 h)."""
    value %= 24 * 3600
    hours, rest = divmod(value, 3600)
    minutes, seconds = divmod(rest, 60)
    return time(hours, minutes, seconds)


def weekdays_from_mask(mask: int) -> frozenset[str]:
    """Day names from the weekday mask (bit 0 = Monday)."""
    return frozenset(day for bit, day in enumerate(WEEKDAYS) if mask >> bit & 1)


def mask_from_weekdays(days: Iterable[str]) -> int:
    """Weekday mask from day names."""
    mask = 0
    for day in days:
        mask |= 1 << WEEKDAYS.index(day)
    return mask


def sorted_weekdays(days: Iterable[str]) -> list[str]:
    """Day names in calendar order."""
    return [day for day in WEEKDAYS if day in set(days)]


@dataclass(frozen=True)
class Schedule:
    """Decoded classic schedule slot."""

    slot: int
    start: time
    duration: int
    weekdays: frozenset[str]
    active: bool
    use_sensor: bool
    valve_linked: bool

    @property
    def configured(self) -> bool:
        """Slot has a duration and at least one day."""
        return self.duration > 0 and bool(self.weekdays)

    @property
    def enabled(self) -> bool:
        """Slot will run (active and configured)."""
        return self.active and self.configured

    @classmethod
    def empty(cls, slot: int) -> Schedule:
        """An empty, inactive slot."""
        return cls(slot, time(0), 0, frozenset(), False, False, False)

    @classmethod
    def decode(cls, slot: int, raw: Mapping[int, bytes | None]) -> Schedule | None:
        """Decode a slot from its raw characteristics, keyed by offset."""
        values = [raw.get(offset) for offset in SLOT_OFFSETS]
        if any(value is None or len(value) == 0 for value in values):
            return None
        start, duration, weekdays, valve, active, sensor = values
        assert start is not None and duration is not None and weekdays is not None
        assert valve is not None and active is not None and sensor is not None
        return cls(
            slot=slot,
            start=seconds_to_time(_u32(start)),
            duration=_u32(duration),
            weekdays=weekdays_from_mask(weekdays[0]),
            active=active[0] != 0,
            use_sensor=any(sensor),
            valve_linked=bytes(valve[:2]) == VALVE_LINK,
        )

    def updated(
        self,
        *,
        start: time | None = None,
        duration: int | None = None,
        weekdays: Iterable[str] | None = None,
        active: bool | None = None,
        use_sensor: bool | None = None,
    ) -> Schedule:
        """Copy with some fields changed."""
        return replace(
            self,
            start=self.start if start is None else start.replace(microsecond=0),
            duration=self.duration if duration is None else int(duration),
            weekdays=self.weekdays if weekdays is None else frozenset(weekdays),
            active=self.active if active is None else active,
            use_sensor=self.use_sensor if use_sensor is None else use_sensor,
        )

    def validate(self) -> str | None:
        """Return an error code if the slot cannot be written as is.

        Codes: invalid_duration, invalid_weekdays, incomplete_schedule (the
        translation keys of the matching errors).
        """
        if not 0 <= self.duration <= MAX_DURATION:
            return "invalid_duration"
        if set(self.weekdays) - set(WEEKDAYS):
            return "invalid_weekdays"
        if self.active and not self.configured:
            return "incomplete_schedule"
        return None

    def same_settings(self, other: Schedule) -> bool:
        """Same start, duration, days, state and sensor link."""
        return (
            self.start,
            self.duration,
            self.weekdays,
            self.active,
            self.use_sensor,
        ) == (
            other.start,
            other.duration,
            other.weekdays,
            other.active,
            other.use_sensor,
        )

    def raw(self) -> dict[int, bytes]:
        """Raw characteristic values, keyed by offset."""
        return {
            OFFSET_START: encode_u32(time_to_seconds(self.start)),
            OFFSET_DURATION: encode_u32(self.duration),
            OFFSET_WEEKDAYS: bytes((mask_from_weekdays(self.weekdays),)),
            OFFSET_VALVE_LINK: VALVE_LINK if self.configured else NO_LINK,
            OFFSET_ACTIVE: b"\x01" if self.active else b"\x00",
            OFFSET_SENSOR_LINK: SENSOR_LINK if self.use_sensor else NO_LINK,
        }

    def write_plan(self) -> list[tuple[int, bytes]]:
        """Ordered writes, same order as the app: deactivate, fields, activate."""
        raw = self.raw()
        plan = [(OFFSET_ACTIVE, b"\x00")]
        plan += [
            (offset, raw[offset])
            for offset in (
                OFFSET_START,
                OFFSET_DURATION,
                OFFSET_WEEKDAYS,
                OFFSET_SENSOR_LINK,
                OFFSET_VALVE_LINK,
            )
        ]
        if self.active:
            plan.append((OFFSET_ACTIVE, b"\x01"))
        return plan

    def as_dict(self) -> dict[str, object]:
        """Readable form, for attributes and action responses."""
        return {
            "slot": self.slot,
            "active": self.active,
            "start_time": self.start.isoformat(),
            "duration": self.duration,
            "weekdays": sorted_weekdays(self.weekdays),
            "use_sensor": self.use_sensor,
        }


def describe_slot_char(offset: int, data: bytes) -> dict[str, object]:
    """Decode one raw characteristic of a slot, best effort (diagnostics)."""
    value = int.from_bytes(data, "little", signed=False) if data else None
    if value is None:
        return {"field": "unknown"}
    if offset == OFFSET_START:
        return {
            "field": "start_time",
            "seconds": value,
            "time": seconds_to_time(value).isoformat(),
        }
    if offset == OFFSET_DURATION:
        return {"field": "duration", "seconds": value}
    if offset == OFFSET_WEEKDAYS:
        return {
            "field": "weekdays",
            "mask": f"0x{value:02x}",
            "days": sorted_weekdays(weekdays_from_mask(value)),
        }
    if offset == OFFSET_VALVE_LINK:
        return {
            "field": "valve_link",
            "service": f"0x{value:04x}",
            "is_valve_0f10": value == 0x0F10,
        }
    if offset == OFFSET_ACTIVE:
        return {"field": "active", "value": bool(value)}
    if offset == OFFSET_SENSOR_LINK:
        return {
            "field": "sensor_link",
            "service": f"0x{value:04x}",
            "uses_sensor": value != 0,
        }
    return {"field": "unknown"}


def device_datetime(value: int) -> datetime:
    """Naive local datetime from a device timestamp (local time stored as UTC)."""
    return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class HistoryEntry:
    """One entry of the watering history."""

    end: datetime
    duration: int
    reason: str | None
    raw_reason: int

    @property
    def start(self) -> datetime:
        """Start of the watering (end minus watered duration)."""
        return self.end - timedelta(seconds=self.duration)

    @property
    def status(self) -> str:
        """Status as the app shows it."""
        if self.reason is None:
            return "watered"
        if self.duration > 0:
            return "partially_watered"
        return "skipped"

    def as_dict(self) -> dict[str, object]:
        """Readable form, for attributes."""
        return {
            "end": self.end.isoformat(),
            "duration": self.duration,
            "status": self.status,
            "reason": self.reason,
        }


def decode_history(
    timestamps: bytes | None,
    reasons: bytes | None,
    durations: bytes | None,
    count: int | None = None,
) -> list[HistoryEntry]:
    """Decode the watering history, oldest first."""
    if not timestamps or reasons is None or durations is None:
        return []
    size = min(len(timestamps) // 4, len(reasons), len(durations) // 4)
    if count is not None:
        size = min(size, max(count, 0))
    entries = []
    for index in range(size):
        stamp = _u32(timestamps[index * 4 : index * 4 + 4])
        if stamp == 0:
            continue
        raw_reason = reasons[index]
        entries.append(
            HistoryEntry(
                end=device_datetime(stamp),
                duration=_u32(durations[index * 4 : index * 4 + 4]),
                reason=SKIP_REASONS.get(raw_reason),
                raw_reason=raw_reason,
            )
        )
    entries.sort(key=lambda entry: entry.end)
    return entries


def next_run(
    schedules: Iterable[Schedule], now: datetime
) -> tuple[datetime, Schedule] | None:
    """Next start among enabled schedules, strictly after `now` (naive local)."""
    best: tuple[datetime, Schedule] | None = None
    enabled = [schedule for schedule in schedules if schedule.enabled]
    for days in range(8):
        day: date = now.date() + timedelta(days=days)
        weekday = WEEKDAYS[day.weekday()]
        for schedule in enabled:
            if weekday not in schedule.weekdays:
                continue
            start = datetime.combine(day, schedule.start)
            if start <= now:
                continue
            if best is None or (start, schedule.slot) < (best[0], best[1].slot):
                best = (start, schedule)
        if best is not None:
            return best
    return best


def reduction_from_raw(raw: int | None) -> int | None:
    """Reduction shown by the app (%), from the raw seasonal adjust value."""
    if raw is None:
        return None
    return max(0, min(100, 100 - int(raw)))


def effective_duration(duration: int, reduction: int | None) -> int:
    """Duration the device actually waters, after the seasonal reduction.

    Measured on the device: 120 s at 9 % reduction (raw 91), twice: the valve
    closed 109 s and 110 s after the scheduled start; the history recorded
    108 s and 110 s (about +/- 1 s around 109 s).
    """
    if reduction is None:
        return duration
    return duration * (100 - reduction) // 100


def raw_from_reduction(reduction: float) -> int:
    """Raw seasonal adjust value from a reduction in %."""
    return 100 - max(0, min(100, round(reduction)))


def activation_reason(valve_open: bool | None, raw: int | None) -> str | None:
    """Activation reason as the app reads it."""
    if valve_open is None:
        return None
    if not valve_open:
        return "none"
    if raw is None:
        return None
    return "manual" if int(raw) == 0 else "schedule"
