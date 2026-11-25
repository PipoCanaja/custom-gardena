"""Classic Water Computer: schedules, history and fixes (custom-gardena, phase 3).

Only used when the coordinator detected a classic Water Computer
(coordinator.classic). Other products get none of these entities or actions,
and keep the official entities unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, time
import logging
from typing import Any, override

from bleak import BleakClient
from gardena_bluetooth.const import DeviceConfiguration, Valve
import voluptuous as vol

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util import dt as dt_util

from .classic_protocol import (
    ACTIVATION_REASONS,
    HISTORY_COUNT_UUID,
    HISTORY_DURATIONS_UUID,
    HISTORY_REASONS_UUID,
    HISTORY_TIMESTAMPS_UUID,
    MAX_DURATION,
    SLOT_OFFSETS,
    WEEKDAYS,
    HistoryEntry,
    Schedule,
    activation_reason,
    char_uuid,
    decode_history,
    next_run,
    raw_from_reduction,
    reduction_from_raw,
    service_uuid,
    slot_uuids,
)
from .const import DOMAIN
from .coordinator import GardenaBluetoothCoordinator
from .entity import GardenaBluetoothEntity

LOGGER = logging.getLogger(__name__)

IO_TIMEOUT = 10
HISTORY_ATTRIBUTE_SIZE = 10

ATTR_ENTRY = "config_entry_id"
ATTR_SLOT = "slot"
ATTR_START = "start_time"
ATTR_DURATION = "duration"
ATTR_WEEKDAYS = "weekdays"
ATTR_ACTIVE = "active"
ATTR_SENSOR = "use_sensor"

SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_CLEAR_SCHEDULE = "clear_schedule"

_SLOT_SCHEMA = {
    vol.Optional(ATTR_ENTRY): cv.string,
    vol.Required(ATTR_SLOT): vol.All(vol.Coerce(int), vol.Range(min=1, max=9)),
}
SET_SCHEMA = vol.Schema(
    {
        **_SLOT_SCHEMA,
        vol.Optional(ATTR_START): cv.time,
        vol.Optional(ATTR_DURATION): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_DURATION)
        ),
        vol.Optional(ATTR_WEEKDAYS): vol.All(cv.ensure_list, [vol.In(WEEKDAYS)]),
        vol.Optional(ATTR_ACTIVE): cv.boolean,
        vol.Optional(ATTR_SENSOR): cv.boolean,
    }
)
CLEAR_SCHEMA = vol.Schema(_SLOT_SCHEMA)


# ---------------------------------------------------------------------------
# Reading and writing schedules
# ---------------------------------------------------------------------------


def cached_schedule(
    coordinator: GardenaBluetoothCoordinator, slot: int
) -> Schedule | None:
    """Schedule of a slot, decoded from the coordinator cache."""
    raw = {
        offset: coordinator.data.get(char_uuid(slot, offset)) for offset in SLOT_OFFSETS
    }
    return Schedule.decode(slot, raw)


def error(
    key: str,
    cls: type[HomeAssistantError] = ServiceValidationError,
    **placeholders: Any,
) -> HomeAssistantError:
    """Translated error (keys in the "exceptions" section of strings.json)."""
    return cls(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={k: str(v) for k, v in placeholders.items()},
    )


async def _read_slot(client: BleakClient, slot: int) -> Schedule:
    raw: dict[int, bytes | None] = {}
    for offset in SLOT_OFFSETS:
        async with asyncio.timeout(IO_TIMEOUT):
            raw[offset] = bytes(await client.read_gatt_char(char_uuid(slot, offset)))
    schedule = Schedule.decode(slot, raw)
    if schedule is None:
        raise error("schedule_read_failed", HomeAssistantError, slot=slot)
    return schedule


async def async_update_schedule(
    coordinator: GardenaBluetoothCoordinator,
    slot: int,
    *,
    clear: bool = False,
    **changes: Any,
) -> Schedule:
    """Read a slot from the device, apply changes, write it like the app does.

    The slot is read back afterwards; a difference raises an error. If a write
    fails half way, the slot stays inactive (the first write deactivates it).
    """
    if not coordinator.classic or slot not in coordinator.classic_slots:
        raise error("slot_unavailable", slot=slot)

    written: Schedule
    try:
        # Raw BleakClient: the cached connection lock is not re-entrant, so
        # the coordinator refresh only happens once this block is left.
        async with coordinator.client._client() as client:  # noqa: SLF001
            if service_uuid(slot) not in {s.uuid for s in client.services}:
                raise error("slot_unavailable", slot=slot)
            current = Schedule.empty(slot) if clear else await _read_slot(client, slot)
            target = current.updated(**changes)
            if problem := target.validate():
                raise error(problem, slot=slot, max=MAX_DURATION)
            for offset, data in target.write_plan():
                LOGGER.debug("Schedule %s +%s <- %s", slot, offset, data.hex(" "))
                async with asyncio.timeout(IO_TIMEOUT):
                    await client.write_gatt_char(
                        char_uuid(slot, offset), data, response=True
                    )
            written = await _read_slot(client, slot)
    except HomeAssistantError:
        raise
    except Exception as err:
        raise error(
            "schedule_write_failed",
            HomeAssistantError,
            slot=slot,
            error=f"{type(err).__name__}: {err}",
        ) from err
    finally:
        # Refresh even after a failure, so the entities show the real state.
        await coordinator.async_refresh_all()

    if not written.same_settings(target):
        raise error("schedule_verify_failed", HomeAssistantError, slot=slot)
    return written


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def coordinator_for_call(call: ServiceCall) -> GardenaBluetoothCoordinator:
    entries = [
        entry
        for entry in call.hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and entry.runtime_data.classic
    ]
    if entry_id := call.data.get(ATTR_ENTRY):
        entries = [entry for entry in entries if entry.entry_id == entry_id]
    if len(entries) != 1:
        raise error("no_classic_entry", count=len(entries))
    return entries[0].runtime_data


async def _async_set_schedule(call: ServiceCall) -> ServiceResponse:
    coordinator = coordinator_for_call(call)
    changes: dict[str, Any] = {}
    if ATTR_START in call.data:
        changes["start"] = call.data[ATTR_START]
    if ATTR_DURATION in call.data:
        changes["duration"] = call.data[ATTR_DURATION]
    if ATTR_WEEKDAYS in call.data:
        changes["weekdays"] = call.data[ATTR_WEEKDAYS]
    if ATTR_ACTIVE in call.data:
        changes["active"] = call.data[ATTR_ACTIVE]
    if ATTR_SENSOR in call.data:
        changes["use_sensor"] = call.data[ATTR_SENSOR]
    schedule = await async_update_schedule(coordinator, call.data[ATTR_SLOT], **changes)
    return schedule.as_dict()


async def _async_clear_schedule(call: ServiceCall) -> ServiceResponse:
    coordinator = coordinator_for_call(call)
    schedule = await async_update_schedule(
        coordinator, call.data[ATTR_SLOT], clear=True
    )
    return schedule.as_dict()


def async_setup_classic_services(hass: HomeAssistant) -> None:
    """Register the schedule actions."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SCHEDULE,
        _async_set_schedule,
        schema=SET_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_SCHEDULE,
        _async_clear_schedule,
        schema=CLEAR_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


