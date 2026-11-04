"""Async BLE client for EZVIZ Bluetooth locks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import logging

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .protocol import (
    ACKNOWLEDGED_PUSHES,
    CMD_CONTROL_LOCK,
    CMD_PIN,
    CMD_QUERY_BATTERY,
    CMD_QUERY_DOOR_STATE,
    CMD_QUERY_KEY,
    CMD_QUERY_KEY_LIST,
    CMD_QUERY_PARTS_INFO,
    CMD_SEND_USER_ID,
    CMD_SET_LOCK_TIME,
    KEY_LIST_PAGE_SIZE,
    KEY_TYPE_PIN,
    NOTIFY_CHAR_UUID,
    RESULT_CONTROL_DONE,
    RESULT_OK,
    RESULT_PIN_EXISTS,
    RESULT_PIN_NOT_FOUND,
    RESULT_PIN_STORAGE_FULL,
    RESULT_USER_NOT_REGISTERED,
    WRITE_CHAR_UUID,
    BatteryLevels,
    Command,
    Frame,
    FrameError,
    KeyDetail,
    LockAction,
    LockState,
    PartsInfo,
    PinEntry,
    PinOperation,
    ValidityWindow,
    build_frame,
    control_lock_body,
    key_detail_body,
    key_list_body,
    parse_battery,
    parse_door_state,
    parse_frame,
    parse_key_detail,
    parse_key_list,
    parse_parts_info,
    parse_pin_reply,
    pin_body,
    response_command,
    send_user_id_body,
    set_lock_time_body,
    validate_name,
    validate_pin,
)

_LOGGER = logging.getLogger(__name__)

# Most responses arrive within 4 s; ControlLock answers after the motor has
# moved.
DEFAULT_TIMEOUT = 4.0
CONTROL_TIMEOUT = 20.0
CONNECT_ATTEMPTS = 2

PushCallback = Callable[[Frame], None]


class EzLockError(Exception):
    """Base class for lock errors."""


class EzLockResponseError(EzLockError):
    """The lock answered with a non-success result code."""

    def __init__(self, command: Command, result: int) -> None:
        super().__init__(
            f"lock rejected command {command[0]:02X}/{command[1]:02X} "
            f"with result {result}"
        )
        self.command = command
        self.result = result


class EzLockUserNotRegistered(EzLockResponseError):
    """The user id is not registered on the lock."""


class EzLockPinExists(EzLockResponseError):
    """The lock already has this PIN."""


class EzLockPinNotFound(EzLockResponseError):
    """The lock has no such PIN."""


class EzLockPinStorageFull(EzLockResponseError):
    """The lock cannot store more PINs."""


_PIN_ERRORS: dict[int, type[EzLockResponseError]] = {
    RESULT_PIN_EXISTS: EzLockPinExists,
    RESULT_PIN_NOT_FOUND: EzLockPinNotFound,
    RESULT_PIN_STORAGE_FULL: EzLockPinStorageFull,
}

# Safety limit for paging through the key list (the lock holds 150 users).
_MAX_KEY_LIST_PAGES = 60

# Frames that carry a PIN in plaintext; keep them out of debug logs.
_REDACTED_COMMANDS: frozenset[Command] = frozenset(
    {CMD_PIN, response_command(CMD_QUERY_KEY)}
)


def _loggable(command: Command, frame: bytes | bytearray) -> str:
    if command in _REDACTED_COMMANDS:
        return f"{command[0]:02x}/{command[1]:02x} <{len(frame)} bytes, contains a PIN>"
    return frame.hex()


class EzLockClient:
    """Connection to one lock."""

    def __init__(
        self,
        ble_device: BLEDevice,
        user_id: str,
        push_callback: PushCallback | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._user_id = user_id
        self.push_callback = push_callback
        self._client: BleakClient | None = None
        self._connect_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._pending: dict[Command, asyncio.Future[Frame]] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

    @property
    def address(self) -> str:
        """Return the Bluetooth address of the lock."""
        return self._ble_device.address

    @property
    def is_connected(self) -> bool:
        """Return whether there is an open connection."""
        return self._client is not None and self._client.is_connected

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Use a newer BLEDevice for the next connection."""
        self._ble_device = ble_device

    async def connect(self) -> None:
        """Connect, subscribe to notifications and announce the user id.

        The lock only answers requests on a connection that started with
        the user id (SendUserId), so every connection sends it first.
        """
        async with self._connect_lock:
            if self.is_connected:
                return
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._ble_device.name or self._ble_device.address,
                disconnected_callback=self._on_disconnected,
                # Each attempt may make the lock ask for the passkey, and it
                # locks out after repeated failures: don't retry much.
                max_attempts=CONNECT_ATTEMPTS,
            )
            try:
                await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
                # No response frame follows; the acknowledged write is all.
                frame = build_frame(CMD_SEND_USER_ID, send_user_id_body(self._user_id))
                _LOGGER.debug("%s: write %s", self.address, frame.hex())
                await client.write_gatt_char(NOTIFY_CHAR_UUID, frame, response=True)
            except BaseException:
                await client.disconnect()
                raise
            self._client = client
            _LOGGER.debug("%s: connected", self.address)

    async def disconnect(self) -> None:
        """Close the connection."""
        async with self._connect_lock:
            client, self._client = self._client, None
            if client is not None and client.is_connected:
                await client.disconnect()
                _LOGGER.debug("%s: disconnected", self.address)

    async def request(
        self,
        command: Command,
        body: bytes = b"",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        char_uuid: str = WRITE_CHAR_UUID,
    ) -> Frame:
        """Send a request and wait for its response.

        Responses can only be matched by command, so requests are
        serialized.
        """
        await self.connect()
        assert self._client is not None
        async with self._request_lock:
            key = response_command(command)
            future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
            self._pending[key] = future
            frame = build_frame(command, body)
            _LOGGER.debug("%s: write %s", self.address, _loggable(command, frame))
            try:
                await self._client.write_gatt_char(char_uuid, frame, response=True)
                async with asyncio.timeout(timeout):
                    return await future
            except TimeoutError:
                raise EzLockError(
                    f"no response to {command[0]:02X}/{command[1]:02X} "
                    f"within {timeout:g} s"
                ) from None
            finally:
                self._pending.pop(key, None)

    async def write(
        self, command: Command, body: bytes = b"", *, char_uuid: str = WRITE_CHAR_UUID
    ) -> None:
        """Send a frame the lock does not answer; done once the write is acked."""
        await self.connect()
        assert self._client is not None
        frame = build_frame(command, body)
        _LOGGER.debug("%s: write %s", self.address, _loggable(command, frame))
        await self._client.write_gatt_char(char_uuid, frame, response=True)

    async def query_state(self) -> LockState:
        """Read lock, door and battery state."""
        frame = await self.request(CMD_QUERY_DOOR_STATE)
        if frame.result != RESULT_OK:
            raise EzLockResponseError(CMD_QUERY_DOOR_STATE, frame.result)
        return parse_door_state(frame.body)

    async def query_battery(self) -> BatteryLevels:
        """Read the battery levels of the lock, keypad and door contact."""
        frame = await self.request(CMD_QUERY_BATTERY)
        if frame.result != RESULT_OK:
            raise EzLockResponseError(CMD_QUERY_BATTERY, frame.result)
        return parse_battery(frame.body)

    async def query_parts_info(self) -> PartsInfo:
        """Read which accessories (keypad, door contact) are bound."""
        frame = await self.request(CMD_QUERY_PARTS_INFO)
        if frame.result != RESULT_OK:
            raise EzLockResponseError(CMD_QUERY_PARTS_INFO, frame.result)
        return parse_parts_info(frame.body)

    async def control(self, action: LockAction) -> None:
        """Lock or unlock."""
        frame = await self.request(
            CMD_CONTROL_LOCK,
            control_lock_body(action, self._user_id),
            timeout=CONTROL_TIMEOUT,
        )
        if frame.result == RESULT_USER_NOT_REGISTERED:
            raise EzLockUserNotRegistered(CMD_CONTROL_LOCK, frame.result)
        if frame.result not in (RESULT_OK, RESULT_CONTROL_DONE):
            raise EzLockResponseError(CMD_CONTROL_LOCK, frame.result)

    async def set_time(self, local_time: datetime) -> None:
        """Set the lock clock; PIN validity windows are checked against it.

        ``local_time`` is the current time in the lock's time zone.
        """
        frame = await self.request(CMD_SET_LOCK_TIME, set_lock_time_body(local_time))
        if frame.result != RESULT_OK:
            raise EzLockResponseError(CMD_SET_LOCK_TIME, frame.result)

    async def list_pins(self) -> list[PinEntry]:
        """Return the PINs stored on the lock (without the codes)."""
        pins: list[PinEntry] = []
        for _ in range(_MAX_KEY_LIST_PAGES):
            frame = await self.request(
                CMD_QUERY_KEY_LIST, key_list_body(KEY_TYPE_PIN, len(pins))
            )
            if frame.result != RESULT_OK:
                raise EzLockResponseError(CMD_QUERY_KEY_LIST, frame.result)
            last_page, entries = parse_key_list(frame.body, KEY_TYPE_PIN)
            pins.extend(entries)
            if last_page or not entries:
                return pins
        raise EzLockError("the key list did not end")

    async def read_key(self, user_index: int, key_type: int = KEY_TYPE_PIN) -> KeyDetail:
        """Return one key, including its PIN."""
        frame = await self.request(CMD_QUERY_KEY, key_detail_body(key_type, user_index))
        if frame.result != RESULT_OK:
            raise EzLockResponseError(CMD_QUERY_KEY, frame.result)
        return parse_key_detail(frame.body)

    async def add_pin(
        self, pin: str, name: str, window: ValidityWindow | None = None
    ) -> int:
        """Store a PIN as a new lock user; return that user's index."""
        validate_pin(pin)
        validate_name(name)
        return await self._pin_request(pin_body(PinOperation.ADD, pin, 0, name, window))

    async def delete_pin(self, user_index: int) -> None:
        """Delete a PIN and the user it belongs to.

        The lock identifies the PIN to delete by its value, so read it first.
        """
        key = await self.read_key(user_index)
        if key.key_type != KEY_TYPE_PIN or key.user_index != user_index or not key.pin:
            raise EzLockPinNotFound(CMD_PIN, RESULT_PIN_NOT_FOUND)
        await self._pin_request(
            pin_body(PinOperation.DELETE, key.pin, user_index, key.name)
        )

    async def _pin_request(self, body: bytes) -> int:
        frame = await self.request(CMD_PIN, body)
        if frame.result != RESULT_OK:
            raise _PIN_ERRORS.get(frame.result, EzLockResponseError)(
                CMD_PIN, frame.result
            )
        return parse_pin_reply(frame.body)

    def _on_disconnected(self, client: BleakClient) -> None:
        _LOGGER.debug("%s: connection lost", self.address)
        if client is self._client:
            self._client = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(EzLockError("disconnected"))

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        try:
            frame = parse_frame(bytes(data))
        except FrameError as err:
            _LOGGER.debug("%s: ignoring notification: %s", self.address, err)
            return
        _LOGGER.debug("%s: notify %s", self.address, _loggable(frame.command, data))
        future = self._pending.get(frame.command)
        if future is not None and not future.done():
            future.set_result(frame)
            return
        if frame.command in ACKNOWLEDGED_PUSHES:
            task = asyncio.create_task(self._acknowledge(frame.command))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        if self.push_callback is not None:
            self.push_callback(frame)

    async def _acknowledge(self, command: Command) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.write_gatt_char(
                WRITE_CHAR_UUID, build_frame(response_command(command)), response=True
            )
        # Best effort: the lock does not answer acknowledgements.
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: could not acknowledge push: %s", self.address, err)
