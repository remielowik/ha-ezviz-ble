"""Binary sensor platform for the EZVIZ Bluetooth integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EzvizBleConfigEntry, EzvizBleCoordinator
from .entity import EzvizBleEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizBleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the door sensor."""
    async_add_entities([EzvizBleDoorSensor(entry.runtime_data)])


class EzvizBleDoorSensor(EzvizBleEntity, BinarySensorEntity):
    """Door open/closed as reported by the lock."""

    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(self, coordinator: EzvizBleCoordinator) -> None:
        super().__init__(coordinator, "door")

    @property
    def available(self) -> bool:
        """Unavailable when the lock does not report the door state."""
        return (
            super().available
            and (state := self.lock_state) is not None
            and state.door_open is not None
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether the door is open."""
        if (state := self.lock_state) is None:
            return None
        return state.door_open