class ClassicEntity(GardenaBluetoothEntity):
    """Base entity for the classic Water Computer."""

    def __init__(
        self,
        coordinator: GardenaBluetoothCoordinator,
        key: str,
        context: set[str],
        slot: int | None = None,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, context)
        self._slot = slot
        if slot is None:
            self._attr_unique_id = f"{coordinator.address}-classic_{key}"
            self._attr_translation_key = key
        else:
            self._attr_unique_id = (
                f"{coordinator.address}-classic_schedule_{slot}_{key}"
            )
            self._attr_translation_key = f"schedule_{key}"
            self._attr_translation_placeholders = {"slot": str(slot)}


class ScheduleEntity(ClassicEntity):
    """Entity bound to one schedule slot."""

    _slot: int

    def __init__(
        self, coordinator: GardenaBluetoothCoordinator, slot: int, key: str
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, key, set(slot_uuids(slot)), slot)
        self._schedule: Schedule | None = None

    @override
    def _handle_coordinator_update(self) -> None:
        self._schedule = cached_schedule(self.coordinator, self._slot)
        self._attr_available = self._schedule is not None
        self._update_from_schedule()
        super()._handle_coordinator_update()

    def _update_from_schedule(self) -> None:
        """Update the entity state from self._schedule."""

    async def _async_change(self, **changes: Any) -> None:
        await async_update_schedule(self.coordinator, self._slot, **changes)


