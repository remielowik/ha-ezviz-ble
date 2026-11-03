"""Config flow for the EZVIZ Bluetooth integration."""

from __future__ import annotations

import logging
from typing import Any

from bleak.exc import BleakError
import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .const import (
    CONF_ACCOUNT,
    CONF_MANAGEMENT_CODE,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .coordinator import EzvizBleConfigEntry
from .ezlock.bind import (
    InvalidManagementCode,
    PairingFailed,
    PairingNotPossible,
    async_bind,
)
from .ezlock.client import EzLockError, EzLockUserNotRegistered
from .ezlock.protocol import SERVICE_UUID, FrameError, lock_user_id

_LOGGER = logging.getLogger(__name__)


class EzvizBleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Bind Home Assistant to an EZVIZ Bluetooth lock."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the lock and enter its management code and EZVIZ account."""
        errors: dict[str, str] = {}
        candidates = self._async_candidates()

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            account = user_input[CONF_ACCOUNT].strip()
            code = user_input[CONF_MANAGEMENT_CODE].strip()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            errors = await self._async_try_bind(address, account, code)
            if not errors:
                info = candidates.get(address)
                return self.async_create_entry(
                    title=info.name if info else address,
                    data={
                        CONF_ADDRESS: address,
                        CONF_ACCOUNT: account,
                        CONF_MANAGEMENT_CODE: code,
                    },
                )

        if not candidates:
            return self.async_abort(reason="no_devices_found")

        labels = {
            address: f"{info.name} ({address})" for address, info in candidates.items()
        }
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_ADDRESS): vol.In(labels),
                        vol.Required(CONF_MANAGEMENT_CODE): str,
                        vol.Required(CONF_ACCOUNT): str,
                    }
                ),
                user_input,
            ),
            errors=errors,
        )

    async def _async_try_bind(
        self, address: str, account: str, code: str
    ) -> dict[str, str]:
        """Bind to the lock and return form errors, if any."""
        if not (code.isdigit() and len(code) == 6):
            return {CONF_MANAGEMENT_CODE: "invalid_code_format"}
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, address, connectable=True
        )
        if ble_device is None:
            return {"base": "not_found"}
        try:
            await async_bind(ble_device, lock_user_id(account), code)
        except PairingNotPossible:
            return {"base": "pairing_not_possible"}
        except InvalidManagementCode:
            return {CONF_MANAGEMENT_CODE: "invalid_code"}
        except PairingFailed as err:
            _LOGGER.debug("Bonding failed: %s", err)
            return {"base": "pairing_failed"}
        except EzLockUserNotRegistered:
            return {CONF_ACCOUNT: "user_not_registered"}
        except (EzLockError, BleakError, FrameError, TimeoutError) as err:
            _LOGGER.debug("Binding failed: %s", err)
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error while binding")
            return {"base": "unknown"}
        return {}

    @callback
    def _async_candidates(self) -> dict[str, bluetooth.BluetoothServiceInfoBleak]:
        """Return connectable named devices, those with the lock service first."""
        configured = self._async_current_ids(include_ignore=False)
        infos = [
            info
            for info in bluetooth.async_discovered_service_info(
                self.hass, connectable=True
            )
            if info.address not in configured
            and info.name
            and info.name.replace("-", ":").upper() != info.address.upper()
        ]
        infos.sort(key=lambda info: (SERVICE_UUID not in info.service_uuids, info.name))
        return {info.address: info for info in infos}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: EzvizBleConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return EzvizBleOptionsFlow()


class EzvizBleOptionsFlow(OptionsFlow):
    """Polling options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the poll interval."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
                }
            ),
        )
