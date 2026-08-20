#!/usr/bin/bash
#
# Builds the 0c63 driver in a container, so no development packages have to be
# installed on the host.
#
# Result: build/libfprint-tod-elan0c63.so
#
# The module is not installed. To test it, scripts/elan0c63-tod-daemon-start.sh
# loads it through FP_TOD_DRIVERS_DIR.

set -Eeuo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly IMAGE="elan0c63-build"
readonly MODULE="libfprint-tod-elan0c63.so"

# Only the OpenCV modules actually used. The opencv4 pkg-config module would
# otherwise drag in dnn, videoio and videostab, none of which this driver
# touches.
readonly OPENCV_LIBS="-lopencv_core -lopencv_imgproc -lopencv_features2d -lopencv_calib3d -lopencv_flann"

if ! command -v podman >/dev/null; then
  echo "Error: podman is required." >&2
  exit 1
fi

if ! podman image exists "${IMAGE}" 2>/dev/null; then
  echo "Building container image ${IMAGE} ..."
  podman build -t "${IMAGE}" -f - "${HERE}" <<'CONTAINERFILE'
FROM registry.opensuse.org/opensuse/tumbleweed:latest
RUN zypper --non-interactive install --no-recommends \
      gcc gcc-c++ pkgconf binutils \
      opencv-devel glib2-devel \
      libfprint-tod-devel libfprint-devel
CONTAINERFILE
fi

echo "Compiling ..."
podman run --rm -v "${HERE}:/src:z" "${IMAGE}" bash -eu -c '
  cd /src
  mkdir -p build

  TOD_CFLAGS=$(pkg-config --cflags libfprint-2-tod-1)
  CV_CFLAGS=$(pkg-config --cflags-only-I opencv4)

  # The matcher is C++ because of OpenCV; the driver stays plain C.
  g++ -O2 -std=c++17 -fPIC -Wall -Wextra -Wno-unused-parameter \
      -c elan0c63-match.cpp -o build/match.o \
      ${CV_CFLAGS} $(pkg-config --cflags glib-2.0)

  gcc -O2 -std=gnu11 -fPIC -Wall -Wextra -Wno-unused-parameter \
      -c elan0c63-tod.c -o build/tod.o \
      -I. ${TOD_CFLAGS}

  # Two libraries are needed: libfprint-2-tod carries the 235 private fpi_*
  # symbols, while the public fp_* types live in libfprint-2.
  g++ -shared -o build/'"${MODULE}"' build/tod.o build/match.o \
      $(pkg-config --libs libfprint-2-tod-1 libfprint-2) \
      '"${OPENCV_LIBS}"'
'

readonly BUILT="${HERE}/build/${MODULE}"

echo
echo "Verifying the result ..."
podman run --rm -v "${HERE}:/src:z" "${IMAGE}" bash -eu -c '
  M=/src/build/'"${MODULE}"'

  printf "  size:              %s bytes\n" "$(stat -c %s "$M")"

  if nm -D --defined-only "$M" | grep -q fpi_tod_shared_driver_get_type; then
    echo "  TOD entry point:   present"
  else
    echo "  TOD entry point:   MISSING" >&2
    exit 1
  fi

  # Every undefined symbol has to come from one of the linked libraries. If one
  # is left over, the driver only fails at runtime. Weak symbols (nm type "w")
  # are toolchain placeholders and need not resolve; only real undefined
  # symbols count.
  nm -D -u "$M" | grep -E "^[0-9a-f ]* U " | sed "s/^.* U //" | sort > /tmp/need
  for L in $(ldd "$M" | sed -n "s/.*=> \([^ ]*\).*/\1/p"); do
    nm -D --defined-only "$L" 2>/dev/null | sed "s/.* //"
  done | sort -u > /tmp/have
  MISSING=$(comm -23 /tmp/need /tmp/have | grep -v "^_ITM\|^__gmon\|^__cxa_finalize" || true)
  if [ -n "$MISSING" ]; then
    echo "  unresolved symbols:"
    echo "$MISSING" | sed "s/^/    /"
    exit 1
  fi
  echo "  symbol resolution: complete"

  echo "  linked against:"
  ldd "$M" | sed -n "s/.*\(libfprint[^ ]*\|libopencv[^ ]*\) =>.*/    \1/p" | sort
'

echo
echo "Done: ${BUILT}"
echo "Load it with: sudo scripts/elan0c63-tod-daemon-start.sh (from the repository root)"
