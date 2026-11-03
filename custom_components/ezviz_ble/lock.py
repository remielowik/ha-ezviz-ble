"""Lock platform for the EZVIZ Bluetooth integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EzvizBleConfigEntry, EzvizBleCoordinator
from .ezlock.protocol import DoorStatus, LockAction
from .entity import EzvizBleEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizBleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the lock entity."""
    async_add_entities([EzvizBleLock(entry.runtime_data)])


class EzvizBleLock(EzvizBleEntity, LockEntity):
    """An EZVIZ Bluetooth lock."""

    _attr_name = None

    def __init__(self, coordinator: EzvizBleCoordinator) -> None:
        super().__init__(coordinator, "lock")

    @property
    def is_locked(self) -> bool | None:
        """Return whether the bolt is locked."""
        if (state := self.lock_state) is None:
            return None
        return state.status is DoorStatus.LOCKED

    @property
    def is_open(self) -> bool | None:
        """Return whether the lock reports the door as not closed."""
        if (state := self.lock_state) is None:
            return None
        return state.status is DoorStatus.NOT_CLOSED

    @property
    def is_locking(self) -> bool:
        """Return whether a lock command is running."""
        return self.coordinator.pending_action is LockAction.LOCK

    @property
    def is_unlocking(self) -> bool:
        """Return whether an unlock command is running."""
        return self.coordinator.pending_action is LockAction.UNLOCK

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the raw state bytes; their meaning is partly unverified."""
        if (state := self.lock_state) is None:
            return None
        return {
            "state_flags": f"0x{state.flags:02x}",
            "door_sensor_byte": state.door_sensor,
            "raw_state": state.raw.hex(),
        }

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the door."""
        await self.coordinator.async_control(LockAction.LOCK)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the door."""
        await self.coordinator.async_control(LockAction.UNLOCK)