class ScheduleActiveSwitch(ScheduleEntity, SwitchEntity):
    """Enable or disable a schedule slot."""

    def __init__(self, coordinator: GardenaBluetoothCoordinator, slot: int) -> None:
        """Initialize."""
        super().__init__(coordinator, slot, "active")

    @override
    def _update_from_schedule(self) -> None:
        schedule = self._schedule
        self._attr_is_on = None if schedule is None else schedule.active
        if schedule is None:
            self._attr_extra_state_attributes = {}
        else:
            attributes = schedule.as_dict()
            del attributes["slot"], attributes["active"]
            self._attr_extra_state_attributes = attributes

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the schedule."""
        await self._async_change(active=True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the schedule."""
        await self._async_change(active=False)


class ScheduleStartTime(ScheduleEntity, TimeEntity):
    """Start time of a schedule slot."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: GardenaBluetoothCoordinator, slot: int) -> None:
        """Initialize."""
        super().__init__(coordinator, slot, "start_time")

    @override
    def _update_from_schedule(self) -> None:
        self._attr_native_value = (
            None if self._schedule is None else self._schedule.start
        )

    @override
    async def async_set_value(self, value: time) -> None:
        """Change the start time."""
        await self._async_change(start=value)


class ScheduleDuration(ScheduleEntity, NumberEntity):
    """Duration of a schedule slot, in seconds."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_DURATION
    _attr_native_step = 1

    def __init__(self, coordinator: GardenaBluetoothCoordinator, slot: int) -> None:
        """Initialize."""
        super().__init__(coordinator, slot, "duration")

    @override
    def _update_from_schedule(self) -> None:
        self._attr_native_value = (
            None if self._schedule is None else self._schedule.duration
        )

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Change the duration."""
        await self._async_change(duration=int(value))


class SeasonalReduction(ClassicEntity, NumberEntity):
    """Reduction of every scheduled duration, as shown by the app."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1

    def __init__(self, coordinator: GardenaBluetoothCoordinator) -> None:
        """Initialize."""
        super().__init__(
            coordinator,
            "seasonal_reduction",
            {DeviceConfiguration.seasonal_adjust.unique_id},
        )

    @override
    def _handle_coordinator_update(self) -> None:
        self._attr_native_value = reduction_from_raw(
            self.coordinator.get_cached(DeviceConfiguration.seasonal_adjust)
        )
        super()._handle_coordinator_update()

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Change the reduction."""
        await self.coordinator.write(
            DeviceConfiguration.seasonal_adjust, raw_from_reduction(value)
        )


class ClassicActivationReason(ClassicEntity, SensorEntity):
    """Activation reason, decoded like the app does for the classic."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(ACTIVATION_REASONS)
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: GardenaBluetoothCoordinator) -> None:
        """Initialize, keeping the unique id of the official entity."""
        super().__init__(
            coordinator,
            "activation_reason",
            {Valve.state.unique_id, Valve.activation_reason.unique_id},
        )
        self._attr_unique_id = (
            f"{coordinator.address}-{Valve.activation_reason.unique_id}"
        )

    @override
    def _handle_coordinator_update(self) -> None:
        raw = self.coordinator.get_cached(Valve.activation_reason)
        self._attr_native_value = activation_reason(
            self.coordinator.get_cached(Valve.state),
            None if raw is None else int(raw),
        )
        super()._handle_coordinator_update()


def _local(value: datetime) -> datetime:
    return value.replace(tzinfo=dt_util.get_default_time_zone())


