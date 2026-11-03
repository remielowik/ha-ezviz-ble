"""Tests for the BLE client with a fake bleak connection.

Run with pytest (needs bleak and bleak-retry-connector):
    pytest -o asyncio_mode=auto tests/test_client.py
"""

from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.backends.device import BLEDevice
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "ezviz_ble"))

from ezlock.client import EzLockClient, EzLockError  # noqa: E402
from ezlock.protocol import (  # noqa: E402
    NOTIFY_CHAR_UUID,
    WRITE_CHAR_UUID,
    build_frame,
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


async def test_send_user_id_does_not_wait_for_a_response(bleak_client) -> None:
    lock = EzLockClient(DEVICE, "me@example.com")
    await lock.send_user_id()
    bleak_client.write_gatt_char.assert_awaited_once_with(
        NOTIFY_CHAR_UUID,
        build_frame((0x01, 0xF9), send_user_id_body("me@example.com")),
        response=True,
    )


async def test_missing_response_raises_clear_error(bleak_client) -> None:
    lock = EzLockClient(DEVICE, "me@example.com")
    with pytest.raises(EzLockError, match="no response to 81/01 within 0.01 s"):
        await lock.request((0x81, 0x01), timeout=0.01)
    bleak_client.write_gatt_char.assert_awaited_once_with(
        WRITE_CHAR_UUID, build_frame((0x81, 0x01)), response=True
    )


async def test_push_does_not_answer_a_request(bleak_client) -> None:
    pushes = []
    lock = EzLockClient(DEVICE, "me@example.com", pushes.append)
    await lock.connect()
    on_notify = bleak_client.start_notify.await_args.args[1]
    # The door state push seen during the first hardware test.
    on_notify(None, bytearray.fromhex("fc00000e81150000080900009bfe"))
    assert [frame.command for frame in pushes] == [(0x81, 0x15)]
