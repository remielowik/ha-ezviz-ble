"""Tests for the lock frame encoding (stdlib only: python -m unittest)."""

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "ezviz_ble"))

from ezlock.protocol import (  # noqa: E402
    CMD_CONTROL_LOCK,
    CMD_QUERY_DOOR_STATE,
    DoorStatus,
    FrameError,
    LockAction,
    Part,
    build_frame,
    control_lock_body,
    lock_user_id,
    parse_battery,
    parse_door_open_record,
    parse_door_state,
    parse_frame,
    parse_parts_info,
    response_command,
    send_user_id_body,
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


if __name__ == "__main__":
    unittest.main()
