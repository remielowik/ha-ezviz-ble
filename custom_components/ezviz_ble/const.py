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

# Set the lock clock this often (and before adding a PIN): PIN validity
# windows are checked against it, and it does not follow daylight saving.
CLOCK_SYNC_HOURS = 6

SERVICE_LIST_PINS = "list_pins"
SERVICE_ADD_PIN = "add_pin"
SERVICE_DELETE_PIN = "delete_pin"
ATTR_PIN = "pin"
ATTR_PIN_NAME = "name"
ATTR_VALID_FROM = "valid_from"
ATTR_VALID_UNTIL = "valid_until"
ATTR_WEEKDAYS = "weekdays"
ATTR_START_TIME = "start_time"
ATTR_END_TIME = "end_time"
ATTR_USER_INDEX = "user_index"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

EVENT_LOCK = f"{DOMAIN}_event"
