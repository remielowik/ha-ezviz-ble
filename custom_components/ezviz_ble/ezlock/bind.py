"""One-time binding of a client (phone, Home Assistant) to a lock."""

from __future__ import annotations

import asyncio
import logging

from bleak.backends.device import BLEDevice

from .client import EzLockClient, EzLockError, EzLockUserNotRegistered
from .protocol import LockState

# dbus-fast (and BlueZ) only exist on Linux.
try:
    from . import bluez
except ImportError:
    bluez = None

_LOGGER = logging.getLogger(__name__)

# The lock advertises slowly; give a local adapter time to see it.
DEVICE_WAIT_SECONDS = 30.0
# Right after bonding the lock may not answer at once.
BOND_SETTLE_SECONDS = 1.0
STATE_ATTEMPTS_AFTER_BOND = 2


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
) -> LockState | None:
    """Announce the user id, bond with the management code and read the state.

    Connect, enable notifications, send the user id and bond using the
    management code as passkey. Commands are only accepted over a bonded
    link for a known user id, and the lock may drop an unbonded connection
    within seconds, so all of this has to happen in one go.

    Returns None if a new bond was made but the lock did not report its
    state: the bond already proves the user id and the management code.
    """
    client = EzLockClient(ble_device, user_id)
    try:
        if bluez is None:
            await client.connect()  # also sends the user id
            bonded_now = False
        else:
            bonded_now = await _async_bond(client, ble_device.address, int(management_code))
        if bonded_now:
            return await _async_state_after_bond(client)
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


async def _async_state_after_bond(client: EzLockClient) -> LockState | None:
    await asyncio.sleep(BOND_SETTLE_SECONDS)
    # Announce the user id again, now over the encrypted link.
    await client.send_user_id()
    for attempt in range(1, STATE_ATTEMPTS_AFTER_BOND + 1):
        try:
            return await client.query_state()
        except EzLockUserNotRegistered:
            raise
        except (EzLockError, TimeoutError) as err:
            _LOGGER.debug(
                "%s: no state after bonding (attempt %d): %s", client.address, attempt, err
            )
    _LOGGER.warning(
        "%s: bonded, but the lock did not report its state yet; it is read on the next poll",
        client.address,
    )
    return None


async def _async_bond(client: EzLockClient, address: str, passkey: int) -> bool:
    """Bond unless BlueZ already is; return whether a new bond was made."""
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
                return False
            _LOGGER.debug("%s: bonding with the management code", address)
            try:
                await agent.pair(device)
            except bluez.BluezAuthenticationFailed as err:
                raise InvalidManagementCode(repr(err)) from err
            except (bluez.BluezError, TimeoutError) as err:
                raise PairingFailed(repr(err)) from err
            _LOGGER.debug("%s: bonded", address)
            return True
    except bluez.BluezError as err:
        # Registering the agent or looking up the device failed.
        raise PairingNotPossible(f"BlueZ is not usable: {err!r}") from err
