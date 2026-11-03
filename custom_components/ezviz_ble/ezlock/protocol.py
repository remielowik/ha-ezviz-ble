"""Frame encoding and decoding for the EZVIZ Bluetooth lock protocol.

Used by the DL01S, DL01 Pro and L2S lock families. Lock-layer frames are
not encrypted; the link is protected by BLE passkey bonding using the lock's 6 digit management
code, and the lock only accepts commands for user ids it knows.

Frame layout (request and response)::

    FC 00 | pid | len | main | sub | result | reserved | body ... | xor | FE

``len`` is the total frame length (body + 10) and ``xor`` is the XOR of every
byte from ``pid`` up to the end of the body. A response to ``(main, sub)``
carries ``(main, sub + 1)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
import hashlib
import struct

SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1930"
# Notifications from the lock. SendUserId is also written here.
NOTIFY_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d2b20"
# Every other request and the acknowledgements of pushed events.
WRITE_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d2b21"

FRAME_HEAD = b"\xfc\x00"
FRAME_TAIL = 0xFE
FRAME_OVERHEAD = 10
MAX_FRAME_LENGTH = 0xFF

USER_ID_FIELD_LENGTH = 32

Command = tuple[int, int]

CMD_SEND_USER_ID: Command = (0x01, 0xF9)
CMD_CONTROL_LOCK: Command = (0x41, 0x5F)
CMD_QUERY_DOOR_STATE: Command = (0x81, 0x01)
CMD_QUERY_BATTERY: Command = (0x81, 0x11)
CMD_QUERY_PARTS_INFO: Command = (0x81, 0x2B)

PUSH_DOOR_STATE_CHANGE: Command = (0x81, 0x15)
PUSH_DOORBELL: Command = (0x81, 0x1D)
PUSH_DOOR_OPEN_RECORD: Command = (0x81, 0x2D)
PUSH_LOW_POWER: Command = (0xC1, 0x01)
PUSH_WARNING: Command = (0xC1, 0x03)

# Pushes that are acknowledged with an empty frame carrying (main, sub + 1).
ACKNOWLEDGED_PUSHES: frozenset[Command] = frozenset(
    {PUSH_DOORBELL, PUSH_DOOR_OPEN_RECORD, PUSH_LOW_POWER, PUSH_WARNING}
)

RESULT_OK = 0
RESULT_USER_NOT_REGISTERED = 2
# ControlLock answers 6 in a case that counts as done (probably "already in
# the requested state"); re-read the state afterwards.
RESULT_CONTROL_DONE = 6

DOOR_STATE_BODY_LENGTH = 12
_STATE_FLAG_UNLOCKED = 0x02
_STATE_FLAG_NOT_CLOSED = 0x80

BATTERY_BODY_LENGTH = 4
PARTS_INFO_BODY_LENGTH = 4
PART_TYPE_DOOR_CONTACT = 3
_KEYPAD_PART_TYPES = frozenset({0, 1, 2, 4})


class FrameError(ValueError):
    """Raised for data that is not a valid lock-layer frame."""


class LockAction(IntEnum):
    """Action byte of the ControlLock command."""

    UNLOCK = 0
    LOCK = 1


class DoorStatus(Enum):
    """Door status derived from a door state query."""

    LOCKED = "locked"
    UNLOCKED = "unlocked"
    NOT_CLOSED = "not_closed"


class OpenMethod(IntEnum):
    """Unlock method reported in a door open record."""

    FINGERPRINT = 1
    PASSWORD = 2
    CARD = 3
    BLUETOOTH = 13
    TEMPORARY_PASSWORD = 255


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded lock-layer frame."""

    main: int
    sub: int
    result: int
    reserved: int
    body: bytes

    @property
    def command(self) -> Command:
        """Return the (main, sub) pair of this frame."""
        return (self.main, self.sub)


@dataclass(frozen=True, slots=True)
class LockState:
    """Result of a door state query."""

    status: DoorStatus
    battery: int
    flags: int
    door_sensor: int
    raw: bytes

    @property
    def door_open(self) -> bool | None:
        """Return whether the door is open, or None when it is not reported.

        The "not closed" flag is not meaningful when byte 10 is non-zero.
        """
        if self.door_sensor:
            return None
        return bool(self.flags & _STATE_FLAG_NOT_CLOSED)


@dataclass(frozen=True, slots=True)
class BatteryLevels:
    """Battery percentages of the lock and its parts."""

    lock: int
    keypad: int
    door_contact: int


@dataclass(frozen=True, slots=True)
class Part:
    """An accessory bound to the lock."""

    type: int
    status: int


@dataclass(frozen=True, slots=True)
class PartsInfo:
    """Accessories bound to the lock."""

    keypad: Part | None
    door_contact: Part | None


@dataclass(frozen=True, slots=True)
class DoorOpenRecord:
    """Pushed when the door was opened."""

    user_name: str
    timestamp: int
    user_index: int
    method: int


