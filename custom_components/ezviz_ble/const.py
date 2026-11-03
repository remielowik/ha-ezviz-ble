"""Constants for the EZVIZ Bluetooth integration."""

DOMAIN = "ezviz_ble"

CONF_ACCOUNT = "account"
CONF_MANAGEMENT_CODE = "management_code"
CONF_POLL_INTERVAL = "poll_interval"

# Minutes between state reads. Every read is a BLE connection, which costs
# lock battery.
DEFAULT_POLL_INTERVAL = 15

# Keep the connection open this long after the last command so pushed
# events (door state, doorbell, ...) still arrive.
IDLE_DISCONNECT_SECONDS = 30

# Time the motor needs before reading back the state after (un)locking.
STATE_SETTLE_SECONDS = 3

EVENT_LOCK = f"{DOMAIN}_event"
