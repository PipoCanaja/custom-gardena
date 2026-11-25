"""Support for time entities (custom-gardena: classic schedule start times)."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .classic import classic_times
from .coordinator import GardenaBluetoothConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GardenaBluetoothConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up time entities based on a config entry."""
    async_add_entities(classic_times(entry.runtime_data))