def response_command(command: Command) -> Command:
    """Return the command a response to ``command`` carries."""
    main, sub = command
    return (main, (sub + 1) & 0xFF)


def _xor(data: bytes) -> int:
    value = 0
    for byte in data:
        value ^= byte
    return value


def build_frame(command: Command, body: bytes = b"") -> bytes:
    """Encode a request frame."""
    main, sub = command
    length = len(body) + FRAME_OVERHEAD
    if length > MAX_FRAME_LENGTH:
        raise ValueError(f"body too long for a single frame: {len(body)} bytes")
    inner = bytes((0x00, length, main, sub, 0x00, 0x00)) + body
    return FRAME_HEAD + inner + bytes((_xor(inner), FRAME_TAIL))


def parse_frame(data: bytes) -> Frame:
    """Decode a frame received from the lock."""
    if len(data) < FRAME_OVERHEAD:
        raise FrameError(f"frame too short: {data.hex()}")
    if data[:2] != FRAME_HEAD or data[-1] != FRAME_TAIL:
        raise FrameError(f"not a lock-layer frame: {data.hex()}")
    if _xor(data[2:-2]) != data[-2]:
        raise FrameError(f"bad checksum: {data.hex()}")
    return Frame(
        main=data[4],
        sub=data[5],
        result=data[6],
        reserved=data[7],
        body=bytes(data[8:-2]),
    )


def lock_user_id(account: str) -> str:
    """Return the user id the lock knows an EZVIZ account by.

    That is the account e-mail (or phone number when there is no e-mail), or
    its MD5 hex digest when it is longer than 31 characters.
    """
    account = account.strip()
    if len(account) > 31:
        return hashlib.md5(account.encode(), usedforsecurity=False).hexdigest()
    return account


def _fixed_string(text: str, size: int = USER_ID_FIELD_LENGTH) -> bytes:
    return text.encode()[:size].ljust(size, b"\x00")


def send_user_id_body(user_id: str) -> bytes:
    """Body of SendUserId: user id (32 bytes) followed by 4 zero bytes."""
    return _fixed_string(user_id) + bytes(4)


def control_lock_body(action: LockAction, user_id: str) -> bytes:
    """Body of ControlLock: action, 3 zero bytes, user id (32 bytes)."""
    return bytes((action, 0, 0, 0)) + _fixed_string(user_id)


def parse_door_state(body: bytes) -> LockState:
    """Decode the body of a door state query response.

    Byte 6 holds the status flags, byte 7 the battery percentage and byte 10 tells
    whether the "not closed" flag should be ignored.
    """
    if len(body) != DOOR_STATE_BODY_LENGTH:
        raise FrameError(f"unexpected door state length: {body.hex()}")
    flags, battery, door_sensor = body[6], body[7], body[10]
    unlocked = bool(flags & _STATE_FLAG_UNLOCKED)
    if not door_sensor and flags & _STATE_FLAG_NOT_CLOSED:
        status = DoorStatus.NOT_CLOSED
    elif unlocked:
        status = DoorStatus.UNLOCKED
    else:
        status = DoorStatus.LOCKED
    return LockState(
        status=status,
        battery=battery,
        flags=flags,
        door_sensor=door_sensor,
        raw=bytes(body),
    )


def parse_battery(body: bytes) -> BatteryLevels:
    """Decode the battery query response: lock, keypad and door contact %."""
    if len(body) != BATTERY_BODY_LENGTH:
        raise FrameError(f"unexpected battery length: {body.hex()}")
    return BatteryLevels(lock=body[0], keypad=body[1], door_contact=body[2])


def parse_parts_info(body: bytes) -> PartsInfo:
    """Decode the parts info response.

    Two ``status, type`` slots; a slot is used when its status is non-zero.
    Type 3 is the door contact, the other known types are keypads.
    """
    if len(body) != PARTS_INFO_BODY_LENGTH:
        raise FrameError(f"unexpected parts info length: {body.hex()}")
    keypad = door_contact = None
    for status, part_type in ((body[0], body[1]), (body[2], body[3])):
        if not status:
            continue
        if part_type == PART_TYPE_DOOR_CONTACT:
            door_contact = Part(type=part_type, status=status)
        elif part_type in _KEYPAD_PART_TYPES:
            keypad = Part(type=part_type, status=status)
    return PartsInfo(keypad=keypad, door_contact=door_contact)


def parse_door_open_record(body: bytes) -> DoorOpenRecord:
    """Decode a door open record push."""
    if len(body) < 39:
        raise FrameError(f"door open record too short: {body.hex()}")
    timestamp, user_index, method = struct.unpack_from("<IHB", body, 32)
    return DoorOpenRecord(
        user_name=body[:32].split(b"\x00", 1)[0].decode(errors="replace"),
        timestamp=timestamp,
        user_index=user_index,
        method=method,
    )
