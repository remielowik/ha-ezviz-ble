"""Lock platform for the EZVIZ Bluetooth integration."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

import voluptuous as vol

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_END_TIME,
    ATTR_PIN,
    ATTR_PIN_NAME,
    ATTR_START_TIME,
    ATTR_USER_INDEX,
    ATTR_VALID_FROM,
    ATTR_VALID_UNTIL,
    ATTR_WEEKDAYS,
    DOMAIN,
    SERVICE_ADD_PIN,
    SERVICE_DELETE_PIN,
    SERVICE_LIST_PINS,
    WEEKDAYS,
)
from .coordinator import EzvizBleConfigEntry, EzvizBleCoordinator
from .ezlock.protocol import (
    NAME_FIELD_LENGTH,
    PIN_MAX_LENGTH,
    PIN_MIN_LENGTH,
    DoorStatus,
    LockAction,
    PinStatus,
    ValidityWindow,
    WeeklyPeriod,
    validate_name,
)
from .entity import EzvizBleEntity

PARALLEL_UPDATES = 1

ADD_PIN_SCHEMA: dict[vol.Marker, Any] = {
    vol.Required(ATTR_PIN): vol.All(
        cv.string, vol.Match(rf"^\d{{{PIN_MIN_LENGTH},{PIN_MAX_LENGTH}}}$")
    ),
    vol.Required(ATTR_PIN_NAME): vol.All(
        cv.string, vol.Length(min=1, max=NAME_FIELD_LENGTH)
    ),
    vol.Optional(ATTR_VALID_FROM): cv.datetime,
    vol.Optional(ATTR_VALID_UNTIL): cv.datetime,
    vol.Optional(ATTR_WEEKDAYS): vol.All(cv.ensure_list, [vol.In(WEEKDAYS)]),
    vol.Optional(ATTR_START_TIME): cv.time,
    vol.Optional(ATTR_END_TIME): cv.time,
}
DELETE_PIN_SCHEMA: dict[vol.Marker, Any] = {
    vol.Required(ATTR_USER_INDEX): vol.All(vol.Coerce(int), vol.Range(min=1)),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizBleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the lock entity and its PIN services."""
    async_add_entities([EzvizBleLock(entry.runtime_data)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_LIST_PINS,
        None,
        "async_list_pins",
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        SERVICE_ADD_PIN,
        ADD_PIN_SCHEMA,
        "async_add_pin",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        SERVICE_DELETE_PIN, DELETE_PIN_SCHEMA, "async_delete_pin"
    )


def _validity_window(
    valid_from: datetime | None,
    valid_until: datetime | None,
    weekdays: list[str] | None,
    start_time: time | None,
    end_time: time | None,
) -> ValidityWindow | None:
    """Build the PIN's validity window from service fields (local time)."""
    begin = dt_util.as_local(valid_from) if valid_from else None
    end = dt_util.as_local(valid_until) if valid_until else None
    if begin and end and end <= begin:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_window"
        )
    schedule = (weekdays, start_time, end_time)
    if any(part is not None for part in schedule):
        if not weekdays or start_time is None or end_time is None or end_time <= start_time:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_schedule"
            )
        periods = (
            WeeklyPeriod(
                weekdays=frozenset(WEEKDAYS.index(day) for day in weekdays),
                start=start_time,
                end=end_time,
            ),
        )
    else:
        periods = ()
    window = ValidityWindow(begin=begin, end=end, periods=periods)
    return window if window.limited else None


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

    async def async_list_pins(self) -> ServiceResponse:
        """List the PINs on the lock (names and validity, never the codes)."""
        pins = await self.coordinator.async_list_pins()
        return {
            "pins": [
                {
                    "user_index": pin.user_index,
                    "name": pin.name,
                    "status": (
                        pin.status.name.lower()
                        if isinstance(pin.status, PinStatus)
                        else str(pin.status)
                    ),
                }
                for pin in pins
            ]
        }

    async def async_add_pin(
        self,
        pin: str,
        name: str,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        weekdays: list[str] | None = None,
        start_time: time | None = None,
        end_time: time | None = None,
    ) -> ServiceResponse:
        """Add a keypad PIN; each PIN becomes its own lock user."""
        try:
            validate_name(name)
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_name"
            ) from err
        window = _validity_window(valid_from, valid_until, weekdays, start_time, end_time)
        user_index = await self.coordinator.async_add_pin(pin, name, window)
        return {"user_index": user_index}

    async def async_delete_pin(self, user_index: int) -> None:
        """Delete a keypad PIN by its user index."""
        await self.coordinator.async_delete_pin(user_index)
