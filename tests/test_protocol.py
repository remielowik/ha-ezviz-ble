"""Tests for the lock frame encoding (stdlib only: python -m unittest)."""

import calendar
from datetime import datetime, time, timedelta, timezone
import hashlib
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "ezviz_ble"))

from ezlock.protocol import (  # noqa: E402
    CMD_CONTROL_LOCK,
    CMD_QUERY_DOOR_STATE,
    KEY_TYPE_PIN,
    DoorStatus,
    FrameError,
    LockAction,
    Part,
    PinEntry,
    PinOperation,
    PinStatus,
    ValidityWindow,
    WeeklyPeriod,
    build_frame,
    control_lock_body,
    key_detail_body,
    key_list_body,
    lock_clock_seconds,
    lock_user_id,
    parse_battery,
    parse_door_open_record,
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
    validity_window_body,
)


def _response(main: int, sub: int, result: int, body: bytes) -> bytes:
    inner = bytes((0x00, len(body) + 10, main, sub, result, 0x00)) + body
    xor = 0
    for byte in inner:
        xor ^= byte
    return b"\xfc\x00" + inner + bytes((xor, 0xFE))


def _state_body(flags: int, battery: int, sensor: int) -> bytes:
    body = bytearray(12)
    body[6], body[7], body[10] = flags, battery, sensor
    return bytes(body)


class FrameTest(unittest.TestCase):
    def test_query_frame(self) -> None:
        self.assertEqual(build_frame(CMD_QUERY_DOOR_STATE).hex(), "fc00000a810100008afe")

    def test_length_and_checksum_cover_body(self) -> None:
        frame = build_frame((0x41, 0x5F), b"\x01\x02")
        self.assertEqual(frame[3], len(frame))
        self.assertEqual(parse_frame(frame).body, b"\x01\x02")

    def test_parse_response(self) -> None:
        frame = parse_frame(_response(0x41, 0x60, 2, b""))
        self.assertEqual(frame.command, response_command(CMD_CONTROL_LOCK))
        self.assertEqual(frame.result, 2)

    def test_reject_bad_checksum(self) -> None:
        data = bytearray(_response(0x81, 0x02, 0, _state_body(0, 50, 0)))
        data[-2] ^= 0xFF
        corrupted = bytes(data)
        with self.assertRaises(FrameError):
            parse_frame(corrupted)

    def test_reject_auth_layer_frame(self) -> None:
        auth_frame = bytes.fromhex("aa55060000000220000028")
        with self.assertRaises(FrameError):
            parse_frame(auth_frame)

    def test_response_command_wraps(self) -> None:
        self.assertEqual(response_command((0x01, 0xFF)), (0x01, 0x00))


class BodyTest(unittest.TestCase):
    def test_control_body(self) -> None:
        body = control_lock_body(LockAction.UNLOCK, "me@example.com")
        self.assertEqual(len(body), 36)
        self.assertEqual(body[:4], b"\x00\x00\x00\x00")
        self.assertEqual(body[4:18], b"me@example.com")
        self.assertEqual(body[18:], bytes(18))
        self.assertEqual(control_lock_body(LockAction.LOCK, "x")[0], 1)

    def test_send_user_id_body(self) -> None:
        body = send_user_id_body("me@example.com")
        self.assertEqual(len(body), 36)
        self.assertEqual(body[:14], b"me@example.com")
        self.assertEqual(body[14:], bytes(22))

    def test_user_id_md5_for_long_accounts(self) -> None:
        self.assertEqual(lock_user_id(" me@example.com "), "me@example.com")
        self.assertEqual(lock_user_id("a" * 31), "a" * 31)
        self.assertEqual(lock_user_id("a" * 32), hashlib.md5(b"a" * 32).hexdigest())


class DoorStateTest(unittest.TestCase):
    def test_locked(self) -> None:
        state = parse_door_state(_state_body(0x00, 87, 0))
        self.assertIs(state.status, DoorStatus.LOCKED)
        self.assertEqual(state.battery, 87)
        self.assertFalse(state.door_open)

    def test_unlocked(self) -> None:
        self.assertIs(parse_door_state(_state_body(0x02, 50, 0)).status, DoorStatus.UNLOCKED)

    def test_not_closed_wins(self) -> None:
        state = parse_door_state(_state_body(0x82, 50, 0))
        self.assertIs(state.status, DoorStatus.NOT_CLOSED)
        self.assertTrue(state.door_open)

    def test_sensor_byte_ignores_not_closed(self) -> None:
        state = parse_door_state(_state_body(0x80, 50, 1))
        self.assertIs(state.status, DoorStatus.LOCKED)
        self.assertIsNone(state.door_open)
        self.assertIs(parse_door_state(_state_body(0x82, 50, 1)).status, DoorStatus.UNLOCKED)

    def test_wrong_length(self) -> None:
        short_body = bytes(11)
        with self.assertRaises(FrameError):
            parse_door_state(short_body)


class PartsTest(unittest.TestCase):
    def test_battery(self) -> None:
        battery = parse_battery(bytes((90, 40, 75, 0)))
        self.assertEqual((battery.lock, battery.keypad, battery.door_contact), (90, 40, 75))

    def test_parts_keypad_and_contact(self) -> None:
        parts = parse_parts_info(bytes((1, 2, 1, 3)))
        self.assertEqual(parts.keypad, Part(type=2, status=1))
        self.assertEqual(parts.door_contact, Part(type=3, status=1))

    def test_parts_unused_slots(self) -> None:
        parts = parse_parts_info(bytes((0, 3, 0, 1)))
        self.assertIsNone(parts.keypad)
        self.assertIsNone(parts.door_contact)

    def test_parts_wrong_length(self) -> None:
        short_body = bytes(3)
        with self.assertRaises(FrameError):
            parse_parts_info(short_body)


