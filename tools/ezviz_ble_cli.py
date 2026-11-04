"""Command line tool to try the EZVIZ Bluetooth lock protocol outside Home Assistant.

Uses the same library as the integration. Needs bleak, bleak-retry-connector
and (for bind) dbus-fast with access to the host's BlueZ; see tools/Dockerfile.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import sys

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "ezviz_ble"))

from ezlock.bind import async_bind  # noqa: E402
from ezlock.client import EzLockClient  # noqa: E402
from ezlock.protocol import (  # noqa: E402
    NOTIFY_CHAR_UUID,
    SERVICE_UUID,
    WRITE_CHAR_UUID,
    Frame,
    LockAction,
    lock_user_id,
)


def _describe(device: BLEDevice, adv: AdvertisementData) -> str:
    parts = [f"{device.address}  rssi={adv.rssi:4d}  name={adv.local_name or device.name!r}"]
    if adv.service_uuids:
        parts.append(f"services={adv.service_uuids}")
    for company, data in adv.manufacturer_data.items():
        parts.append(f"mfr[0x{company:04x}]={data.hex()}")
    for uuid, data in adv.service_data.items():
        parts.append(f"svcdata[{uuid}]={data.hex()}")
    marker = "*" if SERVICE_UUID in adv.service_uuids else " "
    return f"{marker} " + "  ".join(parts)


async def cmd_scan(args: argparse.Namespace) -> None:
    found = await BleakScanner.discover(timeout=args.seconds, return_adv=True)
    for device, adv in sorted(found.values(), key=lambda item: -item[1].rssi):
        if adv.local_name or device.name:
            print(_describe(device, adv))
    print("\n* = advertises the EZVIZ lock service UUID")


async def cmd_watch(args: argparse.Namespace) -> None:
    """Print advertisement changes, e.g. to see if they follow the lock state."""
    last: str | None = None

    def on_adv(device: BLEDevice, adv: AdvertisementData) -> None:
        nonlocal last
        if device.address.upper() != args.address.upper():
            return
        line = _describe(device, adv).split("rssi=")[1].split("  ", 1)[-1]
        if line != last:
            print(f"{asyncio.get_running_loop().time():10.1f}  {line}")
            last = line

    async with BleakScanner(on_adv):
        await asyncio.sleep(args.seconds)


async def _find(address: str) -> BLEDevice:
    device = await BleakScanner.find_device_by_address(address, timeout=20)
    if device is None:
        sys.exit(f"{address} not found; make sure the lock is in range and the phone app is closed")
    return device


def _print_push(frame: Frame) -> None:
    print(f"push {frame.main:02X}/{frame.sub:02X} result={frame.result} body={frame.body.hex()}")


async def cmd_bind(args: argparse.Namespace) -> None:
    state = await async_bind(await _find(args.address), lock_user_id(args.account), args.code)
    if state is None:
        print("bound; the lock did not report its state yet, try `state`")
    else:
        print(f"bound, state: {state}")


async def _with_client(args: argparse.Namespace, operation) -> None:
    client = EzLockClient(await _find(args.address), lock_user_id(args.account), _print_push)
    try:
        await operation(client)
    finally:
        await client.disconnect()


async def cmd_state(args: argparse.Namespace) -> None:
    async def run(client: EzLockClient) -> None:
        state = await client.query_state()
        print(f"status={state.status.value} battery={state.battery}% flags=0x{state.flags:02x} "
              f"door_sensor_byte={state.door_sensor} door_open={state.door_open} raw={state.raw.hex()}")
        print(f"battery: {await client.query_battery()}")
        print(f"parts: {await client.query_parts_info()}")

    await _with_client(args, run)


async def cmd_control(args: argparse.Namespace) -> None:
    action = LockAction.UNLOCK if args.command == "unlock" else LockAction.LOCK

    async def run(client: EzLockClient) -> None:
        await client.control(action)
        print(f"{args.command}: ok")
        await asyncio.sleep(3)
        print(f"state now: {(await client.query_state()).status.value}")

    await _with_client(args, run)


async def cmd_monitor(args: argparse.Namespace) -> None:
    async def run(client: EzLockClient) -> None:
        await client.connect()
        print(f"connected, printing pushes for {args.seconds} s")
        await asyncio.sleep(args.seconds)

    await _with_client(args, run)


async def cmd_raw(args: argparse.Namespace) -> None:
    command = (int(args.main, 16), int(args.sub, 16))
    body = bytes.fromhex(args.body or "")
    char_uuid = NOTIFY_CHAR_UUID if args.notify_char else WRITE_CHAR_UUID

    async def run(client: EzLockClient) -> None:
        frame = await client.request(command, body, char_uuid=char_uuid, timeout=args.timeout)
        print(f"response {frame.main:02X}/{frame.sub:02X} result={frame.result} "
              f"reserved={frame.reserved} body={frame.body.hex()}")

    await _with_client(args, run)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="log every frame")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="list nearby devices")
    scan.add_argument("--seconds", type=float, default=10)
    scan.set_defaults(func=cmd_scan)

    watch = sub.add_parser("watch", help="print advertisement changes of one device")
    watch.add_argument("address")
    watch.add_argument("--seconds", type=float, default=120)
    watch.set_defaults(func=cmd_watch)

    def lock_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        parser_ = sub.add_parser(name, help=help_text)
        parser_.add_argument("address")
        parser_.add_argument("--account", required=True, help="EZVIZ account e-mail or phone")
        return parser_

    bind = lock_parser("bind", "send the user id and bond using the management code")
    bind.add_argument("--code", required=True, help="6 digit management code")
    bind.set_defaults(func=cmd_bind)

    lock_parser("state", "read lock/door/battery state").set_defaults(func=cmd_state)
    lock_parser("unlock", "unlock the door").set_defaults(func=cmd_control)
    lock_parser("lock", "lock the door").set_defaults(func=cmd_control)

    monitor = lock_parser("monitor", "stay connected and print pushed events")
    monitor.add_argument("--seconds", type=float, default=120)
    monitor.set_defaults(func=cmd_monitor)

    raw = lock_parser("raw", "send any lock-layer command")
    raw.add_argument("main", help="hex, e.g. 81")
    raw.add_argument("sub", help="hex, e.g. 01")
    raw.add_argument("body", nargs="?", help="hex body")
    raw.add_argument("--notify-char", action="store_true", help="write to ...2b20 instead of ...2b21")
    raw.add_argument("--timeout", type=float, default=5)
    raw.set_defaults(func=cmd_raw)

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    if not args.verbose:
        logging.getLogger("bleak").setLevel(logging.WARNING)
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
