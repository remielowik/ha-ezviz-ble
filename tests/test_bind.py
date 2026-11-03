"""Tests for the bind flow with fake BlueZ and BLE clients.

Run with pytest (needs bleak, bleak-retry-connector and dbus-fast):
    pytest -o asyncio_mode=auto tests/test_bind.py
"""

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bleak.backends.device import BLEDevice
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "ezviz_ble"))

from ezlock import bind  # noqa: E402
from ezlock.bluez import BluezAuthenticationFailed, BluezDevice, BluezError  # noqa: E402

ADDRESS = "AA:BB:CC:DD:EE:FF"
DEVICE = BLEDevice(ADDRESS, "DL01_TEST00001", {})
PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
ACCOUNT = "me@example.com"
CODE = "123456"


class FakeAgent:
    """Records the order of BlueZ and BLE operations."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.seen = True
        self.paired = False
        self.pair_error: Exception | None = None

    async def __aenter__(self):
        self.calls.append("agent registered")
        return self

    async def __aexit__(self, *exc_info):
        self.calls.append("agent unregistered")

    async def wait_for_device(self, address, timeout):
        return BluezDevice(PATH, connected=False, paired=self.paired) if self.seen else None

    async def get_device(self, address):
        return BluezDevice(PATH, connected=True, paired=self.paired)

    async def pair(self, device):
        self.calls.append("pair")
        if self.pair_error:
            raise self.pair_error


@pytest.fixture
def calls():
    return []


@pytest.fixture
def agent(calls):
    return FakeAgent(calls)


@pytest.fixture(autouse=True)
def fakes(calls, agent):
    fake_bluez = SimpleNamespace(
        PairingAgent=lambda passkey: agent,
        BluezError=BluezError,
        BluezAuthenticationFailed=BluezAuthenticationFailed,
    )

    async def send_user_id(self):
        calls.append("send user id")

    with (
        patch.object(bind, "bluez", fake_bluez),
        patch("ezlock.bind.EzLockClient.connect", AsyncMock()),
        patch("ezlock.bind.EzLockClient.disconnect", AsyncMock()),
        patch("ezlock.bind.EzLockClient.send_user_id", send_user_id),
        patch("ezlock.bind.EzLockClient.query_state", AsyncMock(return_value="state")),
    ):
        yield


async def test_agent_is_registered_before_the_user_id_is_sent(calls) -> None:
    assert await bind.async_bind(DEVICE, ACCOUNT, CODE) == "state"
    assert calls == ["agent registered", "send user id", "pair", "agent unregistered"]


async def test_not_seen_by_a_local_adapter(agent, calls) -> None:
    agent.seen = False
    with pytest.raises(bind.LockNotSeenLocally):
        await bind.async_bind(DEVICE, ACCOUNT, CODE)
    assert "send user id" not in calls


async def test_wrong_management_code(agent) -> None:
    agent.pair_error = BluezAuthenticationFailed("org.bluez.Error.AuthenticationFailed")
    with pytest.raises(bind.InvalidManagementCode):
        await bind.async_bind(DEVICE, ACCOUNT, CODE)


async def test_lock_drops_link_during_pairing(agent) -> None:
    agent.pair_error = BluezError("org.bluez.Error.AuthenticationCanceled")
    with pytest.raises(bind.PairingFailed, match="AuthenticationCanceled"):
        await bind.async_bind(DEVICE, ACCOUNT, CODE)


async def test_already_bonded_skips_pairing(agent, calls) -> None:
    agent.paired = True
    await bind.async_bind(DEVICE, ACCOUNT, CODE)
    assert "pair" not in calls
