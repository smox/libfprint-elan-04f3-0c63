#!/usr/bin/bash
#
# Builds an installable RPM in a container.
#
# Nothing is installed or changed on the host; the finished package lands in
# rpm/ and then has to be installed deliberately with zypper.

set -Eeuo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly NAME="libfprint-tod-elan0c63"
readonly VERSION="0.1.0"
readonly IMAGE="elan0c63-build"

if ! podman image exists "${IMAGE}" 2>/dev/null; then
  echo "Container image missing. Run build.sh first." >&2
  exit 1
fi

rm -rf "${HERE}/rpm"
mkdir -p "${HERE}/rpm"

# Source archive built from exactly the files that go into the package.
readonly STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

mkdir -p "${STAGE}/${NAME}-${VERSION}"
for f in elan0c63-tod.c elan0c63-match.cpp elan0c63-match.h README.md COPYING; do
  cp "${HERE}/${f}" "${STAGE}/${NAME}-${VERSION}/"
done

tar -czf "${STAGE}/${NAME}-${VERSION}.tar.gz" \
    -C "${STAGE}" "${NAME}-${VERSION}"

echo "Building RPM ..."
podman run --rm \
  -v "${STAGE}:/stage:z" \
  -v "${HERE}:/src:z" \
  -v "${HERE}/rpm:/out:z" \
  "${IMAGE}" bash -eu -c '
    zypper --non-interactive install --no-recommends rpm-build binutils >/dev/null 2>&1

    mkdir -p /build/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
    cp /stage/*.tar.gz /build/SOURCES/
    cp /src/libfprint-tod-elan0c63.spec /build/SPECS/

    rpmbuild -ba \
      --define "_topdir /build" \
      /build/SPECS/libfprint-tod-elan0c63.spec 2>&1 | tail -25

    cp -r /build/RPMS/* /out/
    cp /build/SRPMS/* /out/
  '

echo
echo "Inspecting the package ..."
podman run --rm -v "${HERE}/rpm:/out:z" "${IMAGE}" bash -eu -c '
    RPM=$(ls /out/*/*.rpm | head -1)
    echo "  package:  $(basename "$RPM")"
    echo "  contents:"
    rpm -qlp "$RPM" 2>/dev/null | sed "s/^/    /"
    echo "  requires:"
    rpm -qRp "$RPM" 2>/dev/null | grep -vE "^rpmlib|^\(" | sort | sed "s/^/    /" | head -20
'

echo
echo "Done. Install with:"
echo "  sudo zypper --no-gpg-checks install ${HERE}/rpm/x86_64/${NAME}-${VERSION}-*.rpm"
echo
echo "Then let fprintd restart:"
echo "  sudo systemctl stop fprintd.service"
