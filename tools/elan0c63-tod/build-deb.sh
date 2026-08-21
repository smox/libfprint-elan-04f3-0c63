#!/usr/bin/bash
#
# Builds an installable .deb in a container, so no build dependencies land on
# the host. Intended for Ubuntu-based systems, including TUXEDO OS.
#
# Result: deb/libfprint-tod-elan0c63_<version>_<arch>.deb
#
# Default target is Ubuntu 24.04 (noble), which is what TUXEDO OS 4 is based
# on. Pass a different image to build elsewhere:
#
#   ./build-deb.sh docker.io/library/ubuntu:22.04
#   ./build-deb.sh docker.io/library/debian:trixie
#
# Note that the module must match the libfprint on the target machine. A .deb
# built against noble's libfprint 1.94.7+tod1 and OpenCV 4.6 is not
# interchangeable with the openSUSE RPM, which links OpenCV 4.13.

set -Eeuo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly IMAGE="${1:-docker.io/library/ubuntu:24.04}"
readonly PKG="libfprint-tod-elan0c63"

command -v podman >/dev/null || { echo "Error: podman is required." >&2; exit 1; }

VERSION="$(sed -n "1s/.*(\([^-]*\)-.*/\1/p" "${HERE}/debian/changelog")"
[[ -n "${VERSION}" ]] || { echo "Error: no version in debian/changelog." >&2; exit 1; }
readonly VERSION

echo "Building ${PKG} ${VERSION} for ${IMAGE}"

rm -rf "${HERE}/deb"
mkdir -p "${HERE}/deb"

# The source tree is copied into the container rather than bind-mounted for the
# build itself: dpkg-buildpackage writes the .orig tarball and build artefacts
# into the parent directory, and it should not litter the repository.
# Cache the build dependencies in an image. Downloading roughly half a
# gigabyte of build-essential, debhelper and libopencv-dev on every run
# dominated the runtime; this pays for it once per target.
# Derived from the image name, sanitised: a podman repository name may not
# contain uppercase, may not repeat separators and may not begin or end with
# one, so "docker.io/library/ubuntu:24.04" cannot be used verbatim.
BUILDER="elan0c63-deb-$(basename "${IMAGE}" \
  | tr 'A-Z' 'a-z' | tr -c 'a-z0-9' '-' | tr -s '-' | sed 's/^-//; s/-$//')"
readonly BUILDER

if ! podman image exists "${BUILDER}" 2>/dev/null; then
  echo "Building container image ${BUILDER} (first run only) ..."
  podman build -t "${BUILDER}" -f - . >/dev/null <<CONTAINERFILE
FROM ${IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      build-essential debhelper dpkg-dev fakeroot \
      meson ninja-build pkgconf binutils \
      libfprint-2-tod-dev libfprint-2-dev libgusb-dev \
      libglib2.0-dev libopencv-dev \
    && rm -rf /var/lib/apt/lists/*
CONTAINERFILE
fi

podman run --rm -i \
  -v "${HERE}:/src:ro,z" \
  -v "${HERE}/deb:/out:z" \
  -e "DEBIAN_FRONTEND=noninteractive" \
  -e "PKG=${PKG}" -e "PKG_VERSION=${VERSION}" \
  "${BUILDER}" bash -s <<'INNER'
set -Eeuo pipefail

echo
echo "== target environment =="
# In a subshell: os-release defines VERSION and would clobber PKG_VERSION.
(. /etc/os-release && echo "  distribution: ${PRETTY_NAME}")
dpkg-query -W -f='  libfprint-tod-dev: ${Version}\n' libfprint-2-tod-dev
dpkg-query -W -f='  opencv:            ${Version}\n' libopencv-dev
echo "  tod driver dir:    $(pkgconf --variable=tod_driversdir libfprint-2-tod-1)"

# Every fpi_* symbol the driver uses must exist in this libfprint. The TOD
# symbol versions are the part that actually breaks across libfprint lines, so
# report them rather than discovering a mismatch after installation.
echo "  tod ABI versions:  $(readelf --version-info \
    /usr/lib/*/libfprint-2-tod.so.1 2>/dev/null \
    | grep -oE 'LIBFPRINT_TOD_[0-9_.]+' | sort -u | tr '\n' ' ')"
echo

BUILD=/tmp/build
mkdir -p "${BUILD}/${PKG}-${PKG_VERSION}"
# Only the files the package is built from. Copying the whole tree would drag
# in build outputs from other targets and the RPM tree.
for f in elan0c63-tod.c elan0c63-tod.h elan0c63-match.cpp elan0c63-match.h \
         meson.build meson_options.txt COPYING README.md; do
    [ -e "/src/$f" ] && cp "/src/$f" "${BUILD}/${PKG}-${PKG_VERSION}/" || true
done
cp -r /src/debian "${BUILD}/${PKG}-${PKG_VERSION}/"
chmod -R u+w "${BUILD}"

cd "${BUILD}/${PKG}-${PKG_VERSION}"

# 3.0 (quilt) wants an .orig tarball. Generate it from the same files, so the
# source package is self-consistent and can be rebuilt elsewhere.
tar -czf "../${PKG}_${PKG_VERSION}.orig.tar.gz" \
    --exclude=debian --transform "s,^\.,${PKG}-${PKG_VERSION}," .

dpkg-buildpackage -us -uc -b

cd ..
cp -v *.deb /out/
INNER

echo
echo "Verifying the package ..."
readonly VERIFIER="${BUILDER}-verify"
if ! podman image exists "${VERIFIER}" 2>/dev/null; then
  podman build -t "${VERIFIER}" -f - . >/dev/null <<CONTAINERFILE
FROM ${IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      gcc binutils pkgconf libfprint-2-tod-dev libfprint-2-dev
CONTAINERFILE
fi

# Deliberately a fresh container each time: the package must pull its own
# runtime dependencies in, and a builder image would already have them.
podman run --rm -i -v "${HERE}/deb:/deb:ro,z" -v "${HERE}:/src:ro,z" \
  -e "DEBIAN_FRONTEND=noninteractive" "${VERIFIER}" bash -s <<'INNER'
set -Eeuo pipefail
DEB=$(ls /deb/*.deb | head -1)

echo "== package contents =="
dpkg-deb -c "$DEB" | sed 's/^/  /'
echo
echo "== dependencies dpkg derived from the ELF =="
dpkg-deb -f "$DEB" Depends | tr ',' '\n' | sed 's/^ */  /'
echo
echo "== installing into a clean root =="
apt-get update -qq
apt-get install -y -qq --no-install-recommends "$DEB" >/dev/null
dpkg-query -W -f='  installed: ${Package} ${Version} ${Status}\n' libfprint-tod-elan0c63

M=$(dpkg-query -L libfprint-tod-elan0c63 | grep '\.so$')
echo "  module:    $M"
nm -D -u "$M" | grep -E '^[0-9a-f ]* U ' | sed 's/^.* U //; s/@.*//' | sort -u > /tmp/need
for L in $(ldd "$M" | sed -n 's/.*=> \([^ ]*\).*/\1/p'); do
    nm -D --defined-only "$L" 2>/dev/null | sed 's/.* //; s/@.*//'
done | sort -u > /tmp/have
MISSING=$(comm -23 /tmp/need /tmp/have | grep -v '^_ITM\|^__gmon\|^__cxa_finalize' || true)
if [ -n "$MISSING" ]; then
    echo "  UNRESOLVED SYMBOLS after a real install:"
    echo "$MISSING" | sed 's/^/    /'
    exit 1
fi
echo "  symbols:   all resolved against the installed dependencies"
echo "  missing libs: $(ldd "$M" | grep -c 'not found' || true)"

# Symbol lists cannot tell whether libfprint can actually instantiate the
# driver. Load it the way libfprint does and read the class back.
echo
echo "== load test on the installed module =="
cc -O1 -o /tmp/smoke /src/tod-smoke-test.c \
   $(pkg-config --cflags --libs libfprint-2-tod-1 libfprint-2) -ldl
/tmp/smoke "$M"
INNER

echo
echo "Done:"
ls -la "${HERE}/deb/"
