"""Tests for the shared BlueZ passkey agent (no D-Bus needed).

Run with pytest (needs dbus-fast):
    pytest -o asyncio_mode=auto tests/test_bluez.py
"""

from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from dbus_fast.errors import DBusError
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "ezviz_ble"))

from ezlock import bluez  # noqa: E402

LOCK = "AA:BB:CC:DD:EE:FF"
LOCK_PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
OTHER_PATH = "/org/bluez/hci0/dev_11_22_33_44_55_66"


def test_address_from_path() -> None:
    assert bluez._address_from_path(LOCK_PATH) == LOCK


def test_agent_only_answers_for_locks_in_session() -> None:
    agent = bluez._PasskeyAgent({LOCK: 12345})
    assert agent._passkey_for(LOCK_PATH) == 12345
    with pytest.raises(DBusError):
        agent._passkey_for(OTHER_PATH)


async def test_sessions_share_one_agent() -> None:
    registry = bluez._AgentRegistry()
    bus = MagicMock()
    with (
        patch.object(registry, "_register", AsyncMock(return_value=bus)) as register,
        patch.object(bluez, "_call", AsyncMock()) as call,
    ):
        assert await registry.acquire(LOCK, 12345) is bus
        await registry.acquire("11:22:33:44:55:66", 54321)
        register.assert_awaited_once()
        await registry.release(LOCK)
        assert LOCK not in registry._passkeys
        bus.disconnect.assert_not_called()
        await registry.release("11:22:33:44:55:66")
    bus.disconnect.assert_called_once()
    assert call.await_args.args[3] == "UnregisterAgent"
