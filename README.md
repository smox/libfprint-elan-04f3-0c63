# libfprint driver for the ELAN 04f3:0c63

A working fingerprint driver for a sensor that never worked reliably under
Linux — plus the measurements that explain why it didn't.

Found in the TUXEDO InfinityBook S 15 Gen6 and several other TUXEDO and Clevo
models.

```
Measured on one machine, 20 attempts:
  9 of 10 genuine attempts recognised     (stock driver: 1 of 5)
  0 of 10 other fingers accepted
```

That is a small sample. [What these numbers do and do not
support](#what-this-does-not-claim) is spelled out below.

## Is this your sensor?

```console
$ lsusb | grep 04f3
Bus 003 Device 002: ID 04f3:0c63 Elan Microelectronics Corp. ELAN:Fingerprint
```

You need exactly `04f3:0c63`. Other ELAN ids are different hardware; this
driver declares only this one and leaves every other device alone.

## What was wrong

**The sensor is too small for the algorithm libfprint uses on it.**

It is 80×80 px at a measured 498 dpi — 16.6 mm². libfprint matches with
NBIS/Bozorth, which needs at least ten minutiae. Cropping libfprint's own
reference fingerprint to decreasing sizes shows where that becomes possible:

| Edge | Area | Minutiae found | Enough for Bozorth |
|---:|---:|---:|---|
| 80 px | 17 mm² | 2 | no ← this sensor |
| 100 px | 26 mm² | 6 | no |
| **120 px** | **37 mm²** | **13** | **yes** |
| 200 px | 104 mm² | 26 | yes |

Across 123 captures from two people and eight fingers, **not one capture ever
produced more than four minutiae.**

libfprint works around this by asking you to *swipe* and stitching the frames
into a taller image. That is the only reason minutiae exist here at all — and
also why they are unstable. For this device the stitching:

- discards 30 of the 80 rows of every frame (`ELAN_MAX_FRAME_HEIGHT` is 50)
- cannot represent a stationary finger — the motion search starts at `dy = 2`,
  so a press is stretched into a strip
- leaves a third of its output image empty

This driver takes the route the vendor's own Windows driver takes for the same
device: keep the native press capture and match local SIFT descriptors with a
RANSAC geometry check instead of minutiae. Same images, a median of 142 usable
features instead of 1.

Related upstream reports: [work item 817](https://gitlab.freedesktop.org/libfprint/libfprint/-/work_items/817)
(calibration failures), [work item 549](https://gitlab.freedesktop.org/libfprint/libfprint/-/work_items/549)
and [issue 272](https://gitlab.freedesktop.org/libfprint/libfprint/-/issues/272)
(small ELAN sensors), [MR !217](https://gitlab.freedesktop.org/libfprint/libfprint/-/merge_requests/217)
(calibration polling window).

## Install

Currently packaged for **openSUSE Tumbleweed** only, because that is what can
actually be verified here. Build the package yourself:

```bash
tools/elan0c63-tod/build.sh        # builds the module in a container
tools/elan0c63-tod/build-rpm.sh    # builds an installable RPM
sudo zypper --no-gpg-checks install tools/elan0c63-tod/rpm/x86_64/libfprint-tod-elan0c63-*.rpm
sudo systemctl stop fprintd.service
```

Nothing is installed on the host during the build; it runs in a container. The
package itself installs exactly one file into `/usr/lib64/libfprint-2/tod-1/`.
It replaces no system library and changes no configuration. It does pull in the
OpenCV runtime as a dependency.

Check that it took over:

```console
$ fprintd-list "$USER"
... for ElanTech 04f3:0c63 (descriptor matching).
```

If you instead see `ElanTech Fingerprint Sensor (swipe)`, the stock driver is
still active — stop `fprintd` again.

**No allowlist is needed.** libfprint scores every driver that claims a device
and picks the highest (`fp-context.c`, `usb_device_added_cb`); the default is
50. This driver reports 90 through `usb_discover`, but only for `04f3:0c63`.

### Uninstall

```bash
sudo zypper remove libfprint-tod-elan0c63
sudo systemctl stop fprintd.service
```

The stock driver takes over immediately. Nothing else was modified, so there is
nothing else to undo. Prints enrolled here live in
`/var/lib/fprint/<user>/elan0c63/` and do not interfere with the stock driver,
which uses its own directory.

### Other distributions

Only Tumbleweed is packaged and tested. The source is two files and about 900
lines, so building elsewhere is not hard — you need libfprint with TOD support,
fprintd, and OpenCV. Note that Debian and Ubuntu ship a different libfprint
version line (`1.95.x+tod1`), which needs a rebuild rather than the same
binary. Open an issue saying which distribution you want.

## Testing

```bash
tools/elan0c63-tod/fingerprint-test.sh
tools/elan0c63-tod/fingerprint-test.sh --rounds 10 --finger left-index-finger
```

Walks you through enrolment, genuine verifications, and a counter-check with
**other** fingers that must all be rejected. Then writes a report with match
scores, feature counts, calibration values, errors, and your model and package
versions.

The report contains **no fingerprint image and no biometric template** — the
scores are counts of geometrically confirmed feature correspondences. Read it
before sharing it anyway.

One thing that matters more than it sounds: **during enrolment, place your
finger normally each time — do not try to hit the same spot.** The sensor sees
about 4×4 mm, so the twelve stages are meant to cover different parts of your
fingertip. That was the strongest single lever measured: 5 enrolment stages gave
67 % recognition, 14 gave 88.5 %, and the curve had not flattened.

## What this does not claim

- **Small sample.** 20 verification attempts on one laptop by one person. The
  offline analysis behind it covers 123 captures from two people — still small.
- **No false-accept rate.** With eight fingers there are only 28 independent
  finger pairs. Zero false accepts were observed, but that does not support any
  figure like "1 in 50,000". Anyone claiming one from a sample this size is
  guessing.
- **Two genuine matches scored 8 and 10** against a threshold of 5. Closer than
  "9 out of 10" makes it sound.
- **Untested:** long-term use, wet or cold fingers, PAM login. Fingerprint
  login has not been enabled here.

If it fails on your machine, that is a useful result — please open an issue with
the report.

## Repository layout

| Path | Contents |
|---|---|
| `tools/elan0c63-tod/` | the driver, RPM spec, build and test scripts |
| `tools/fpcapture/` | capture and offline analysis tooling |
| `upstream/patches/` | three independent libfprint fixes, not yet submitted |
| `scripts/` | load the driver in a temporary fprintd without installing it |

The `upstream/patches/` series is separate from this driver and applies to
libfprint master (`c4654fdc`): a non-injective byte decoding in the ELAN
calibration path, an uninitialised `FpImage.ppmm`, and enrolment accepting
samples the matcher can never score. All three pass libfprint's test suite
(37 passed, 0 failed, 2 expected skips) and its uncrustify check.

`tools/fpcapture/` is how the numbers in this README were produced. If you
want to check a constant rather than take it on trust, that is the harness —
it captures raw frames over USB without libfprint in the way, and its
resolution measurement has a self-test against patterns of known period.

No biometric data is in this repository. Raw captures live outside it under
`/var/lib/fprint-research`, and the capture tool refuses to write anywhere
inside a repository.

## Credits and licence

The USB protocol is taken from libfprint's in-tree `elan` driver
(© 2017 Igor Filatov, LGPL-2.1+). This driver is LGPL-2.1-or-later.

The code was written with Claude (Anthropic) over two working sessions. Every
hardware measurement was run and reviewed by a human, who is responsible for
the result. This is stated openly so the work can be judged on its
measurements.
