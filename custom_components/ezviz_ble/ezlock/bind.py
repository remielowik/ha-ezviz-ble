"""One-time binding of a client (phone, Home Assistant) to a lock."""

from __future__ import annotations

import logging

from bleak.backends.device import BLEDevice

from .client import EzLockClient, EzLockError
from .protocol import LockState

# dbus-fast (and BlueZ) only exist on Linux.
try:
    from . import bluez
except ImportError:
    bluez = None

_LOGGER = logging.getLogger(__name__)

# The lock advertises slowly; give a local adapter time to see it.
DEVICE_WAIT_SECONDS = 30.0


class PairingNotPossible(EzLockError):
    """The lock needs bonding, but it is not reachable through a local BlueZ adapter."""


class LockNotSeenLocally(PairingNotPossible):
    """No local Bluetooth adapter has heard the lock advertise yet."""


class PairingFailed(EzLockError):
    """BlueZ could not bond with the lock."""


class InvalidManagementCode(PairingFailed):
    """The lock rejected the management code as passkey."""


async def async_bind(
    ble_device: BLEDevice, user_id: str, management_code: str
) -> LockState:
    """Announce the user id, bond with the management code and read the state.

    Connect, enable notifications, send the user id and bond using the
    management code as passkey. Commands are only accepted over a bonded
    link for a known user id. The lock drops an unbonded connection after
    about 7 s, so all of this has to happen in one go.
    """
    client = EzLockClient(ble_device, user_id)
    try:
        if bluez is None:
            await client.connect()  # also sends the user id
        else:
            await _async_bond(client, ble_device.address, int(management_code))
        try:
            return await client.query_state()
        except (EzLockError, TimeoutError) as err:
            if bluez is None:
                raise PairingNotPossible(
                    "lock did not answer and cannot be bonded without a local adapter"
                ) from err
            raise
    finally:
        await client.disconnect()


async def _async_bond(client: EzLockClient, address: str, passkey: int) -> None:
    try:
        # Register the agent before sending the user id: the lock may start
        # pairing on its own as soon as it has received it.
        async with bluez.agent_session(address, passkey) as agent:
            if await agent.wait_for_device(address, DEVICE_WAIT_SECONDS) is None:
                raise LockNotSeenLocally(
                    f"no local Bluetooth adapter has seen {address} within "
                    f"{DEVICE_WAIT_SECONDS:g} s"
                )
            # Connecting sends the user id, after which the lock may start
            # pairing on its own.
            await client.connect()

            device = await agent.get_device(address)
            _LOGGER.debug("%s: BlueZ device state: %s", address, device)
            if device is None or not device.connected:
                raise PairingNotPossible("lock is not connected through a local adapter")
            if device.paired:
                return
            _LOGGER.debug("%s: bonding with the management code", address)
            try:
                await agent.pair(device)
            except bluez.BluezAuthenticationFailed as err:
                raise InvalidManagementCode(repr(err)) from err
            except (bluez.BluezError, TimeoutError) as err:
                raise PairingFailed(repr(err)) from err
            _LOGGER.debug("%s: bonded", address)
    except bluez.BluezError as err:
        # Registering the agent or looking up the device failed.
        raise PairingNotPossible(f"BlueZ is not usable: {err!r}") from err
