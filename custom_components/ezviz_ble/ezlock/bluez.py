"""Passkey bonding through BlueZ over D-Bus.

The lock asks for passkey entry when bonding; the passkey is its 6 digit
management code. bleak cannot answer passkey requests, so this module
registers its own org.bluez.Agent1 for the duration of the pairing call.
"""

# No "from __future__ import annotations": dbus-fast reads the D-Bus
# signatures from the annotations of the agent methods.

import asyncio
import contextlib
from dataclasses import dataclass

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

BLUEZ_SERVICE = "org.bluez"
DEVICE_INTERFACE = "org.bluez.Device1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
AGENT_PATH = "/org/homeassistant/ezviz_ble/agent"

PAIR_TIMEOUT = 30.0


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
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802, F821
        return f"{self._passkey:06d}"

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802, F821
        return self._passkey

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802, F821
        pass

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802, F821
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802, F821
        pass

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802, F821
        pass

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802, F821
        pass

    @method()
    def Cancel(self):  # noqa: N802
        pass


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


async def _find_device(bus: MessageBus, address: str) -> BluezDevice | None:
    (objects,) = await _call(bus, "/", OBJECT_MANAGER_INTERFACE, "GetManagedObjects")
    for path, interfaces in objects.items():
        device = interfaces.get(DEVICE_INTERFACE)
        if device and device["Address"].value.upper() == address.upper():
            return BluezDevice(
                path=path,
                connected=device["Connected"].value,
                paired=device["Paired"].value,
            )
    return None


async def async_get_device(address: str) -> BluezDevice | None:
    """Return the BlueZ state of a device, or None if BlueZ doesn't know it."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        return await _find_device(bus, address)
    finally:
        bus.disconnect()


async def async_pair(address: str, passkey: int, timeout: float = PAIR_TIMEOUT) -> None:
    """Bond with the device using passkey entry and mark it trusted."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        device = await _find_device(bus, address)
        if device is None:
            raise BluezError("org.bluez.Error.DoesNotExist", address)
        if not device.paired:
            bus.export(AGENT_PATH, _PasskeyAgent(passkey))
            await _call(
                bus,
                "/org/bluez",
                AGENT_MANAGER_INTERFACE,
                "RegisterAgent",
                "os",
                [AGENT_PATH, "KeyboardOnly"],
            )
            try:
                async with asyncio.timeout(timeout):
                    await _call(bus, device.path, DEVICE_INTERFACE, "Pair")
            except BluezError as err:
                if err.name != "org.bluez.Error.AlreadyExists":
                    raise
            except TimeoutError:
                with contextlib.suppress(BluezError):
                    await _call(bus, device.path, DEVICE_INTERFACE, "CancelPairing")
                raise
            finally:
                with contextlib.suppress(BluezError):
                    await _call(
                        bus,
                        "/org/bluez",
                        AGENT_MANAGER_INTERFACE,
                        "UnregisterAgent",
                        "o",
                        [AGENT_PATH],
                    )
        await _call(
            bus,
            device.path,
            PROPERTIES_INTERFACE,
            "Set",
            "ssv",
            [DEVICE_INTERFACE, "Trusted", Variant("b", True)],
        )
    finally:
        bus.disconnect()
