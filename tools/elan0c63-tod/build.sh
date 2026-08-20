#!/usr/bin/bash
#
# Baut den 0c63-Treiber in einem Container, damit auf dem Host keine
# Entwicklungspakete installiert werden müssen.
#
# Ergebnis: build/libfprint-tod-elan0c63.so
#
# Das Modul wird nicht installiert. Zum Testen lädt es
# scripts/elan0c63-tod-daemon-start.sh über FP_TOD_DRIVERS_DIR.

set -Eeuo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly IMAGE="elan0c63-build"
readonly MODULE="libfprint-tod-elan0c63.so"

# Nur die tatsächlich benutzten OpenCV-Module. Das pkg-config-Paket opencv4
# zieht sonst dnn, videoio und videostab mit herein, die der Treiber nicht
# anfasst.
readonly OPENCV_LIBS="-lopencv_core -lopencv_imgproc -lopencv_features2d -lopencv_calib3d -lopencv_flann"

if ! command -v podman >/dev/null; then
  echo "Fehler: podman wird benötigt." >&2
  exit 1
fi

if ! podman image exists "${IMAGE}" 2>/dev/null; then
  echo "Baue Containerabbild ${IMAGE} ..."
  podman build -t "${IMAGE}" -f - "${HERE}" <<'CONTAINERFILE'
FROM registry.opensuse.org/opensuse/tumbleweed:latest
RUN zypper --non-interactive install --no-recommends \
      gcc gcc-c++ pkgconf binutils \
      opencv-devel glib2-devel \
      libfprint-tod-devel libfprint-devel
CONTAINERFILE
fi

echo "Übersetze ..."
podman run --rm -v "${HERE}:/src:z" "${IMAGE}" bash -eu -c '
  cd /src
  mkdir -p build

  TOD_CFLAGS=$(pkg-config --cflags libfprint-2-tod-1)
  CV_CFLAGS=$(pkg-config --cflags-only-I opencv4)

  # Der Matcher ist C++ wegen OpenCV, der Treiber bleibt reines C.
  g++ -O2 -std=c++17 -fPIC -Wall -Wextra -Wno-unused-parameter \
      -c elan0c63-match.cpp -o build/match.o \
      ${CV_CFLAGS} $(pkg-config --cflags glib-2.0)

  gcc -O2 -std=gnu11 -fPIC -Wall -Wextra -Wno-unused-parameter \
      -c elan0c63-tod.c -o build/tod.o \
      -I. ${TOD_CFLAGS}

  # Zwei Bibliotheken sind nötig: libfprint-2-tod trägt die 235 privaten
  # fpi_*-Symbole, die öffentlichen fp_*-Typen liegen in libfprint-2.
  g++ -shared -o build/'"${MODULE}"' build/tod.o build/match.o \
      $(pkg-config --libs libfprint-2-tod-1 libfprint-2) \
      '"${OPENCV_LIBS}"'
'

readonly BUILT="${HERE}/build/${MODULE}"

echo
echo "Prüfe das Ergebnis ..."
podman run --rm -v "${HERE}:/src:z" "${IMAGE}" bash -eu -c '
  M=/src/build/'"${MODULE}"'

  printf "  Größe:            %s Byte\n" "$(stat -c %s "$M")"

  if nm -D --defined-only "$M" | grep -q fpi_tod_shared_driver_get_type; then
    echo "  TOD-Einstiegspunkt: vorhanden"
  else
    echo "  TOD-Einstiegspunkt: FEHLT" >&2
    exit 1
  fi

  # Jedes undefinierte Symbol muss aus einer der verlinkten Bibliotheken
  # kommen. Bleibt eines übrig, scheitert der Treiber erst zur Laufzeit.
  # Schwache Symbole (nm-Typ "w") sind Toolchain-Platzhalter und muessen
  # nicht aufloesbar sein; nur echte undefinierte Symbole zaehlen.
  nm -D -u "$M" | grep -E "^[0-9a-f ]* U " | sed "s/^.* U //" | sort > /tmp/need
  for L in $(ldd "$M" | sed -n "s/.*=> \([^ ]*\).*/\1/p"); do
    nm -D --defined-only "$L" 2>/dev/null | sed "s/.* //"
  done | sort -u > /tmp/have
  MISSING=$(comm -23 /tmp/need /tmp/have | grep -v "^_ITM\|^__gmon\|^__cxa_finalize" || true)
  if [ -n "$MISSING" ]; then
    echo "  ungelöste Symbole:"
    echo "$MISSING" | sed "s/^/    /"
    exit 1
  fi
  echo "  Symbolauflösung:  vollständig"

  echo "  verlinkt gegen:"
  ldd "$M" | sed -n "s/.*\(libfprint[^ ]*\|libopencv[^ ]*\) =>.*/    \1/p" | sort
'

echo
echo "Fertig: ${BUILT}"
echo "Laden mit: sudo /home/michael/rootserver/scripts/elan0c63-tod-daemon-start.sh"
