#!/usr/bin/bash
#
# Stops the temporary fprintd and first exports the journal of exactly that
# invocation, so a measurement series does not need repeated root prompts.

set -Eeuo pipefail

readonly TEST_UNIT="fprintd-elan0c63-tod"

# Journals go next to the caller, not into the repository: they carry fprintd
# debug output and .log is git-ignored anyway.
readonly LOG_DIR="${SUDO_USER:+/home/${SUDO_USER}}"
readonly LOG_TARGET="${LOG_DIR:-${PWD}}"

if (( EUID != 0 )); then
  echo "Run this once as root:"
  echo "  sudo $0"
  exit 77
fi

TEST_INVOCATION_ID="$(systemctl show --property=InvocationID --value \
  "${TEST_UNIT}.service" 2>/dev/null || true)"
readonly TEST_INVOCATION_ID

LOG_FILE=""
if [[ -d "${LOG_TARGET}" ]]; then
  LOG_STAMP="$(date '+%Y%m%d-%H%M%S')"
  readonly LOG_STAMP

  if LOG_FILE="$(mktemp --tmpdir="${LOG_TARGET}" \
      "elan0c63-tod-journal-${LOG_STAMP}-XXXXXX.log")"; then
    if [[ "${TEST_INVOCATION_ID}" =~ ^[[:xdigit:]]{32}$ ]]; then
      journalctl --quiet --no-pager --output=short-iso-precise \
        "_SYSTEMD_INVOCATION_ID=${TEST_INVOCATION_ID}" >"${LOG_FILE}" \
        || echo "Warning: could not export the journal completely." >&2
    else
      journalctl --quiet --no-pager --output=short-iso-precise \
        --unit="${TEST_UNIT}.service" --boot=0 >"${LOG_FILE}" \
        || echo "Warning: could not export the journal completely." >&2
    fi

    chmod 0644 "${LOG_FILE}"
    chown --reference="${LOG_TARGET}" "${LOG_FILE}"
  else
    echo "Warning: could not create an output file for the journal." >&2
  fi
fi

systemctl stop "${TEST_UNIT}.service" 2>/dev/null || true
systemctl reset-failed "${TEST_UNIT}.service" 2>/dev/null || true

echo "The temporary 0c63 service has stopped."
if [[ -n "${LOG_FILE}" ]]; then
  echo "Journal: ${LOG_FILE}"
fi
echo "The distribution fprintd is reactivated on the next D-Bus access."

exit 0
