# Runs tools/ezviz_ble_cli.py against the host's BlueZ through the D-Bus socket.
#
#   docker build -t ezviz-ble-cli -f tools/Dockerfile .
#   docker run --rm -it --security-opt apparmor=unconfined \
#     -v /run/dbus:/run/dbus:ro ezviz-ble-cli scan
#
# Docker's default AppArmor profile blocks D-Bus calls to BlueZ, hence
# apparmor=unconfined. Root inside the container is what BlueZ's D-Bus
# policy needs for scanning and pairing.
FROM python:3.13-slim
# Same versions as Home Assistant 2026.2's bluetooth integration.
RUN pip install --no-cache-dir "bleak==2.1.1" "bleak-retry-connector==4.4.3" "dbus-fast==3.1.2"
WORKDIR /app
COPY custom_components/ezviz_ble/ezlock custom_components/ezviz_ble/ezlock
COPY tools/ezviz_ble_cli.py tools/ezviz_ble_cli.py
ENTRYPOINT ["python", "tools/ezviz_ble_cli.py"]
