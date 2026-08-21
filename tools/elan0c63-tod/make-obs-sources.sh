#!/usr/bin/bash
#
# Generates the source set that OBS needs to build this package for both rpm
# and deb targets, into obs/.
#
#   libfprint-tod-elan0c63-<version>.tar.gz   upstream source, both targets
#   libfprint-tod-elan0c63.spec               rpm recipe
#   libfprint-tod-elan0c63.dsc                deb recipe
#   debian.tar.gz                             the debian/ directory
#
# OBS picks the .spec for rpm repositories and the .dsc for deb repositories,
# so one package serves openSUSE and Ubuntu from the same sources.
#
# Upload with:
#   osc add obs/* && osc commit

set -Eeuo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly NAME="libfprint-tod-elan0c63"
readonly OUT="${HERE}/obs"

# Single source of truth for the version: the debian changelog. The spec is
# checked against it below rather than being a second place to edit.
DEB_VERSION="$(sed -n '1s/.*(\(.*\)).*/\1/p' "${HERE}/debian/changelog")"
VERSION="${DEB_VERSION%-*}"
readonly DEB_VERSION VERSION

SPEC_VERSION="$(sed -n 's/^Version: *//p' "${HERE}/${NAME}.spec")"
if [[ "${SPEC_VERSION}" != "${VERSION}" ]]; then
  echo "Error: spec says ${SPEC_VERSION}, debian/changelog says ${VERSION}." >&2
  echo "These must agree; OBS builds both from one tarball." >&2
  exit 1
fi

rm -rf "${OUT}"
mkdir -p "${OUT}"

# ---------------------------------------------------------------- tarball
#
# meson.build and meson_options.txt are included even though the spec builds
# with plain g++: debian/rules uses meson, and both targets unpack the same
# tarball.
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

mkdir -p "${STAGE}/${NAME}-${VERSION}"
for f in elan0c63-tod.c elan0c63-match.cpp elan0c63-match.h \
         meson.build meson_options.txt tod-smoke-test.c README.md COPYING; do
  cp "${HERE}/${f}" "${STAGE}/${NAME}-${VERSION}/"
done

# --sort=name and a fixed mtime keep the tarball byte-identical across runs, so
# OBS does not see a new source revision when nothing actually changed.
tar --sort=name --mtime="@0" --owner=0 --group=0 --numeric-owner \
    -czf "${OUT}/${NAME}-${VERSION}.tar.gz" -C "${STAGE}" "${NAME}-${VERSION}"

# ------------------------------------------------------------ debian.tar.gz
#
# DEBTRANSFORM-FILES-TAR unpacks this into the source root, so the paths inside
# have to start with debian/.
#
# debian/source/format is excluded on purpose. It says 3.0 (quilt) for the local
# build-deb.sh path, which generates its own .orig tarball, while the .dsc below
# declares Format: 1.0 because that is what OBS debtransform constructs. The
# file is only read when building a source package, never for the binary build
# OBS performs, so shipping both would be a contradiction with no upside.
tar --sort=name --mtime="@0" --owner=0 --group=0 --numeric-owner \
    --exclude='debian/source' \
    -czf "${OUT}/debian.tar.gz" -C "${HERE}" debian

# -------------------------------------------------------------------- spec
cp "${HERE}/${NAME}.spec" "${OUT}/"

# --------------------------------------------------------------------- dsc
#
# Derived from debian/control rather than written by hand: Build-Depends is the
# field most likely to drift, and a stale copy here fails on OBS only, where it
# is slowest to diagnose.
build_depends="$(awk '
  /^Build-Depends:/ { collecting = 1; sub(/^Build-Depends:[ \t]*/, ""); }
  collecting {
    gsub(/^[ \t]+/, "");
    printf "%s", $0;
    if (/,$/) { printf " "; } else { collecting = 0; print ""; }
    next
  }
' "${HERE}/debian/control")"

maintainer="$(sed -n 's/^Maintainer: *//p' "${HERE}/debian/control")"

cat > "${OUT}/${NAME}.dsc" <<DSC
Format: 1.0
Source: ${NAME}
Version: ${DEB_VERSION}
Binary: ${NAME}
Maintainer: ${maintainer}
Architecture: any
Build-Depends: ${build_depends}
DEBTRANSFORM-TAR: ${NAME}-${VERSION}.tar.gz
DEBTRANSFORM-FILES-TAR: debian.tar.gz
DSC

echo "Generated in ${OUT}:"
for f in "${OUT}"/*; do
  printf '  %-46s %8s bytes\n' "$(basename "$f")" "$(stat -c %s "$f")"
done
echo
echo "--- ${NAME}.dsc ---"
sed 's/^/  /' "${OUT}/${NAME}.dsc"
