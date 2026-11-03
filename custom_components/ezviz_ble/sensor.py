"""Sensor platform for the EZVIZ Bluetooth integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EzvizBleConfigEntry, EzvizBleCoordinator, EzvizBleData
from .ezlock.protocol import PartsInfo
from .entity import EzvizBleEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EzvizBleBatteryDescription(SensorEntityDescription):
    """Describes a battery sensor of the lock or one of its parts."""

    value_fn: Callable[[EzvizBleData], int | None]
    exists_fn: Callable[[PartsInfo | None], bool] = lambda _parts: True


BATTERY_SENSORS = (
    EzvizBleBatteryDescription(
        key="battery",
        # The door state carries the lock battery too, so this works even if
        # the separate battery query fails.
        value_fn=lambda data: data.state.battery,
    ),
    EzvizBleBatteryDescription(
        key="keypad_battery",
        translation_key="keypad_battery",
        value_fn=lambda data: data.battery.keypad if data.battery else None,
        exists_fn=lambda parts: parts is not None and parts.keypad is not None,
    ),
    EzvizBleBatteryDescription(
        key="door_contact_battery",
        translation_key="door_contact_battery",
        value_fn=lambda data: data.battery.door_contact if data.battery else None,
        exists_fn=lambda parts: parts is not None and parts.door_contact is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizBleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up battery sensors for the lock and the parts bound to it."""
    coordinator = entry.runtime_data
    async_add_entities(
        EzvizBleBatterySensor(coordinator, description)
        for description in BATTERY_SENSORS
        if description.exists_fn(coordinator.parts)
    )


class EzvizBleBatterySensor(EzvizBleEntity, SensorEntity):
    """Battery level of the lock or one of its parts."""

    entity_description: EzvizBleBatteryDescription
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: EzvizBleCoordinator, description: EzvizBleBatteryDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | None:
        """Return the battery percentage."""
        if (data := self.coordinator.data) is None:
            return None
        value = self.entity_description.value_fn(data)
        return value if value is not None and value <= 100 else None
