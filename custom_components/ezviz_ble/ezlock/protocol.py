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

import calendar
from dataclasses import dataclass, field
from datetime import datetime, time
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

CMD_SET_LOCK_TIME: Command = (0x01, 0x03)
CMD_SEND_USER_ID: Command = (0x01, 0xF9)
CMD_PIN: Command = (0x41, 0x59)
CMD_CONTROL_LOCK: Command = (0x41, 0x5F)
CMD_QUERY_KEY_LIST: Command = (0x41, 0x6B)
CMD_QUERY_KEY: Command = (0x41, 0x6D)
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

# PIN management.
KEY_TYPE_PIN = 1
PIN_MIN_LENGTH = 6
PIN_MAX_LENGTH = 10
NAME_FIELD_LENGTH = 32
# Users created for a PIN get this type; the lock only enforces a validity
# window for users with bit 0 set.
PIN_USER_TYPE = 1
RESULT_PIN_EXISTS = 3
RESULT_PIN_NOT_FOUND = 5
RESULT_PIN_STORAGE_FULL = 6
KEY_LIST_PAGE_SIZE = 3
_KEY_LIST_ENTRY_LENGTH = 36
KEY_DETAIL_BODY_LENGTH = 116
VALIDITY_WINDOW_LENGTH = 36
MAX_WEEKLY_PERIODS = 3

# The lock keeps local wall-clock time. Validity windows use it as Unix
# seconds; setting the clock uses it relative to this epoch.
LOCK_CLOCK_EPOCH = 946656000


class PinOperation(IntEnum):
    """Operation byte of the PIN command."""

    ADD = 1
    DELETE = 2
    MODIFY = 3


class PinStatus(IntEnum):
    """Validity of a PIN as reported in the key list."""

    NO_LIMIT = 0
    ACTIVE = 1
    EXPIRED = 2
    NOT_YET_VALID = 3


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


@dataclass(frozen=True, slots=True)
class WeeklyPeriod:
    """Weekdays and a time range in which a PIN works.

    ``weekdays`` uses 0 = Monday ... 6 = Sunday (Python's convention). The
    range may not cross midnight; ``end`` is exclusive.
    """

    weekdays: frozenset[int]
    start: time
    end: time


@dataclass(frozen=True, slots=True)
class ValidityWindow:
    """When a PIN works. ``None`` bounds and no periods mean no limit."""

    begin: datetime | None = None
    end: datetime | None = None
    periods: tuple[WeeklyPeriod, ...] = field(default_factory=tuple)

    @property
    def limited(self) -> bool:
        """Return whether the window restricts anything."""
        return self.begin is not None or self.end is not None or bool(self.periods)


@dataclass(frozen=True, slots=True)
class PinEntry:
    """A PIN in the lock's key list."""

    user_index: int
    status: PinStatus | int
    name: str


@dataclass(frozen=True, slots=True)
class KeyDetail:
    """One key (PIN, card or phone) as stored on the lock."""

    key_type: int
    user_type: int
    user_index: int
    name: str
    credential: bytes

    @property
    def pin(self) -> str:
        """Return the PIN of a PIN key."""
        return self.credential.split(b"\x00", 1)[0].decode(errors="replace")


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


def lock_clock_seconds(local_time: datetime) -> int:
    """Return a local wall-clock time as the lock counts it.

    The lock stores the local date and time fields as if they were UTC Unix
    seconds. ``local_time`` must already be in the lock's time zone.
    """
    return calendar.timegm(local_time.replace(tzinfo=None).timetuple())


def set_lock_time_body(local_time: datetime) -> bytes:
    """Body of the clock command: local time relative to the lock epoch."""
    return struct.pack("<I", lock_clock_seconds(local_time) - LOCK_CLOCK_EPOCH)


def validate_pin(pin: str) -> None:
    """Raise ValueError unless ``pin`` is 6 to 10 digits.

    The keypad also accepts an entry that merely contains a stored PIN and
    treats 5 digit entries differently, so shorter PINs are refused.
    """
    if not (pin.isdigit() and PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH):
        raise ValueError(f"a PIN is {PIN_MIN_LENGTH} to {PIN_MAX_LENGTH} digits")


def validate_name(name: str) -> None:
    """Raise ValueError unless ``name`` fits the lock's name field."""
    if not name.strip():
        raise ValueError("the name is empty")
    if len(name.encode()) > NAME_FIELD_LENGTH:
        raise ValueError(f"the name is longer than {NAME_FIELD_LENGTH} bytes")


