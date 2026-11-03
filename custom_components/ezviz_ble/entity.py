"""Base entity for the EZVIZ Bluetooth integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import EzvizBleCoordinator
from .ezlock.protocol import LockState


class EzvizBleEntity(CoordinatorEntity[EzvizBleCoordinator]):
    """Entity attached to one lock."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EzvizBleCoordinator, key: str) -> None:
        super().__init__(coordinator)
        address = coordinator.client.address
        self._attr_unique_id = f"{address}_{key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer="EZVIZ",
            name=coordinator.config_entry.title,
            serial_number=coordinator.config_entry.title,
        )

    @property
    def lock_state(self) -> LockState | None:
        """Return the last read lock state."""
        data = self.coordinator.data
        return data.state if data else None
