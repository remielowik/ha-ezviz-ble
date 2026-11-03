"""Async BLE client for EZVIZ Bluetooth locks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .protocol import (
    ACKNOWLEDGED_PUSHES,
    CMD_CONTROL_LOCK,
    CMD_QUERY_BATTERY,
    CMD_QUERY_DOOR_STATE,
    CMD_QUERY_PARTS_INFO,
    CMD_SEND_USER_ID,
    NOTIFY_CHAR_UUID,
    RESULT_CONTROL_DONE,
    RESULT_OK,
    RESULT_USER_NOT_REGISTERED,
    WRITE_CHAR_UUID,
    BatteryLevels,
    Command,
    Frame,
    FrameError,
    LockAction,
    LockState,
    PartsInfo,
    build_frame,
    control_lock_body,
    parse_battery,
    parse_door_state,
    parse_frame,
    parse_parts_info,
    response_command,
    send_user_id_body,
)

_LOGGER = logging.getLogger(__name__)

# Most responses arrive within 4 s; ControlLock answers after the motor has
# moved.
DEFAULT_TIMEOUT = 4.0
CONTROL_TIMEOUT = 20.0

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
        """Connect and subscribe to notifications, if not connected yet."""
        async with self._connect_lock:
            if self.is_connected:
                return
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._ble_device.name or self._ble_device.address,
                disconnected_callback=self._on_disconnected,
            )
            try:
                await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
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
            _LOGGER.debug("%s: write %s", self.address, frame.hex())
            try:
                await self._client.write_gatt_char(char_uuid, frame, response=True)
                async with asyncio.timeout(timeout):
                    return await future
            finally:
                self._pending.pop(key, None)

    async def send_user_id(self) -> None:
        """Announce the user id; needed right before bonding."""
        frame = await self.request(
            CMD_SEND_USER_ID,
            send_user_id_body(self._user_id),
            char_uuid=NOTIFY_CHAR_UUID,
        )
        if frame.result != RESULT_OK:
            raise EzLockResponseError(CMD_SEND_USER_ID, frame.result)

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

    def _on_disconnected(self, client: BleakClient) -> None:
        _LOGGER.debug("%s: connection lost", self.address)
        if client is self._client:
            self._client = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(EzLockError("disconnected"))

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        _LOGGER.debug("%s: notify %s", self.address, data.hex())
        try:
            frame = parse_frame(bytes(data))
        except FrameError as err:
            _LOGGER.debug("%s: ignoring notification: %s", self.address, err)
            return
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