def _period_body(period: WeeklyPeriod) -> bytes:
    if not period.weekdays or period.start >= period.end:
        raise ValueError("a weekly period needs weekdays and a start before its end")
    # The lock numbers days from Sunday (bit 0).
    mask = 0
    for day in period.weekdays:
        mask |= 1 << ((day + 1) % 7)
    return bytes(
        (1, 0xFF, 0xFF, mask, period.start.hour, period.start.minute,
         period.end.hour, period.end.minute)
    )


def validity_window_body(window: ValidityWindow | None) -> bytes:
    """Encode a validity window (36 bytes)."""
    if window is None or not window.limited:
        return bytes(VALIDITY_WINDOW_LENGTH)
    if len(window.periods) > MAX_WEEKLY_PERIODS:
        raise ValueError(f"at most {MAX_WEEKLY_PERIODS} weekly periods")
    begin = lock_clock_seconds(window.begin) if window.begin else 0
    end = lock_clock_seconds(window.end) if window.end else 0xFFFFFFFF
    if begin > end:
        raise ValueError("the window ends before it begins")
    periods = b"".join(_period_body(period) for period in window.periods)
    body = struct.pack("<BII", 1, begin, end) + bytes(3) + periods
    return body.ljust(VALIDITY_WINDOW_LENGTH, b"\x00")


def pin_body(
    operation: PinOperation,
    pin: str,
    user_index: int,
    name: str,
    window: ValidityWindow | None = None,
) -> bytes:
    """Body of the PIN command (84 bytes).

    operation, password index (always 0), PIN (10 bytes ASCII), 0, the PIN's
    user (u16 index, type, name[32]) and its validity window. Adding creates
    a new lock user and ignores ``user_index``; deleting sends no window.
    """
    window_bytes = (
        bytes(VALIDITY_WINDOW_LENGTH)
        if operation is PinOperation.DELETE
        else validity_window_body(window)
    )
    return (
        bytes((operation, 0))
        + _fixed_string(pin, PIN_MAX_LENGTH)
        + b"\x00"
        + struct.pack("<HB", user_index, PIN_USER_TYPE)
        + _fixed_string(name, NAME_FIELD_LENGTH)
        + window_bytes
    )


def parse_pin_reply(body: bytes) -> int:
    """Return the user index from a PIN command reply."""
    if len(body) < 2:
        raise FrameError(f"PIN reply too short: {body.hex()}")
    return struct.unpack_from("<H", body)[0]


def key_list_body(key_type: int, start: int, count: int = KEY_LIST_PAGE_SIZE) -> bytes:
    """Body of the key list query: type, number of keys to skip, page size."""
    return bytes((key_type, start, count)) + bytes(5)


def parse_key_list(body: bytes, key_type: int = KEY_TYPE_PIN) -> tuple[bool, list[PinEntry]]:
    """Decode a key list page: (last page?, entries of ``key_type``)."""
    if len(body) < 4 or (len(body) - 4) % _KEY_LIST_ENTRY_LENGTH:
        raise FrameError(f"unexpected key list length: {body.hex()}")
    entries = []
    for offset in range(4, len(body), _KEY_LIST_ENTRY_LENGTH):
        entry = body[offset : offset + _KEY_LIST_ENTRY_LENGTH]
        user_index = struct.unpack_from("<H", entry, 1)[0]
        if entry[0] != key_type or not user_index:
            continue
        status = entry[3]
        entries.append(
            PinEntry(
                user_index=user_index,
                status=PinStatus(status) if status in PinStatus._value2member_map_ else status,
                name=entry[4:36].split(b"\x00", 1)[0].decode(errors="replace"),
            )
        )
    return body[0] == 1, entries


def key_detail_body(key_type: int, user_index: int) -> bytes:
    """Body of the single key query (36 bytes)."""
    return struct.pack("<BHB", key_type, user_index, 0) + bytes(32)


def parse_key_detail(body: bytes) -> KeyDetail:
    """Decode the single key query reply (116 bytes)."""
    if len(body) != KEY_DETAIL_BODY_LENGTH:
        raise FrameError(f"unexpected key detail length: {body.hex()}")
    return KeyDetail(
        key_type=body[0],
        user_type=body[1],
        user_index=struct.unpack_from("<H", body, 2)[0],
        name=body[4:36].split(b"\x00", 1)[0].decode(errors="replace"),
        credential=bytes(body[68:78]),
    )


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
