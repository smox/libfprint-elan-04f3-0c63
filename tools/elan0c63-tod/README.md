# The driver

Implementation notes. For what this is, why it exists, and how to install it,
see the [top-level README](../../README.md).

## Files

| File | Contents |
|---|---|
| `elan0c63-tod.c` | USB protocol, state machines, enrol and verify |
| `elan0c63-match.cpp` | feature extraction and comparison via OpenCV |
| `elan0c63-match.h` | the C interface between the two |
| `verify-against-python.cpp` | offline check against the Python reference |
| `build.sh` | container build with symbol verification |
| `build-rpm.sh` | builds an installable RPM |
| `libfprint-tod-elan0c63.spec` | RPM spec |
| `meson.build` | alternative build path |

The USB protocol is taken from libfprint's in-tree `elan` driver
(`libfprint/drivers/elan.c`, (c) 2017 Igor Filatov, LGPL-2.1+). This driver is
LGPL-2.1-or-later.

## Why a separate driver rather than a patch

`FpImageDevice` hands every capture to NBIS and compares with Bozorth;
`fpi-image-device.c` calls `fpi_print_bz3_match()` directly, with no hook for
an alternative matcher. An image device therefore cannot match differently.

The way out is the one the match-on-chip drivers take: implement `FpDevice`
directly instead of `FpImageDevice`. The similarity is structural only - MOC
sensors compare inside the chip, this driver compares in the driver. What they
share is bypassing the hard-wired NBIS path.

Loading happens through TOD. The loader looks for the symbol
`fpi_tod_shared_driver_get_type` in every module (`tod-shared-loader.c`); that
is all an external driver needs. `libfprint-tod-devel` provides
`drivers_api.h` and the private headers.

## What it does differently

| | in-tree `elan` | this driver |
|---|---|---|
| Interaction | swipe | press |
| Rows per frame | 50 of 80 | all 80, minus a 3 px border |
| Image assembly | frames stitched | sharpest single frame |
| Features | NBIS minutiae (median 1) | SIFT keypoints (median 151) |
| Geometry check | Bozorth | RANSAC similarity fit |
| Enrolment stages | 5 | 12 |

## Building

```bash
./build.sh        # the module, in a container
./build-rpm.sh    # an installable RPM
```

Both run in a container; nothing is installed on the host. `build.sh` then
checks that the TOD entry point is exported and that every undefined symbol
resolves against a linked library - otherwise the driver would only fail at
runtime.

## About the constants

Every value in `elan0c63-match.cpp`, and the thresholds in
`elan0c63-match.h`, was measured against a corpus of 123 captures and
confirmed by cross-validation across two people. They are **not** free
choices. The comments in the code name the measurement behind each value and,
where applicable, the wrong value assumed beforehand.

Two examples of why that matters:

- The SIFT contrast threshold was initially lowered to 0.02, reasoned as "the
  image is small and low contrast". That reasoning is plausible and wrong: a
  low threshold admits keypoints made of noise, which carry no identity and
  match a stranger's finger as readily as one's own. It cost 50 percentage
  points of recognition.
- Descriptors are stored as `uint8`. That is not lossy-but-acceptable here, it
  is **lossless** - every value in the corpus was already an integer between 0
  and 220. Verified, not assumed.

Change a value and you have to re-measure. The harness for that is in
[`../fpcapture/`](../fpcapture/).

## Portability

The prototype ran on OpenCV 5.0, the target system has 4.13. SIFT differs
noticeably between them: only 10 of 123 captures yield the same keypoint
count. The end result is unchanged (85.4 % against 84.6 %), so the approach
does not depend on a particular OpenCV version.

To re-check that after an OpenCV update, export a corpus and run
`verify-against-python.cpp` against it.
