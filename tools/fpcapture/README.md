# fpcapture - capture and offline analysis

Tooling that talks to the ELAN 04f3:0c63 directly over USB, so raw frames can
be captured and analysed without libfprint in the way.

## Why this exists

Before this, every experiment cost a hardware session with a real finger, and
two algorithms could never be compared on the same image. These tools separate
**capture** from **analysis**: frames are recorded once, then any image
processing can run over them offline as often as needed.

That is what made several thousand impostor comparisons possible - and with
them a defensible false-accept figure rather than an assertion.

## How it differs from libfprint

It speaks the ELAN USB protocol directly. Three deliberate departures from the
in-tree driver, each with a reason found by measurement:

| in-tree driver | here | why |
|---|---|---|
| crops to 50 of 80 rows | keeps all 80 | 37.5 % of the sensor area was being discarded |
| stitches frames into a swipe | stores each frame separately | the assembly invents motion that never happened |
| normalises to 8 bit | stores raw 14-bit values | keeps information available to later methods |

The protocol itself comes from the open-source libfprint driver
(`libfprint/drivers/elan.c`, LGPL-2.1+).

## Requirements

The Python environment lives in `tools/.venv` and is separate from the system.
Nothing is installed on the host.

```bash
python3 -m venv tools/.venv
tools/.venv/bin/pip install pyusb numpy scipy pillow opencv-python-headless
```

The USB device is only accessible to root, so both capture scripts need
`sudo`. A running `fprintd` holds the device and has to be stopped first.

## First contact - captures no finger

```bash
sudo tools/.venv/bin/python tools/fpcapture/probe.py
```

Reads firmware, sensor dimensions and the calibration values of the empty
sensor. Stores nothing, changes nothing. Also answers whether the sensor
really reports 80x80.

## Recording a corpus

```bash
sudo tools/.venv/bin/python tools/fpcapture/capture_corpus.py \
    --subject alice --samples 15 \
    --fingers right-index right-middle left-index left-middle
```

On a person's first capture it asks for explicit consent and records it in
`CONSENT.json`.

## Data protection

These rules are enforced in code, not merely documented:

- The output path **must not** lie inside the project directory;
  `guard_output_path()` aborts otherwise. Raw captures can therefore never end
  up in a git repository.
- Default location is `/var/lib/fprint-research/corpus`, mode 0700, owned by
  the calling user rather than root.
- Individual captures are written with mode 0600.
- No transfer to any external service, in any form.
- Deletable on request at any time, and after the project ends.

## File format

One capture is an `.npz` archive with three entries:

| Entry | Contents |
|---|---|
| `frames` | `uint16` array `(n, 80, 80)`, raw ADC values |
| `background` | `uint16` array `(80, 80)`, empty-sensor reference of the same session |
| `metadata` | JSON: subject, finger, timestamp, firmware, calibration delta |

## Analysis

```bash
tools/.venv/bin/python tools/fpcapture/analyze.py --selftest
tools/.venv/bin/python tools/fpcapture/analyze.py /path/to/sample-001.npz
```

`analyze.py` measures capture quality, sensor resolution via ridge frequency,
frame stability and sensor artefacts.

The self-test checks the resolution measurement against synthetic patterns of
known period and must pass before any analysis is trusted. It has already
caught a systematic half-bin offset in the method itself, which had been
overestimating the ridge period by up to 10 %.

## Matcher

`matcher.py` implements the descriptor comparison that replaces the
minutiae-based NBIS/Bozorth path. The reason: all 123 corpus captures yield
between 0 and 4 minutiae, while Bozorth needs ten.

Every default in `MatcherConfig` was measured against the corpus and confirmed
by cross-validation over both subjects. They are **not** free choices; any
deviation has to be re-measured. The comments name the measurement for each
value.

Result with a gallery of 10 and a threshold of 5:

```text
recognition at zero false accepts:        82.6 %
highest impostor score over 6,615 pairs:      4   (genuine reaches 66)
in-tree libfprint for comparison:          20 %   (1 of 5)
```

The limits of that statement are in the research journal: the comparisons come
from only eight fingers, the independent unit is the finger pair, and there are
28 of those. A false-accept rate anywhere near commercial figures **cannot** be
derived from this.

## NBIS test harness

The minutiae counter under `nbisbench` (container) builds libfprint's own
unmodified NBIS sources and calls the same `get_minutiae()` with the same
parameters. Validated against `tests/elan/capture.png`: 34 minutiae, exactly
what the library itself reports there.
