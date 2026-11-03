"""Passkey bonding through BlueZ over D-Bus.

The lock asks for passkey entry when bonding; the passkey is its 6 digit
management code. bleak cannot answer passkey requests, so this module
registers its own org.bluez.Agent1.

The agent is registered as the default agent because the lock starts
pairing itself (SMP security request): right after it receives the user id,
and again on reconnects when BlueZ has no key it can use as central. It only
answers for the locks that hold an agent session and rejects every other
device. Sessions are reference counted, so several locks share one agent.
"""

# No "from __future__ import annotations": dbus-fast reads the D-Bus
# signatures from the annotations of the agent methods.

import asyncio
from collections.abc import AsyncIterator
import contextlib
from dataclasses import dataclass
import logging

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, method

_LOGGER = logging.getLogger(__name__)

BLUEZ_SERVICE = "org.bluez"
BLUEZ_ROOT = "/org/bluez"
DEVICE_INTERFACE = "org.bluez.Device1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
AGENT_PATH = "/org/homeassistant/ezviz_ble/agent"
# Same IO capability as Android phones, so the lock picks passkey entry.
AGENT_CAPABILITY = "KeyboardDisplay"

PAIR_TIMEOUT = 30.0
_POLL_INTERVAL = 0.5


class BluezError(Exception):
    """A BlueZ D-Bus call failed."""

    def __init__(self, name: str, message: str = "") -> None:
        super().__init__(f"{name}: {message}" if message else name)
        self.name = name


class BluezAuthenticationFailed(BluezError):
    """The lock rejected the passkey."""


@dataclass(frozen=True, slots=True)
class BluezDevice:
    """State of a device known to BlueZ."""

    path: str
    connected: bool
    paired: bool


def _address_from_path(path: str) -> str:
    """Return AA:BB:CC:DD:EE:FF for /org/bluez/hciN/dev_AA_BB_CC_DD_EE_FF."""
    return path.rsplit("/", 1)[-1].removeprefix("dev_").replace("_", ":").upper()


