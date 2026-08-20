#!/usr/bin/bash
#
# Beendet den temporären fprintd mit dem lokalen 0c63-Treiber und exportiert
# vorher das Journal genau dieser Invocation, damit während einer Messreihe
# keine wiederholten Root-Passwortdialoge nötig sind.

set -Eeuo pipefail

readonly TEST_UNIT="fprintd-elan0c63-tod"
readonly LOG_DIR="/home/michael/rootserver/fingerprint"

if (( EUID != 0 )); then
  echo "Bitte einmalig als root starten:"
  echo "  sudo $0"
  exit 77
fi

TEST_INVOCATION_ID="$(systemctl show --property=InvocationID --value \
  "${TEST_UNIT}.service" 2>/dev/null || true)"
readonly TEST_INVOCATION_ID

LOG_FILE=""
if [[ -d "${LOG_DIR}" ]]; then
  LOG_STAMP="$(date '+%Y%m%d-%H%M%S')"
  readonly LOG_STAMP

  if LOG_FILE="$(mktemp --tmpdir="${LOG_DIR}" \
      "elan0c63-tod-journal-${LOG_STAMP}-XXXXXX.log")"; then
    if [[ "${TEST_INVOCATION_ID}" =~ ^[[:xdigit:]]{32}$ ]]; then
      journalctl --quiet --no-pager --output=short-iso-precise \
        "_SYSTEMD_INVOCATION_ID=${TEST_INVOCATION_ID}" >"${LOG_FILE}" \
        || echo "Warnung: Journal konnte nicht vollständig exportiert werden." >&2
    else
      journalctl --quiet --no-pager --output=short-iso-precise \
        --unit="${TEST_UNIT}.service" --boot=0 >"${LOG_FILE}" \
        || echo "Warnung: Journal konnte nicht vollständig exportiert werden." >&2
    fi

    chmod 0644 "${LOG_FILE}"
    chown --reference="${LOG_DIR}" "${LOG_FILE}"
  else
    echo "Warnung: Für das Journal konnte keine Ausgabedatei angelegt werden." >&2
  fi
fi

systemctl stop "${TEST_UNIT}.service" 2>/dev/null || true
systemctl reset-failed "${TEST_UNIT}.service" 2>/dev/null || true

echo "Der temporäre 0c63-Dienst ist beendet."
if [[ -n "${LOG_FILE}" ]]; then
  echo "Journal: ${LOG_FILE}"
fi
echo "Das offizielle fprintd wird beim nächsten D-Bus-Zugriff wieder aktiviert."

exit 0
