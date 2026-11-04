"""Coordinator for the EZVIZ Bluetooth integration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from bleak.exc import BleakError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CLOCK_SYNC_HOURS,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    EVENT_LOCK,
    IDLE_DISCONNECT_SECONDS,
    STATE_SETTLE_SECONDS,
)
from .ezlock.client import (
    EzLockClient,
    EzLockError,
    EzLockPinExists,
    EzLockPinNotFound,
    EzLockPinStorageFull,
    EzLockUserNotRegistered,
)
from .ezlock.protocol import (
    PUSH_DOOR_OPEN_RECORD,
    PUSH_DOOR_STATE_CHANGE,
    PUSH_DOORBELL,
    PUSH_LOW_POWER,
    PUSH_WARNING,
    BatteryLevels,
    Frame,
    FrameError,
    LockAction,
    LockState,
    OpenMethod,
    PartsInfo,
    PinEntry,
    ValidityWindow,
    parse_door_open_record,
)

# dbus-fast (and BlueZ) only exist on Linux.
try:
    from .ezlock import bluez
except ImportError:
    bluez = None

_LOGGER = logging.getLogger(__name__)

_COMMUNICATION_ERRORS = (EzLockError, BleakError, FrameError, TimeoutError)

type EzvizBleConfigEntry = ConfigEntry[EzvizBleCoordinator]


@dataclass(frozen=True, slots=True)
class EzvizBleData:
    """Everything read from the lock in one poll."""

    state: LockState
    battery: BatteryLevels | None


class EzvizBleCoordinator(DataUpdateCoordinator[EzvizBleData]):
    """Polls the lock and runs commands over short-lived connections."""

    config_entry: EzvizBleConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: EzvizBleConfigEntry, client: EzLockClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=timedelta(
                minutes=entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
            ),
        )
        self.client = client
        self.parts: PartsInfo | None = None
        self.pending_action: LockAction | None = None
        self._operation_lock = asyncio.Lock()
        self._cancel_idle_disconnect: CALLBACK_TYPE | None = None
        self._clock_synced: datetime | None = None

    async def _async_update_data(self) -> EzvizBleData:
        try:
            return await self._async_run(self._async_read)
        except _COMMUNICATION_ERRORS as err:
            bluez_state = await self._async_bluez_state()
            hint = (
                "; if this keeps happening after setup, apply the one-time host"
                " step from the README (Bluetooth key fix)"
                if "bonded=True" in bluez_state
                else ""
            )
            raise UpdateFailed(
                f"Could not read the lock state: {err!r} (BlueZ: {bluez_state}){hint}"
            ) from err

    async def _async_bluez_state(self) -> str:
        if bluez is None:
            return "not available"
        try:
            return await bluez.async_describe_device(self.client.address)
        except Exception as err:  # noqa: BLE001
            return f"unknown ({err!r})"

    async def _async_read(self) -> EzvizBleData:
        state = await self.client.query_state()
        await self._async_sync_clock()
        # Parts and battery levels are extras: don't fail the poll on them.
        if self.parts is None:
            try:
                self.parts = await self.client.query_parts_info()
            except _COMMUNICATION_ERRORS as err:
                _LOGGER.debug("Could not read the parts info: %s", err)
        try:
            battery = await self.client.query_battery()
        except _COMMUNICATION_ERRORS as err:
            _LOGGER.debug("Could not read the battery levels: %s", err)
            battery = self.data.battery if self.data else None
        return EzvizBleData(state=state, battery=battery)

    async def async_control(self, action: LockAction) -> None:
        """Lock or unlock, then read back the state."""
        self.pending_action = action
        self.async_update_listeners()
        try:
            await self._async_run(lambda: self.client.control(action))
        except EzLockUserNotRegistered as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="user_not_registered"
            ) from err
        except _COMMUNICATION_ERRORS as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        finally:
            self.pending_action = None
            self.async_update_listeners()
        await asyncio.sleep(STATE_SETTLE_SECONDS)
        await self.async_refresh()

    async def _async_sync_clock(self, *, force: bool = False) -> None:
        """Set the lock clock to local time if it is due; never fails the caller."""
        now = dt_util.now()
        if (
            not force
            and self._clock_synced is not None
            and now - self._clock_synced < timedelta(hours=CLOCK_SYNC_HOURS)
        ):
            return
        try:
            await self.client.set_time(now)
        except _COMMUNICATION_ERRORS as err:
            _LOGGER.debug("Could not set the lock clock: %s", err)
            return
        self._clock_synced = now

    async def async_list_pins(self) -> list[PinEntry]:
        """Return the PINs stored on the lock."""
        return await self._async_pin_operation(self.client.list_pins)

    async def async_add_pin(
        self, pin: str, name: str, window: ValidityWindow | None
    ) -> int:
        """Store a PIN as a new lock user and return its user index."""

        async def _add() -> int:
            # Validity windows are checked against the lock clock.
            await self._async_sync_clock(force=True)
            return await self.client.add_pin(pin, name, window)

        return await self._async_pin_operation(_add)

    async def async_delete_pin(self, user_index: int) -> None:
        """Delete a PIN and its lock user."""
        await self._async_pin_operation(lambda: self.client.delete_pin(user_index))

    async def _async_pin_operation[_T](
        self, operation: Callable[[], Awaitable[_T]]
    ) -> _T:
        try:
            return await self._async_run(operation)
        except EzLockPinExists as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="pin_exists"
            ) from err
        except EzLockPinNotFound as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="pin_not_found"
            ) from err
        except EzLockPinStorageFull as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="pin_storage_full"
            ) from err
        except _COMMUNICATION_ERRORS as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_shutdown(self) -> None:
        """Cancel timers and close the connection."""
        await super().async_shutdown()
        self._async_cancel_idle_disconnect()
        await self.client.disconnect()

    async def _async_run[_T](self, operation: Callable[[], Awaitable[_T]]) -> _T:
        async with self._operation_lock:
            self._async_cancel_idle_disconnect()
            try:
                return await operation()
            finally:
                self._cancel_idle_disconnect = async_call_later(
                    self.hass, IDLE_DISCONNECT_SECONDS, self._async_idle_disconnect
                )

    async def _async_idle_disconnect(self, _now: datetime) -> None:
        self._cancel_idle_disconnect = None
        async with self._operation_lock:
            await self.client.disconnect()

    @callback
    def _async_cancel_idle_disconnect(self) -> None:
        if self._cancel_idle_disconnect is not None:
            self._cancel_idle_disconnect()
            self._cancel_idle_disconnect = None

    @callback
    def async_handle_push(self, frame: Frame) -> None:
        """Handle an event the lock pushed while connected."""
        if frame.command == PUSH_DOOR_STATE_CHANGE:
            # The push body layout is uncertain; re-read the state instead.
            self.config_entry.async_create_background_task(
                self.hass, self.async_request_refresh(), "ezviz_ble_push_refresh"
            )
            return
        data: dict[str, object] = {"address": self.client.address}
        if frame.command == PUSH_DOOR_OPEN_RECORD:
            try:
                record = parse_door_open_record(frame.body)
            except FrameError as err:
                _LOGGER.debug("Ignoring door open record: %s", err)
                return
            method = (
                OpenMethod(record.method).name.lower()
                if record.method in OpenMethod._value2member_map_
                else str(record.method)
            )
            data |= {
                "type": "door_opened",
                "user_name": record.user_name,
                "user_index": record.user_index,
                "method": method,
            }
        elif frame.command == PUSH_DOORBELL:
            data["type"] = "doorbell"
        elif frame.command == PUSH_LOW_POWER:
            data |= {"type": "low_battery", "body": frame.body.hex()}
        elif frame.command == PUSH_WARNING:
            data |= {"type": "alarm", "body": frame.body.hex()}
        else:
            _LOGGER.debug(
                "Unhandled push %02X/%02X: %s", frame.main, frame.sub, frame.body.hex()
            )
            return
        self.hass.bus.async_fire(EVENT_LOCK, data)
