#!/usr/bin/bash
#
# Test harness for the ELAN 04f3:0c63 descriptor-matching driver.
#
# Runs a guided enrolment and a series of verifications, then writes a report
# you can share. The report contains match scores, feature counts and timings.
# It contains no fingerprint image data and no biometric template.
#
# Usage:   ./fingerprint-test.sh
#          ./fingerprint-test.sh --rounds 10 --finger right-index-finger

set -Eeuo pipefail

VERSION="0.1.0"
FINGER="right-index-finger"
ROUNDS=8
IMPOSTOR_ROUNDS=8
REPORT=""
SKIP_ENROLL=0

usage() {
  cat <<EOF
ELAN 04f3:0c63 driver test, version ${VERSION}

  --finger NAME     finger to enrol and test (default: ${FINGER})
  --rounds N        verification attempts with the enrolled finger (default: ${ROUNDS})
  --impostor N      attempts with OTHER fingers (default: ${IMPOSTOR_ROUNDS})
  --skip-enroll     reuse an existing enrolment
  --report PATH     where to write the report (default: ./fingerprint-report-<date>.txt)
  --help

Valid finger names:
  left-thumb  left-index-finger  left-middle-finger  left-ring-finger
  left-little-finger  right-thumb  right-index-finger  right-middle-finger
  right-ring-finger  right-little-finger
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --finger)    FINGER="$2"; shift 2 ;;
    --rounds)    ROUNDS="$2"; shift 2 ;;
    --impostor)  IMPOSTOR_ROUNDS="$2"; shift 2 ;;
    --report)    REPORT="$2"; shift 2 ;;
    --skip-enroll) SKIP_ENROLL=1; shift ;;
    --help|-h)   usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 64 ;;
  esac
done

[[ -n "${REPORT}" ]] || REPORT="./fingerprint-report-$(date +%Y%m%d-%H%M%S).txt"

readonly DRIVER_SO="libfprint-tod-elan0c63.so"
readonly SENSOR_ID="04f3:0c63"

note() { printf '  %s\n' "$*"; }
head2() { printf '\n== %s ==\n' "$*"; }

# ---------------------------------------------------------------- preflight

head2 "Checking prerequisites"

FAIL=0

if lsusb 2>/dev/null | grep -qi "${SENSOR_ID}"; then
  note "sensor ${SENSOR_ID}: present"
else
  note "sensor ${SENSOR_ID}: NOT FOUND"
  note "  This driver is only for that exact device. Check with: lsusb | grep 04f3"
  FAIL=1
fi

DRIVER_PATH="$(find /usr/lib64 /usr/lib -name "${DRIVER_SO}" 2>/dev/null | head -1 || true)"
if [[ -n "${DRIVER_PATH}" ]]; then
  note "driver: ${DRIVER_PATH}"
else
  note "driver: NOT INSTALLED"
  note "  Install the package first, then run this again."
  FAIL=1
fi

for tool in fprintd-enroll fprintd-verify fprintd-delete journalctl; do
  command -v "${tool}" >/dev/null || { note "missing tool: ${tool}"; FAIL=1; }
done

(( FAIL == 0 )) || { echo; echo "Prerequisites not met, aborting."; exit 1; }

# The driver only wins over the in-tree elan driver if libfprint picked it.
DEVICE_NAME="$(timeout 20 fprintd-list "${USER}" 2>&1 \
                 | grep -oE "ElanTech[^.]*" | head -1 || true)"
if [[ "${DEVICE_NAME}" == *"descriptor matching"* ]]; then
  note "active driver: ${DEVICE_NAME}"
else
  note "active driver: ${DEVICE_NAME:-unknown}"
  note "  WARNING: the in-tree driver appears to be in use, not this one."
  note "  Try: sudo systemctl stop fprintd.service   then run again."
fi

# Reading fprintd's journal needs privileges. Without it the report still
# works, it just has no scores in it.
HAVE_JOURNAL=0
if sudo -n true 2>/dev/null; then
  HAVE_JOURNAL=1
  note "journal access: yes"
