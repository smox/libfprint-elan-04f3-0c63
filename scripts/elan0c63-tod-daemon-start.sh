#!/usr/bin/bash
#
# Startet einen temporären fprintd, der den lokalen 0c63-Treiber lädt.
#
# Es wird nichts installiert und keine Systemdatei ersetzt. Der Treiber wird
# über zwei Umgebungsvariablen eingebunden, die libfprint selbst vorsieht:
#
#   FP_TOD_DRIVERS_DIR   Verzeichnis, aus dem zusätzliche Treiber geladen werden
#   FP_DRIVERS_ALLOWLIST schaltet alle anderen Treiber ab, damit nicht der
#                        eingebaute elan-Treiber dasselbe Gerät beansprucht
#
# Nach dem Stoppen übernimmt wieder das offizielle fprintd.

set -Eeuo pipefail

readonly TEST_UNIT="fprintd-elan0c63-tod"
readonly DRIVER_DIR="/home/michael/rootserver/tools/elan0c63-tod/build"
readonly DRIVER="${DRIVER_DIR}/libfprint-tod-elan0c63.so"
readonly ENTRY_SYMBOL="fpi_tod_shared_driver_get_type"

if (( EUID != 0 )); then
  echo "Bitte einmalig als root starten:"
  echo "  sudo $0"
  exit 77
fi

if [[ ! -r "${DRIVER}" ]]; then
  echo "Fehler: Der Treiber fehlt: ${DRIVER}" >&2
  echo "Zuerst bauen: tools/elan0c63-tod/build.sh" >&2
  exit 1
fi

if ! nm -D --defined-only "${DRIVER}" 2>/dev/null | grep -q "${ENTRY_SYMBOL}"; then
  echo "Fehler: ${DRIVER} exportiert den TOD-Einstiegspunkt nicht." >&2
  exit 1
fi

echo "Stoppe gegebenenfalls laufende fprintd-Dienste ..."
systemctl stop fprintd.service 2>/dev/null || true
systemctl stop "${TEST_UNIT}.service" 2>/dev/null || true
systemctl reset-failed "${TEST_UNIT}.service" 2>/dev/null || true

echo "Starte temporären fprintd mit lokalem 0c63-Treiber ..."
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
  echo "Fehler: Der temporäre fprintd besitzt keine laufende Haupt-PID." >&2
  exit 1
fi

# Belegen, dass wirklich unser Modul geladen wurde und nicht der eingebaute
# Treiber eingesprungen ist.
sleep 1
if ! grep -qF -- "${DRIVER}" "/proc/${TEST_PID}/maps" 2>/dev/null; then
  echo "Warnung: Der lokale Treiber ist noch nicht in /proc/${TEST_PID}/maps." >&2
  echo "Module werden erst bei der ersten Geräteabfrage geladen; das ist" >&2
  echo "normal. Nach dem ersten fprintd-Aufruf erneut prüfen mit:" >&2
  echo "  sudo grep elan0c63 /proc/${TEST_PID}/maps" >&2
fi

echo
echo "Temporärer Dienst läuft als PID ${TEST_PID}."
echo "Treiberverzeichnis: ${DRIVER_DIR}"
echo "Zugelassene Treiber: elan0c63 (der eingebaute elan-Treiber ist abgeschaltet)"
echo "Es wurde nichts installiert und keine Aufnahme gestartet."

exit 0
