"""Tests for the BLE client with a fake bleak connection.

Run with pytest (needs bleak and bleak-retry-connector):
    pytest -o asyncio_mode=auto tests/test_client.py
"""

import logging
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, call, patch

from bleak.backends.device import BLEDevice
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "ezviz_ble"))

from ezlock.client import EzLockClient, EzLockError, EzLockPinExists  # noqa: E402
from ezlock.protocol import (  # noqa: E402
    CMD_PIN,
    CMD_QUERY_KEY,
    CMD_QUERY_KEY_LIST,
    NOTIFY_CHAR_UUID,
    WRITE_CHAR_UUID,
    build_frame,
    parse_frame,
    response_command,
    send_user_id_body,
)

DEVICE = BLEDevice("AA:BB:CC:DD:EE:FF", "DL01_TEST00001", {})


@pytest.fixture
def bleak_client():
    """A connected fake BleakClient returned by establish_connection."""
    client = MagicMock()
    client.is_connected = True
    client.start_notify = AsyncMock()
    client.write_gatt_char = AsyncMock()
    client.disconnect = AsyncMock()
    with patch("ezlock.client.establish_connection", AsyncMock(return_value=client)):
        yield client


USER_ID_WRITE = call(
    NOTIFY_CHAR_UUID,
    build_frame((0x01, 0xF9), send_user_id_body("me@example.com")),
    response=True,
)


async def test_connect_announces_user_id_without_waiting_for_a_response(bleak_client) -> None:
    lock = EzLockClient(DEVICE, "me@example.com")
    await lock.connect()
    assert bleak_client.write_gatt_char.await_args_list == [USER_ID_WRITE]


async def test_user_id_is_sent_once_per_connection(bleak_client) -> None:
    lock = EzLockClient(DEVICE, "me@example.com")
    with pytest.raises(EzLockError):
        await lock.request((0x81, 0x01), timeout=0.01)
    with pytest.raises(EzLockError):
        await lock.request((0x81, 0x11), timeout=0.01)
    assert bleak_client.write_gatt_char.await_args_list[0] == USER_ID_WRITE
    assert bleak_client.write_gatt_char.await_args_list.count(USER_ID_WRITE) == 1


async def test_missing_response_raises_clear_error(bleak_client) -> None:
    lock = EzLockClient(DEVICE, "me@example.com")
    with pytest.raises(EzLockError, match="no response to 81/01 within 0.01 s"):
        await lock.request((0x81, 0x01), timeout=0.01)
    assert bleak_client.write_gatt_char.await_args_list == [
        USER_ID_WRITE,
        call(WRITE_CHAR_UUID, build_frame((0x81, 0x01)), response=True),
    ]


async def test_failed_user_id_write_closes_the_connection(bleak_client) -> None:
    bleak_client.write_gatt_char.side_effect = OSError("write failed")
    lock = EzLockClient(DEVICE, "me@example.com")
    with pytest.raises(OSError):
        await lock.connect()
    bleak_client.disconnect.assert_awaited_once()
    assert not lock.is_connected


async def test_push_does_not_answer_a_request(bleak_client) -> None:
    pushes = []
    lock = EzLockClient(DEVICE, "me@example.com", pushes.append)
    await lock.connect()
    on_notify = bleak_client.start_notify.await_args.args[1]
    # The door state push seen during the first hardware test.
    on_notify(None, bytearray.fromhex("fc00000e81150000080900009bfe"))
    assert [frame.command for frame in pushes] == [(0x81, 0x15)]


def _reply(command, result: int = 0, body: bytes = b"") -> bytearray:
    main, sub = response_command(command)
    inner = bytes((0x00, len(body) + 10, main, sub, result, 0x00)) + body
    xor = 0
    for byte in inner:
        xor ^= byte
    return bytearray(b"\xfc\x00" + inner + bytes((xor, 0xFE)))


def _answer(bleak_client, replies: dict) -> list:
    """Let the fake lock answer requests from ``replies[command]`` in order."""
    requests = []

    async def write(uuid, data, response=True):
        if uuid != WRITE_CHAR_UUID:
            return
        frame = parse_frame(bytes(data))
        requests.append(frame)
        result, body = replies[frame.command].pop(0)
        on_notify = bleak_client.start_notify.await_args.args[1]
        on_notify(None, _reply(frame.command, result, body))

    bleak_client.write_gatt_char.side_effect = write
    return requests


def _entry(index: int, name: str) -> bytes:
    return bytes((1,)) + index.to_bytes(2, "little") + b"\x00" + name.encode().ljust(32, b"\x00")


async def test_list_pins_pages_until_the_last_page(bleak_client) -> None:
    requests = _answer(bleak_client, {
        CMD_QUERY_KEY_LIST: [
            (0, b"\x00\x00\x00\x00" + _entry(2, "a") + _entry(3, "b") + _entry(4, "c")),
            (0, b"\x01\x00\x00\x00" + _entry(5, "d") + bytes(36)),
        ],
    })
    lock = EzLockClient(DEVICE, "me@example.com")
    pins = await lock.list_pins()
    assert [(pin.user_index, pin.name) for pin in pins] == [(2, "a"), (3, "b"), (4, "c"), (5, "d")]
    assert [request.body[1] for request in requests] == [0, 3]


async def test_delete_pin_sends_the_pin_it_read(bleak_client) -> None:
    detail = bytearray(116)
    detail[0:4] = b"\x01\x01\x02\x00"
    detail[4:8] = b"test"
    detail[68:74] = b"246813"
    requests = _answer(bleak_client, {
        CMD_QUERY_KEY: [(0, bytes(detail))],
        CMD_PIN: [(0, b"\x02\x00\x00\x00")],
    })
    lock = EzLockClient(DEVICE, "me@example.com")
    await lock.delete_pin(2)
    delete = requests[-1]
    assert delete.command == CMD_PIN
    assert delete.body[:12] == b"\x02\x00246813\x00\x00\x00\x00"
    assert delete.body[13:16] == b"\x02\x00\x01"


async def test_add_pin_reports_an_existing_pin(bleak_client) -> None:
    _answer(bleak_client, {CMD_PIN: [(3, b"\x00\x00\x00\x00")]})
    lock = EzLockClient(DEVICE, "me@example.com")
    with pytest.raises(EzLockPinExists):
        await lock.add_pin("246813", "test")


async def test_add_pin_rejects_short_pins_without_asking_the_lock(bleak_client) -> None:
    lock = EzLockClient(DEVICE, "me@example.com")
    with pytest.raises(ValueError):
        await lock.add_pin("12345", "test")
    bleak_client.write_gatt_char.assert_not_awaited()


async def test_pins_are_not_logged(bleak_client, caplog) -> None:
    _answer(bleak_client, {CMD_PIN: [(0, b"\x02\x00\x00\x00")]})
    lock = EzLockClient(DEVICE, "me@example.com")
    with caplog.at_level(logging.DEBUG, logger="ezlock.client"):
        assert await lock.add_pin("246813", "test") == 2
    assert b"246813".hex() not in caplog.text
    assert "contains a PIN" in caplog.text
