"""Passkey bonding through BlueZ over D-Bus.

The lock asks for passkey entry when bonding; the passkey is its 6 digit
management code. bleak cannot answer passkey requests, so this module
registers its own org.bluez.Agent1 while binding. It is registered as the
default agent because the lock may start the pairing itself (SMP security
request) right after it receives the user id, before Pair() is called.
"""

# No "from __future__ import annotations": dbus-fast reads the D-Bus
# signatures from the annotations of the agent methods.

import asyncio
import contextlib
from dataclasses import dataclass
import logging
from typing import Self

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
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


class _PasskeyAgent(ServiceInterface):
    def __init__(self, passkey: int) -> None:
        super().__init__("org.bluez.Agent1")
        self._passkey = passkey

    @method()
    def Release(self):  # noqa: N802
        _LOGGER.debug("Agent released")

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802, F821
        _LOGGER.debug("RequestPinCode for %s", device)
        return f"{self._passkey:06d}"

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802, F821
        masked = f"{self._passkey:06d}"
        _LOGGER.debug(
            "RequestPasskey for %s: answering %d digit code ending in %s",
            device,
            len(masked),
            masked[-2:],
        )
        return self._passkey

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802, F821
        _LOGGER.debug("DisplayPinCode for %s", device)

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802, F821
        _LOGGER.debug("DisplayPasskey for %s (entered %s)", device, entered)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802, F821
        _LOGGER.debug("RequestConfirmation for %s", device)

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802, F821
        _LOGGER.debug("RequestAuthorization for %s", device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802, F821
        _LOGGER.debug("AuthorizeService %s for %s", uuid, device)

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


class PairingAgent:
    """Default BlueZ passkey agent for the duration of a bind.

    Use as an async context manager around connecting, sending the user id
    and bonding.
    """

    def __init__(self, passkey: int) -> None:
        self._passkey = passkey
        self._bus: MessageBus | None = None

    async def __aenter__(self) -> Self:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            bus.export(AGENT_PATH, _PasskeyAgent(self._passkey))
            await _call(
                bus,
                BLUEZ_ROOT,
                AGENT_MANAGER_INTERFACE,
                "RegisterAgent",
                "os",
                [AGENT_PATH, AGENT_CAPABILITY],
            )
            await _call(
                bus,
                BLUEZ_ROOT,
                AGENT_MANAGER_INTERFACE,
                "RequestDefaultAgent",
                "o",
                [AGENT_PATH],
            )
        except BaseException:
            bus.disconnect()
            raise
        self._bus = bus
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        bus, self._bus = self._bus, None
        if bus is None:
            return
        with contextlib.suppress(BluezError):
            await _call(
                bus, BLUEZ_ROOT, AGENT_MANAGER_INTERFACE, "UnregisterAgent", "o", [AGENT_PATH]
            )
        bus.disconnect()

    async def get_device(self, address: str) -> BluezDevice | None:
        """Return the BlueZ state of a device, or None if BlueZ doesn't know it."""
        assert self._bus is not None
        (objects,) = await _call(
            self._bus, "/", OBJECT_MANAGER_INTERFACE, "GetManagedObjects"
        )
        for path, interfaces in objects.items():
            device = interfaces.get(DEVICE_INTERFACE)
            if device and device["Address"].value.upper() == address.upper():
                return BluezDevice(
                    path=path,
                    connected=device["Connected"].value,
                    paired=device["Paired"].value,
                )
        return None

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
        assert self._bus is not None
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
        assert self._bus is not None
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


async def async_describe_device(address: str) -> str:
    """Return BlueZ's pairing state of a device, for diagnostics."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        (objects,) = await _call(bus, "/", OBJECT_MANAGER_INTERFACE, "GetManagedObjects")
    finally:
        bus.disconnect()
    for path, interfaces in objects.items():
        device = interfaces.get(DEVICE_INTERFACE)
        if device and device["Address"].value.upper() == address.upper():
            states = ", ".join(
                f"{key.lower()}={device[key].value}"
                for key in ("Connected", "Paired", "Bonded", "Trusted", "Blocked")
                if key in device
            )
            return f"{path}: {states}"
    return "not known to BlueZ"