class PushTest(unittest.TestCase):
    def test_door_open_record(self) -> None:
        body = b"Alice".ljust(32, b"\x00") + (1234).to_bytes(4, "little") + (7).to_bytes(2, "little") + bytes((13,)) + bytes(13)
        record = parse_door_open_record(body)
        self.assertEqual((record.user_name, record.timestamp, record.user_index, record.method), ("Alice", 1234, 7, 13))



class PinTest(unittest.TestCase):
    def test_add_pin_body_layout(self) -> None:
        body = pin_body(PinOperation.ADD, "246813", 0, "test")
        self.assertEqual(len(body), 84)
        self.assertEqual(body[:2], b"\x01\x00")
        self.assertEqual(body[2:12], b"246813\x00\x00\x00\x00")
        self.assertEqual(body[12], 0)
        # The PIN's user: index (ignored when adding), type 1, name.
        self.assertEqual(body[13:16], b"\x00\x00\x01")
        self.assertEqual(body[16:48], b"test".ljust(32, b"\x00"))
        self.assertEqual(body[48:], bytes(36))

    def test_delete_pin_body_sends_no_window(self) -> None:
        window = ValidityWindow(begin=datetime(2026, 11, 4, 9, 0))
        body = pin_body(PinOperation.DELETE, "246813", 2, "test", window)
        self.assertEqual(body[:2], b"\x02\x00")
        self.assertEqual(body[13:16], b"\x02\x00\x01")
        self.assertEqual(body[48:], bytes(36))

    def test_key_list_page(self) -> None:
        # Reply seen on hardware after adding one PIN named "test".
        body = bytes.fromhex("01000000" + "01020000") + b"test".ljust(32, b"\x00")
        last, entries = parse_key_list(body)
        self.assertTrue(last)
        self.assertEqual(entries, [PinEntry(user_index=2, status=PinStatus.NO_LIMIT, name="test")])

    def test_key_list_skips_unused_entries(self) -> None:
        body = bytes.fromhex("01000000") + bytes(36)
        self.assertEqual(parse_key_list(body), (True, []))
        self.assertEqual(key_list_body(KEY_TYPE_PIN, 3), bytes((1, 3, 3, 0, 0, 0, 0, 0)))

    def test_key_detail(self) -> None:
        body = bytearray(116)
        body[0:4] = b"\x01\x01\x02\x00"
        body[4:8] = b"test"
        body[68:74] = b"246813"
        detail = parse_key_detail(bytes(body))
        self.assertEqual((detail.key_type, detail.user_index, detail.name, detail.pin), (1, 2, "test", "246813"))
        self.assertEqual(key_detail_body(KEY_TYPE_PIN, 2), b"\x01\x02\x00\x00" + bytes(32))
        with self.assertRaises(FrameError):
            parse_key_detail(bytes(10))

    def test_validity_window(self) -> None:
        begin, end = datetime(2026, 11, 4, 9, 0), datetime(2026, 11, 5, 17, 30)
        period = WeeklyPeriod(weekdays=frozenset({0, 6}), start=time(8, 0), end=time(12, 30))
        body = validity_window_body(ValidityWindow(begin, end, (period,)))
        self.assertEqual(len(body), 36)
        self.assertEqual(body[0], 1)
        self.assertEqual(body[1:9], struct.pack("<II", calendar.timegm(begin.timetuple()), calendar.timegm(end.timetuple())))
        self.assertEqual(body[9:12], bytes(3))
        # Monday is bit 1 and Sunday bit 0 on the lock.
        self.assertEqual(body[12:20], bytes((1, 0xFF, 0xFF, 0b11, 8, 0, 12, 30)))
        self.assertEqual(body[20:], bytes(16))
        self.assertEqual(validity_window_body(None), bytes(36))
        with self.assertRaises(ValueError):
            validity_window_body(ValidityWindow(end, begin))

    def test_open_ended_window(self) -> None:
        body = validity_window_body(ValidityWindow(end=datetime(2026, 11, 5)))
        self.assertEqual(struct.unpack_from("<BII", body), (1, 0, calendar.timegm(datetime(2026, 11, 5).timetuple())))

    def test_lock_time_ignores_the_time_zone(self) -> None:
        local = datetime(2026, 11, 4, 2, 54, 43, tzinfo=timezone(timedelta(hours=1)))
        naive_utc_seconds = calendar.timegm(datetime(2026, 11, 4, 2, 54, 43).timetuple())
        self.assertEqual(lock_clock_seconds(local), naive_utc_seconds)
        self.assertEqual(set_lock_time_body(local), struct.pack("<I", naive_utc_seconds - 946656000))

    def test_pin_validation(self) -> None:
        validate_pin("246813")
        validate_pin("1234567890")
        for bad in ("12345", "12345678901", "24681a", ""):
            with self.assertRaises(ValueError):
                validate_pin(bad)
        validate_name("Cleaner")
        for bad in ("", "  ", "x" * 33):
            with self.assertRaises(ValueError):
                validate_name(bad)

    def test_pin_reply(self) -> None:
        self.assertEqual(parse_pin_reply(bytes.fromhex("02000000")), 2)


if __name__ == "__main__":
    unittest.main()