else
  note "journal access: needs your password"
  note "  The match scores live in fprintd's system journal. Without them the"
  note "  report only has pass/fail counts, which is much less useful."
  note ""
  if sudo -v; then
    HAVE_JOURNAL=1
    note "journal access: granted"
  else
    note "journal access: declined - report will have no scores"
  fi
fi

START_TIME="$(date '+%Y-%m-%d %H:%M:%S')"
START_CURSOR=""
if (( HAVE_JOURNAL )); then
  START_CURSOR="$(sudo journalctl -u fprintd.service -n0 --show-cursor 2>/dev/null \
                  | sed -n 's/^-- cursor: //p' || true)"
fi

# ------------------------------------------------------------------ enrol

if (( SKIP_ENROLL == 0 )); then
  head2 "Enrolment: ${FINGER}"
  note "You will be asked to touch the sensor about 12 times."
  note ""
  note "IMPORTANT: place your finger NORMALLY each time - do NOT try to hit"
  note "the exact same spot. The sensor only sees about 4 x 4 mm, so the"
  note "stages are meant to cover different parts of your fingertip."
  note "Lift the finger between stages."
  note ""
  read -r -p "  Press Enter to start, or Ctrl-C to abort. "

  fprintd-delete "${USER}" -f "${FINGER}" >/dev/null 2>&1 || true

  if ! fprintd-enroll -f "${FINGER}"; then
    echo
    echo "Enrolment failed. The report will still be written; please share it."
  fi
fi

# ----------------------------------------------------------------- verify

head2 "Verification: ${ROUNDS} attempts with ${FINGER}"
note "Place the same finger normally each time."
note ""
read -r -p "  Press Enter to start. "

GENUINE_PASS=0
for ((i = 1; i <= ROUNDS; i++)); do
  printf '  [%d/%d] ' "$i" "${ROUNDS}"
  if fprintd-verify -f "${FINGER}" 2>&1 | grep -q "verify-match"; then
    echo "match"
    GENUINE_PASS=$((GENUINE_PASS + 1))
  else
    echo "no match"
  fi
done

IMPOSTOR_FAIL=0
if (( IMPOSTOR_ROUNDS > 0 )); then
  head2 "Security check: ${IMPOSTOR_ROUNDS} attempts with OTHER fingers"
  note "Use fingers you did NOT enrol. Vary them."
  note "Every one of these MUST be rejected."
  note ""
  read -r -p "  Press Enter to start. "

  for ((i = 1; i <= IMPOSTOR_ROUNDS; i++)); do
    printf '  [%d/%d] ' "$i" "${IMPOSTOR_ROUNDS}"
    if fprintd-verify -f "${FINGER}" 2>&1 | grep -q "verify-match"; then
      echo "MATCH  <-- false accept, this is a problem"
    else
      echo "rejected"
      IMPOSTOR_FAIL=$((IMPOSTOR_FAIL + 1))
    fi
  done
fi

# ----------------------------------------------------------------- report

head2 "Writing report"

