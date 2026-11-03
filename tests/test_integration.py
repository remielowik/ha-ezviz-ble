"""Home Assistant tests for the EZVIZ Bluetooth integration.

Run with pytest-homeassistant-custom-component installed:
    pytest -o asyncio_mode=auto tests/test_integration.py
"""

from unittest.mock import AsyncMock, patch

from bleak.backends.device import BLEDevice
import pytest

from homeassistant.components.lock import LockState as HALockState
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_ADDRESS, STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_ble.const import (
    CONF_ACCOUNT,
    CONF_MANAGEMENT_CODE,
    DOMAIN,
    EVENT_LOCK,
)
from custom_components.ezviz_ble.ezlock.bind import InvalidManagementCode
from custom_components.ezviz_ble.ezlock.protocol import (
    PUSH_DOOR_OPEN_RECORD,
    Frame,
    LockAction,
    parse_battery,
    parse_door_state,
    parse_parts_info,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
NAME = "Q12345678"
BLE_DEVICE = BLEDevice(ADDRESS, NAME, {})
PACKAGE = "custom_components.ezviz_ble"


def _state(flags: int = 0x00, battery: int = 80, sensor: int = 0):
    body = bytearray(12)
    body[6], body[7], body[10] = flags, battery, sensor
    return parse_door_state(bytes(body))


class _ServiceInfo:
    address = ADDRESS
    name = NAME
    service_uuids = ["00010203-0405-0607-0809-0a0b0c0d1930"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/ezviz_ble."""
    return


@pytest.fixture(autouse=True)
def skip_bluetooth_setup(hass: HomeAssistant) -> None:
    """Pretend the bluetooth stack is set up; the tests patch the calls we make."""
    hass.config.components.update({"bluetooth", "bluetooth_adapters"})


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=NAME,
        unique_id=ADDRESS,
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_ACCOUNT: "me@example.com",
            CONF_MANAGEMENT_CODE: "123456",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def client_mocks():
    """Patch the BLE client and HA bluetooth lookups used during setup."""
    with (
        patch(f"{PACKAGE}.bluetooth.async_ble_device_from_address", return_value=BLE_DEVICE),
        patch(f"{PACKAGE}.bluetooth.async_register_callback", return_value=lambda: None),
        patch(f"{PACKAGE}.coordinator.STATE_SETTLE_SECONDS", 0),
        patch(f"{PACKAGE}.coordinator.bluez", None),
        patch(f"{PACKAGE}.ezlock.client.EzLockClient.query_state", new_callable=AsyncMock) as query,
        patch(
            f"{PACKAGE}.ezlock.client.EzLockClient.query_battery",
            return_value=parse_battery(bytes((80, 65, 0, 0))),
        ),
        patch(
            f"{PACKAGE}.ezlock.client.EzLockClient.query_parts_info",
            # Slot 1: keypad (type 1), slot 2 unused: no door contact.
            return_value=parse_parts_info(bytes((1, 1, 0, 0))),
        ),
        patch(f"{PACKAGE}.ezlock.client.EzLockClient.control", new_callable=AsyncMock) as control,
        patch(f"{PACKAGE}.ezlock.client.EzLockClient.disconnect", new_callable=AsyncMock),
    ):
        query.return_value = _state()
        yield query, control


async def test_config_flow(hass: HomeAssistant) -> None:
    with (
        patch(f"{PACKAGE}.config_flow.bluetooth.async_discovered_service_info", return_value=[_ServiceInfo()]),
        patch(f"{PACKAGE}.config_flow.bluetooth.async_ble_device_from_address", return_value=BLE_DEVICE),
        patch(f"{PACKAGE}.config_flow.async_bind", return_value=_state()) as bind,
        patch(f"{PACKAGE}.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ADDRESS: ADDRESS, CONF_MANAGEMENT_CODE: "12345", CONF_ACCOUNT: "me@example.com"},
        )
        assert result["errors"] == {CONF_MANAGEMENT_CODE: "invalid_code_format"}

        bind.side_effect = InvalidManagementCode("rejected")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ADDRESS: ADDRESS, CONF_MANAGEMENT_CODE: "000000", CONF_ACCOUNT: "me@example.com"},
        )
        assert result["errors"] == {CONF_MANAGEMENT_CODE: "invalid_code"}

        bind.side_effect = None
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ADDRESS: ADDRESS, CONF_MANAGEMENT_CODE: "123456", CONF_ACCOUNT: " me@example.com "},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert result["data"] == {
        CONF_ADDRESS: ADDRESS,
        CONF_ACCOUNT: "me@example.com",
        CONF_MANAGEMENT_CODE: "123456",
    }
    bind.assert_awaited_with(BLE_DEVICE, "me@example.com", "123456")


async def test_entities_and_commands(hass: HomeAssistant, entry, client_mocks) -> None:
    query, control = client_mocks
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("lock.q12345678").state == HALockState.LOCKED
    assert hass.states.get("sensor.q12345678_battery").state == "80"
    assert hass.states.get("sensor.q12345678_keypad_battery").state == "65"
    assert hass.states.get("sensor.q12345678_door_contact_battery") is None
    assert hass.states.get("binary_sensor.q12345678_door").state == STATE_OFF

    query.return_value = _state(flags=0x02, sensor=1)
    await hass.services.async_call("lock", "unlock", {"entity_id": "lock.q12345678"}, blocking=True)
    await hass.async_block_till_done()
    control.assert_awaited_once_with(LockAction.UNLOCK)
    assert hass.states.get("lock.q12345678").state == HALockState.UNLOCKED
    assert hass.states.get("binary_sensor.q12345678_door").state == STATE_UNAVAILABLE

    query.return_value = _state(flags=0x82)
    await hass.services.async_call("lock", "lock", {"entity_id": "lock.q12345678"}, blocking=True)
    await hass.async_block_till_done()
    control.assert_awaited_with(LockAction.LOCK)
    assert hass.states.get("lock.q12345678").state == HALockState.OPEN

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_push_event(hass: HomeAssistant, entry, client_mocks) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    events = []
    hass.bus.async_listen(EVENT_LOCK, events.append)

    body = b"Alice".ljust(32, b"\x00") + (1234).to_bytes(4, "little") + (7).to_bytes(2, "little") + bytes((13,)) + bytes(13)
    entry.runtime_data.client.push_callback(Frame(*PUSH_DOOR_OPEN_RECORD, 0, 0, body))
    await hass.async_block_till_done()

    assert events[0].data == {
        "address": ADDRESS,
        "type": "door_opened",
        "user_name": "Alice",
        "user_index": 7,
        "method": "bluetooth",
    }
