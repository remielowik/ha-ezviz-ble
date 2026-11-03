"""The EZVIZ Bluetooth lock integration."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_ACCOUNT
from .coordinator import EzvizBleConfigEntry, EzvizBleCoordinator
from .ezlock.client import EzLockClient
from .ezlock.protocol import lock_user_id

PLATFORMS = [Platform.BINARY_SENSOR, Platform.LOCK, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: EzvizBleConfigEntry) -> bool:
    """Set up an EZVIZ Bluetooth lock."""
    address: str = entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if ble_device is None:
        raise ConfigEntryNotReady(f"Lock {address} is not in Bluetooth range")

    client = EzLockClient(ble_device, lock_user_id(entry.data[CONF_ACCOUNT]))
    coordinator = EzvizBleCoordinator(hass, entry, client)
    client.push_callback = coordinator.async_handle_push

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        client.set_ble_device(service_info.device)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            bluetooth.BluetoothCallbackMatcher(address=address, connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EzvizBleConfigEntry) -> bool:
    """Unload an EZVIZ Bluetooth lock."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: EzvizBleConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