{
  echo "ELAN 04f3:0c63 descriptor driver - test report"
  echo "generated by fingerprint-test.sh ${VERSION}"
  echo "started ${START_TIME}, finished $(date '+%Y-%m-%d %H:%M:%S')"
  echo
  echo "This report contains match scores and feature counts only."
  echo "It contains no fingerprint image and no biometric template."
  echo
  echo "== System =="
  echo "product:   $(cat /sys/class/dmi/id/product_name 2>/dev/null || echo unknown)"
  echo "board:     $(cat /sys/class/dmi/id/board_name 2>/dev/null || echo unknown)"
  echo "os:        $(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME}" || echo unknown)"
  echo "kernel:    $(uname -r)"
  echo "libfprint: $(rpm -q libfprint-2-tod1 2>/dev/null \
                    || dpkg-query -W -f='${Version}' libfprint-2-tod1 2>/dev/null \
                    || echo unknown)"
  echo "driver:    $(rpm -q libfprint-tod-elan0c63 2>/dev/null || echo "file: ${DRIVER_PATH}")"
  echo "opencv:    $(rpm -qf --qf '%{NAME}-%{VERSION}\n' \
                    "$(ldd "${DRIVER_PATH}" 2>/dev/null \
                       | sed -n 's/.*=> \(.*libopencv_core[^ ]*\).*/\1/p' | head -1)" \
                    2>/dev/null || echo unknown)"
  echo "device:    ${DEVICE_NAME:-unknown}"
  echo "usb:       $(lsusb 2>/dev/null | grep -i "${SENSOR_ID}" || echo unknown)"
  echo
  echo "== Result =="
  echo "finger tested:      ${FINGER}"
  echo "genuine attempts:   ${GENUINE_PASS} of ${ROUNDS} recognised"
  echo "impostor attempts:  ${IMPOSTOR_FAIL} of ${IMPOSTOR_ROUNDS} correctly rejected"
  if (( IMPOSTOR_ROUNDS > 0 && IMPOSTOR_FAIL < IMPOSTOR_ROUNDS )); then
    echo "  WARNING: at least one other finger was accepted."
  fi
  echo
} > "${REPORT}"

if (( HAVE_JOURNAL )); then
  {
    echo "== Match scores =="
    echo "Score is the number of geometrically confirmed feature correspondences."
    echo "The driver accepts at 5 or above."
    echo
    if [[ -n "${START_CURSOR}" ]]; then
      sudo journalctl -u fprintd.service --after-cursor "${START_CURSOR}" 2>/dev/null
    else
      sudo journalctl -u fprintd.service --since "${START_TIME}" 2>/dev/null
    fi | grep -oE "(verify|identify) best score [0-9]+" \
       | grep -oE "[0-9]+$" | nl -w4 -s'. score '
    echo
    echo "== Features per capture =="
    echo "How many SIFT keypoints each capture produced."
    echo
    if [[ -n "${START_CURSOR}" ]]; then
      sudo journalctl -u fprintd.service --after-cursor "${START_CURSOR}" 2>/dev/null
    else
      sudo journalctl -u fprintd.service --since "${START_TIME}" 2>/dev/null
    fi | grep -oE "capture yielded [0-9]+ keypoints from [0-9]+ frames" \
       | nl -w4 -s'. '
    echo
    echo "== Calibration =="
    if [[ -n "${START_CURSOR}" ]]; then
      sudo journalctl -u fprintd.service --after-cursor "${START_CURSOR}" 2>/dev/null
    else
      sudo journalctl -u fprintd.service --since "${START_TIME}" 2>/dev/null
    fi | grep -oE "calibration mean [0-9]+, background mean [0-9]+, delta [0-9]+" \
       | sort | uniq -c | sed 's/^/  /'
    echo
    echo "== Errors and retries =="
    if [[ -n "${START_CURSOR}" ]]; then
      sudo journalctl -u fprintd.service --after-cursor "${START_CURSOR}" 2>/dev/null
    else
      sudo journalctl -u fprintd.service --since "${START_TIME}" 2>/dev/null
    fi | grep -iE "error|retry|failed|warning" \
       | grep -vE "Failed to open directory" \
       | sed -E 's/^[^ ]+ [^ ]+ [^:]+: //' | sort | uniq -c | sort -rn | head -20 \
       | sed 's/^/  /'
    echo "  (empty means no errors)"
  } >> "${REPORT}"
else
  echo "== Match scores ==" >> "${REPORT}"
  echo "not collected - no privileges to read the fprintd journal" >> "${REPORT}"
fi

echo
note "Report written: ${REPORT}"
note ""
note "Summary: ${GENUINE_PASS}/${ROUNDS} recognised, "\
"${IMPOSTOR_FAIL}/${IMPOSTOR_ROUNDS} others correctly rejected"
note ""
note "Please read the report before sharing it. It should contain no personal"
note "data beyond your laptop model and package versions."