class NextWatering(ClassicEntity, SensorEntity):
    """Next scheduled watering."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: GardenaBluetoothCoordinator) -> None:
        """Initialize."""
        context = {
            uuid for slot in coordinator.classic_slots for uuid in slot_uuids(slot)
        }
        context.add(DeviceConfiguration.seasonal_adjust.unique_id)
        super().__init__(coordinator, "next_watering", context)

    @override
    def _handle_coordinator_update(self) -> None:
        schedules = [
            schedule
            for slot in self.coordinator.classic_slots
            if (schedule := cached_schedule(self.coordinator, slot)) is not None
        ]
        reduction = reduction_from_raw(
            self.coordinator.get_cached(DeviceConfiguration.seasonal_adjust)
        )
        found = next_run(schedules, dt_util.now().replace(tzinfo=None))
        attributes: dict[str, Any] = {"seasonal_reduction": reduction}
        self._attr_native_value = None
        if found is not None:
            start, schedule = found
            attributes["slot"] = schedule.slot
            attributes["duration"] = schedule.duration
            attributes["next_schedule_start"] = _local(start).isoformat()
            # At 100 % reduction the device runs every schedule for 0 s.
            if reduction != 100:
                self._attr_native_value = _local(start)
        self._attr_extra_state_attributes = attributes
        super()._handle_coordinator_update()


class LastWatering(ClassicEntity, SensorEntity):
    """End of the last entry of the watering history."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: GardenaBluetoothCoordinator) -> None:
        """Initialize."""
        super().__init__(
            coordinator,
            "last_watering",
            {
                HISTORY_TIMESTAMPS_UUID,
                HISTORY_COUNT_UUID,
                HISTORY_REASONS_UUID,
                HISTORY_DURATIONS_UUID,
            },
        )

    def _history(self) -> list[HistoryEntry]:
        data: Mapping[str, Any] = self.coordinator.data
        count = data.get(HISTORY_COUNT_UUID)
        return decode_history(
            data.get(HISTORY_TIMESTAMPS_UUID),
            data.get(HISTORY_REASONS_UUID),
            data.get(HISTORY_DURATIONS_UUID),
            count[0] if count else None,
        )

    @override
    def _handle_coordinator_update(self) -> None:
        history = self._history()
        if not history:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {"history": []}
        else:
            last = history[-1]
            self._attr_native_value = _local(last.end)
            self._attr_extra_state_attributes = {
                "start": _local(last.start).isoformat(),
                "duration": last.duration,
                "status": last.status,
                "reason": last.reason,
                "history": [
                    entry.as_dict()
                    for entry in reversed(history[-HISTORY_ATTRIBUTE_SIZE:])
                ],
            }
        super()._handle_coordinator_update()


# ---------------------------------------------------------------------------
# Platform hooks, called from the official platform files
# ---------------------------------------------------------------------------


def classic_switches(coordinator: GardenaBluetoothCoordinator) -> list[SwitchEntity]:
    """Switch entities of the classic Water Computer."""
    if not coordinator.classic:
        return []
    return [
        ScheduleActiveSwitch(coordinator, slot) for slot in coordinator.classic_slots
    ]


def classic_times(coordinator: GardenaBluetoothCoordinator) -> list[TimeEntity]:
    """Time entities of the classic Water Computer."""
    if not coordinator.classic:
        return []
    return [ScheduleStartTime(coordinator, slot) for slot in coordinator.classic_slots]


def classic_numbers(
    hass: HomeAssistant, coordinator: GardenaBluetoothCoordinator
) -> list[NumberEntity]:
    """Number entities of the classic Water Computer."""
    if not coordinator.classic:
        return []
    entities: list[NumberEntity] = [
        ScheduleDuration(coordinator, slot) for slot in coordinator.classic_slots
    ]
    if DeviceConfiguration.seasonal_adjust.unique_id in coordinator.characteristics:
        entities.append(SeasonalReduction(coordinator))
        # Replaces the official "seasonal adjust" entity (shown in days).
        registry = er.async_get(hass)
        if entity_id := registry.async_get_entity_id(
            "number",
            DOMAIN,
            f"{coordinator.address}-{DeviceConfiguration.seasonal_adjust.unique_id}",
        ):
            registry.async_remove(entity_id)
    return entities


def classic_sensors(coordinator: GardenaBluetoothCoordinator) -> list[SensorEntity]:
    """Sensor entities of the classic Water Computer."""
    if not coordinator.classic:
        return []
    entities: list[SensorEntity] = [NextWatering(coordinator)]
    if HISTORY_TIMESTAMPS_UUID in coordinator.characteristics:
        entities.append(LastWatering(coordinator))
    if {Valve.state.unique_id, Valve.activation_reason.unique_id}.issubset(
        coordinator.characteristics
    ):
        entities.append(ClassicActivationReason(coordinator))
    return entities


# Official descriptions replaced on the classic Water Computer.
REPLACED_NUMBER_KEYS = {DeviceConfiguration.seasonal_adjust.unique_id}
REPLACED_SENSOR_KEYS = {Valve.activation_reason.unique_id}
