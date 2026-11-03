# EZVIZ Bluetooth locks for Home Assistant

> [!WARNING]
> **Disclaimer:** this integration and its documentation were created
> entirely by Claude, an AI model by Anthropic. I have not reviewed the code
> myself; I only check whether it works for my own lock and setup. This
> software controls a door lock: use it at your own risk, and keep another
> way to open your door.

Custom integration that talks to EZVIZ smart locks directly over Bluetooth,
without the EZVIZ cloud or gateway. Setup is done entirely in the Home
Assistant UI.

> **Status:** unit tested, but **not yet run against a real lock**. Try it
> with the CLI below first.

## Supported devices

These locks share one Bluetooth protocol, which this integration
implements:

| Lock | Model codes | Status |
| --- | --- | --- |
| DL01S | `CS-DL01S-0ZBTA-LK` | written for this one |
| DL01 Pro | `CS-DL01CP-BT`, `CS-DL01CP-BTY` | same protocol, untested |
| DL01 (other variants) | `CS-DL01BCP-R200-BT-GR` | same protocol, untested |
| L2S | | same protocol, untested |

A keypad and door contact sensor bound to the lock are picked up
automatically.

Other EZVIZ locks and video locks use a different Bluetooth protocol and
are **not supported**. All other EZVIZ devices (cameras, doorbells, plugs,
...) only use Bluetooth to set up Wi-Fi; Home Assistant's built-in EZVIZ
integration covers those.

## What you get

* `lock.<serial>`: lock/unlock; shows *open* when the lock reports the door
  as not closed.
* `sensor.<serial>_battery`: battery percentage of the lock.
* `sensor.<serial>_keypad_battery` / `sensor.<serial>_door_contact_battery`:
  battery of the keypad and door contact sensor, if they are bound to the
  lock. Unlocking with the keypad (code, fingerprint, card) shows up as a
  `door_opened` event with the method.
* `binary_sensor.<serial>_door`: door open/closed. Unavailable when the lock
  doesn't report it.
* `ezviz_ble_event` events for door opened (who, which method), doorbell,
  low battery and alarms. They only arrive while Home Assistant is connected,
  which is up to 30 s after each poll or command.

The state is polled every 15 minutes by default (configurable). Every poll
is a short Bluetooth connection, which costs lock battery.

## Requirements

* The lock must already be set up in the EZVIZ app with your account.
* You need its **6 digit management code** and the **e-mail address** (or
  phone number, if the account has none) of that EZVIZ account. The
  management code is the one you set in the EZVIZ app; only if it was never
  changed is it the default from the battery cover label (0 followed by the
  last five digits of the serial number). Repeated wrong codes may lock you
  out for a while.
* Home Assistant must reach the lock through a **local Bluetooth adapter
  (BlueZ)**. The lock only accepts commands over a bonded link, and bonding
  uses passkey entry, which ESPHome Bluetooth proxies can't do. If proxies are
  closer to the lock than the host adapter, Home Assistant may route the
  connection through them and the lock will refuse it.

## Install

With [HACS](https://hacs.xyz):

1. *HACS → ⋮ → Custom repositories*, add
   `https://github.com/remielowik/ha-ezviz-ble` with type *Integration*.
2. Install *EZVIZ Bluetooth* and restart Home Assistant.

Or manually: copy `custom_components/ezviz_ble` into your Home Assistant
`config/custom_components/` folder and restart.

Then:

1. Close the EZVIZ app or turn off your phone's Bluetooth (the lock accepts
   one Bluetooth connection at a time) and go to *Settings → Devices &
   services → Add integration → EZVIZ Bluetooth*.
2. Pick the lock (its Bluetooth name is its serial number) and enter the
   management code and account.

Setup registers Home Assistant with the lock like the EZVIZ app registers
a phone: it sends the account id, bonds using the management code as
passkey and reads the state. The poll
interval can be changed under the integration's *Configure* button.

## Trying it first with the CLI

`tools/ezviz_ble_cli.py` uses the same library and talks to the host's BlueZ
over D-Bus. Run it in Docker:

```sh
docker build -t ezviz-ble-cli -f tools/Dockerfile .
alias ezble='docker run --rm -it --security-opt apparmor=unconfined -v /run/dbus:/run/dbus:ro ezviz-ble-cli'

ezble scan                                   # lock is marked with * if it advertises the service
ezble bind  AA:BB:CC:DD:EE:FF --account you@example.com --code 123456
ezble state AA:BB:CC:DD:EE:FF --account you@example.com   # also prints battery levels and parts
ezble unlock AA:BB:CC:DD:EE:FF --account you@example.com
ezble lock   AA:BB:CC:DD:EE:FF --account you@example.com
ezble monitor AA:BB:CC:DD:EE:FF --account you@example.com   # prints pushed events
ezble watch AA:BB:CC:DD:EE:FF                # do advertisements change with the lock state?
ezble -v raw AA:BB:CC:DD:EE:FF --account you@example.com 81 01   # any command, frames logged
```

`--security-opt apparmor=unconfined` is needed because Docker's default
AppArmor profile blocks D-Bus calls to BlueZ.

## Not verified yet

* Whether the lock really refuses commands without bonding. If it doesn't,
  ESPHome proxies would work too.
* Whether bonding Home Assistant affects the phones already bonded with the
  lock. The lock supports several users, so it shouldn't.
* What result code 6 of lock/unlock means. It is treated as success.
* The meaning of door-state byte 10 and the bit layout of the DoorStateChange
  push. The integration re-reads the state instead.
* Whether the advertisement changes with the lock state, which would allow
  instant updates without polling. Check this with `ezble watch`.

## Tests

```sh
python3 -m unittest tests/test_protocol.py                          # stdlib only
pytest -o asyncio_mode=auto tests/test_integration.py tests/test_client.py tests/test_bind.py tests/test_bluez.py   # needs pytest-homeassistant-custom-component
```