class _PasskeyAgent(ServiceInterface):
    def __init__(self, passkeys: dict[str, int]) -> None:
        super().__init__("org.bluez.Agent1")
        self._passkeys = passkeys

    def _passkey_for(self, device: str) -> int:
        passkey = self._passkeys.get(_address_from_path(device))
        if passkey is None:
            _LOGGER.debug("Rejecting agent request for unrelated device %s", device)
            raise DBusError("org.bluez.Error.Rejected", "not an EZVIZ lock in session")
        return passkey

    @method()
    def Release(self):  # noqa: N802
        _LOGGER.debug("Agent released")

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802, F821
        return f"{self._passkey_for(device):06d}"

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802, F821
        passkey = self._passkey_for(device)
        masked = f"{passkey:06d}"
        _LOGGER.debug(
            "RequestPasskey for %s: answering %d digit code ending in %s",
            device,
            len(masked),
            masked[-2:],
        )
        return passkey

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802, F821
        self._passkey_for(device)

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802, F821
        _LOGGER.debug("DisplayPasskey for %s (entered %s)", device, entered)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802, F821
        self._passkey_for(device)
        _LOGGER.debug("RequestConfirmation for %s", device)

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802, F821
        self._passkey_for(device)
        _LOGGER.debug("RequestAuthorization for %s", device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802, F821
        self._passkey_for(device)

    @method()
    def Cancel(self):  # noqa: N802
        _LOGGER.debug("Pairing canceled")


async def _call(
    bus: MessageBus,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: list | None = None,
) -> list:
    reply = await bus.call(
        Message(
            destination=BLUEZ_SERVICE,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body or [],
        )
    )
    assert reply is not None
    if reply.message_type == MessageType.ERROR:
        message = reply.body[0] if reply.body else ""
        if reply.error_name in (
            "org.bluez.Error.AuthenticationFailed",
            "org.bluez.Error.AuthenticationRejected",
        ):
            raise BluezAuthenticationFailed(reply.error_name, message)
        raise BluezError(reply.error_name or "unknown", message)
    return reply.body


async def _find_device(bus: MessageBus, address: str) -> tuple[str, dict] | None:
    (objects,) = await _call(bus, "/", OBJECT_MANAGER_INTERFACE, "GetManagedObjects")
    for path, interfaces in objects.items():
        device = interfaces.get(DEVICE_INTERFACE)
        if device and device["Address"].value.upper() == address.upper():
            return path, device
    return None


class AgentSession:
    """Access to BlueZ while the shared passkey agent is registered."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus

    async def get_device(self, address: str) -> BluezDevice | None:
        """Return the BlueZ state of a device, or None if BlueZ doesn't know it."""
        found = await _find_device(self._bus, address)
        if found is None:
            return None
        path, device = found
        return BluezDevice(
            path=path,
            connected=device["Connected"].value,
            paired=device["Paired"].value,
        )

    async def wait_for_device(self, address: str, timeout: float) -> BluezDevice | None:
        """Wait until a local adapter has seen the device advertise."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (device := await self.get_device(address)) is None:
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(_POLL_INTERVAL * 2)
        return device

    async def pair(self, device: BluezDevice, timeout: float = PAIR_TIMEOUT) -> None:
        """Bond with a connected device and mark it trusted."""
        try:
            async with asyncio.timeout(timeout):
                await _call(self._bus, device.path, DEVICE_INTERFACE, "Pair")
        except BluezAuthenticationFailed:
            raise
        except BluezError as err:
            # The lock may have started pairing itself (InProgress), and that
            # bond can still complete while our Pair() call fails.
            _LOGGER.debug("Pair() failed with %r, checking for a bond anyway", err)
            try:
                await self._wait_paired(device.path, timeout)
            except (BluezError, TimeoutError):
                raise err from None
        except TimeoutError:
            with contextlib.suppress(BluezError):
                await _call(self._bus, device.path, DEVICE_INTERFACE, "CancelPairing")
            raise
        else:
            await self._wait_paired(device.path, timeout)
        await _call(
            self._bus,
            device.path,
            PROPERTIES_INTERFACE,
            "Set",
            "ssv",
            [DEVICE_INTERFACE, "Trusted", Variant("b", True)],
        )

    async def _wait_paired(self, path: str, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            while True:
                (props,) = await _call(
                    self._bus, path, PROPERTIES_INTERFACE, "GetAll", "s", [DEVICE_INTERFACE]
                )
                if props["Paired"].value:
                    return
                if not props["Connected"].value:
                    raise BluezError(
                        "org.bluez.Error.AuthenticationCanceled",
                        "lock disconnected during pairing",
                    )
                await asyncio.sleep(_POLL_INTERVAL)


class _AgentRegistry:
    """One default agent shared by all lock sessions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._bus: MessageBus | None = None
        self._passkeys: dict[str, int] = {}
        self._sessions: dict[str, int] = {}

    async def acquire(self, address: str, passkey: int) -> MessageBus:
        address = address.upper()
        async with self._lock:
            if self._bus is None:
                self._bus = await self._register()
            self._passkeys[address] = passkey
            self._sessions[address] = self._sessions.get(address, 0) + 1
            return self._bus

    async def release(self, address: str) -> None:
        address = address.upper()
        async with self._lock:
            self._sessions[address] -= 1
            if not self._sessions[address]:
                del self._sessions[address]
                del self._passkeys[address]
            if not self._sessions and self._bus is not None:
                bus, self._bus = self._bus, None
                with contextlib.suppress(BluezError):
                    await _call(
                        bus, BLUEZ_ROOT, AGENT_MANAGER_INTERFACE, "UnregisterAgent", "o", [AGENT_PATH]
                    )
                bus.disconnect()
                _LOGGER.debug("Passkey agent unregistered")

    async def _register(self) -> MessageBus:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            bus.export(AGENT_PATH, _PasskeyAgent(self._passkeys))
            await _call(
                bus,
                BLUEZ_ROOT,
                AGENT_MANAGER_INTERFACE,
                "RegisterAgent",
                "os",
                [AGENT_PATH, AGENT_CAPABILITY],
            )
            await _call(
                bus, BLUEZ_ROOT, AGENT_MANAGER_INTERFACE, "RequestDefaultAgent", "o", [AGENT_PATH]
            )
        except BaseException:
            bus.disconnect()
            raise
        _LOGGER.debug("Passkey agent registered as default agent")
        return bus


_REGISTRY = _AgentRegistry()


@contextlib.asynccontextmanager
async def agent_session(address: str, passkey: int) -> AsyncIterator[AgentSession]:
    """Answer passkey requests from this lock while the context is open."""
    bus = await _REGISTRY.acquire(address, passkey)
    try:
        yield AgentSession(bus)
    finally:
        await _REGISTRY.release(address)


async def async_describe_device(address: str) -> str:
    """Return BlueZ's pairing state of a device, for diagnostics."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        found = await _find_device(bus, address)
    finally:
        bus.disconnect()
    if found is None:
        return "not known to BlueZ"
    path, device = found
    states = ", ".join(
        f"{key.lower()}={device[key].value}"
        for key in ("Connected", "Paired", "Bonded", "Trusted", "Blocked")
        if key in device
    )
    return f"{path}: {states}"
