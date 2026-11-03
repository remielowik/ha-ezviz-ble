"""One-time binding of a client (phone, Home Assistant) to a lock."""

from __future__ import annotations

import logging

from bleak.backends.device import BLEDevice

from .client import EzLockClient, EzLockError, EzLockResponseError
from .protocol import LockState

_LOGGER = logging.getLogger(__name__)


class PairingNotPossible(EzLockError):
    """The lock needs bonding, but it is not reachable through a local BlueZ adapter."""


class PairingFailed(EzLockError):
    """BlueZ could not bond with the lock."""


class InvalidManagementCode(PairingFailed):
    """The lock rejected the management code as passkey."""


async def async_bind(
    ble_device: BLEDevice, user_id: str, management_code: str
) -> LockState:
    """Announce the user id, bond with the management code and read the state.

    Connect, enable notifications, send the user id and then bond using the
    management code as passkey. Commands are only accepted over a bonded
    link for a known user id.
    """
    client = EzLockClient(ble_device, user_id)
    try:
        await client.connect()
        try:
            await client.send_user_id()
        except EzLockResponseError as err:
            # Usually fatal, but the bond may already exist: log and go on.
            _LOGGER.warning("%s: lock rejected the user id: %s", client.address, err)

        # dbus-fast (and BlueZ) only exist on Linux.
        try:
            from . import bluez  # noqa: PLC0415
        except ImportError:
            bluez = None

        device = await bluez.async_get_device(ble_device.address) if bluez else None
        if device is not None and not device.paired:
            if not device.connected:
                # Our connection runs through a proxy; BlueZ cannot bond it.
                raise PairingNotPossible("lock is not connected through a local adapter")
            try:
                await bluez.async_pair(ble_device.address, int(management_code))
            except bluez.BluezAuthenticationFailed as err:
                raise InvalidManagementCode(str(err)) from err
            except bluez.BluezError as err:
                raise PairingFailed(str(err)) from err

        try:
            return await client.query_state()
        except (EzLockError, TimeoutError) as err:
            if device is None:
                raise PairingNotPossible(
                    "lock did not answer and cannot be bonded without a local adapter"
                ) from err
            raise
    finally:
        await client.disconnect()
