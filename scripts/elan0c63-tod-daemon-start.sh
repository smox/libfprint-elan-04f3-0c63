#!/usr/bin/bash
#
# Starts a temporary fprintd that loads the local 0c63 driver.
#
# Nothing is installed and no system file is replaced. The driver is loaded
# through two environment variables libfprint provides for this purpose:
#
#   FP_TOD_DRIVERS_DIR   directory to load additional drivers from
#   FP_DRIVERS_ALLOWLIST disables every other driver, so the in-tree elan
#                        driver cannot claim the same device
#
# After stopping, the distribution's fprintd takes over again.

set -Eeuo pipefail

readonly TEST_UNIT="fprintd-elan0c63-tod"

# Derived from this script's own location, so the repository can live anywhere.
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly DRIVER_DIR="${REPO_ROOT}/tools/elan0c63-tod/build"
readonly DRIVER="${DRIVER_DIR}/libfprint-tod-elan0c63.so"
readonly ENTRY_SYMBOL="fpi_tod_shared_driver_get_type"

if (( EUID != 0 )); then
  echo "Run this once as root:"
  echo "  sudo $0"
  exit 77
fi

if [[ ! -r "${DRIVER}" ]]; then
  echo "Error: driver not found: ${DRIVER}" >&2
  echo "Build it first: tools/elan0c63-tod/build.sh" >&2
  exit 1
fi

if ! nm -D --defined-only "${DRIVER}" 2>/dev/null | grep -q "${ENTRY_SYMBOL}"; then
  echo "Error: ${DRIVER} does not export the TOD entry point." >&2
  exit 1
fi

echo "Stopping any running fprintd services ..."
systemctl stop fprintd.service 2>/dev/null || true
systemctl stop "${TEST_UNIT}.service" 2>/dev/null || true
systemctl reset-failed "${TEST_UNIT}.service" 2>/dev/null || true

echo "Starting temporary fprintd with the local 0c63 driver ..."
systemd-run \
  --unit="${TEST_UNIT}" \
  --description="Temporary fprintd with local ELAN 0c63 descriptor driver" \
  --collect \
  --service-type=dbus \
  --property=BusName=net.reactivated.Fprint \
  --property=StateDirectory=fprint \
  --property=StateDirectoryMode=0700 \
  --property=ProtectSystem=strict \
  --property=ProtectHome=read-only \
  --property=ProtectKernelTunables=true \
  --property=ProtectKernelLogs=true \
  --property=ProtectControlGroups=true \
  --property=ProtectKernelModules=true \
  --property=ProtectClock=true \
  --property=PrivateTmp=true \
  --property=NoNewPrivileges=true \
  --property=RestrictRealtime=true \
  --property='RestrictAddressFamilies=AF_UNIX AF_LOCAL AF_NETLINK' \
  --property='DeviceAllow=char-usb_device rw' \
  --property=ReadWritePaths=/sys/devices \
  --setenv="FP_TOD_DRIVERS_DIR=${DRIVER_DIR}" \
  --setenv="FP_DRIVERS_ALLOWLIST=elan0c63" \
  --setenv=G_MESSAGES_DEBUG=all \
  /usr/libexec/fprintd --no-timeout

readonly TEST_PID="$(systemctl show --property=MainPID --value "${TEST_UNIT}.service")"

if [[ -z "${TEST_PID}" || "${TEST_PID}" == "0" ]]; then
  echo "Error: the temporary fprintd has no running main PID." >&2
  exit 1
fi

# Confirm our module really was loaded and the in-tree driver did not step in.
sleep 1
if ! grep -qF -- "${DRIVER}" "/proc/${TEST_PID}/maps" 2>/dev/null; then
  echo "Note: the local driver is not in /proc/${TEST_PID}/maps yet." >&2
  echo "Modules load on the first device query, which is expected. Check" >&2
  echo "again after the first fprintd call with:" >&2
  echo "  sudo grep elan0c63 /proc/${TEST_PID}/maps" >&2
fi

echo
echo "Temporary service running as PID ${TEST_PID}."
echo "Driver directory: ${DRIVER_DIR}"
echo "Allowed drivers: elan0c63 (the in-tree elan driver is disabled)"
echo "Nothing was installed and no capture was started."

exit 0
