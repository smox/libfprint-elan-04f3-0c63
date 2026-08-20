#!/usr/bin/env python3
"""Offline analysis of raw captures.

Works exclusively on stored ``.npz`` files and never touches the sensor. Only
derived figures are printed, never image data itself.

Answers three questions:

1. How good is the capture at all? (contrast, signal-to-noise)
2. What resolution does the sensor really have?
3. Are there defective pixels that would produce the same spurious structure
   in every image?

Usage::

    tools/.venv/bin/python tools/fpcapture/analyze.py /path/to/sample-001.npz
    tools/.venv/bin/python tools/fpcapture/analyze.py --png /path/sample-001.npz
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# Ridge spacing of an adult finger. The literature gives 0.4 to 0.5 mm from
# ridge centre to ridge centre. We use the midpoint and report the span as
# uncertainty.
RIDGE_PERIOD_MM = 0.45
RIDGE_PERIOD_MM_MIN = 0.40
RIDGE_PERIOD_MM_MAX = 0.50

MM_PER_INCH = 25.4


def load_sample(path: Path) -> tuple[np.ndarray, np.ndarray, dict]:
    with np.load(path) as data:
        frames = data["frames"].astype(np.float64)
        background = data["background"].astype(np.float64)
        metadata = json.loads(str(data["metadata"]))
    return frames, background, metadata


def signal_image(
    frames: np.ndarray,
    background: np.ndarray,
    mode: str = "best",
) -> np.ndarray:
    """Turn several frames of one press into a single image.

    This originally averaged over all frames, reasoned as reducing sensor noise
    by the square root of the frame count. Measurement against the corpus
    refutes that: the sharpest single frame beats the mean in 120 of 123
    captures, by a median factor of 1.26.

    The reason is movement. Over the eight frames the skin deforms and the mean
    smears the ridges. Aligning by cross-correlation did not help either
    (factor 0.96), which is what marks the deformation as elastic rather than
    rigid.

    ``mode``:

    * ``best``    - sharpest single frame, judged by ridge clarity
    * ``mean``    - the old average, kept only for comparison
    * ``median``  - robust against single outliers, but equally unsharp
    """
    signals = frames - background

    if mode == "mean":
        return signals.mean(axis=0)
    if mode == "median":
        return np.median(signals, axis=0)
    if mode != "best":
        raise ValueError(f"Unknown mode: {mode}")

    if signals.shape[0] == 1:
        return signals[0]

    scores = [ridge_frequency(frame).get("prominence", 0.0) for frame in signals]
    return signals[int(np.argmax(scores))]


def quality_metrics(signal: np.ndarray, frames: np.ndarray) -> dict:
    """Figures describing capture quality."""
    p1, p99 = np.percentile(signal, [1, 99])
    contrast = float(p99 - p1)

    # Noise: spread between frames at the same position. The finger is still,
    # so any deviation is noise.
    if frames.shape[0] > 1:
        noise = float(frames.std(axis=0).mean())
    else:
        noise = float("nan")

    snr = contrast / noise if noise and noise == noise and noise > 0 else float("nan")

    # Coverage: fraction of the area carrying appreciable signal.
    threshold = p1 + 0.15 * contrast
    coverage = float((signal > threshold).mean())

    return {
        "contrast": contrast,
        "noise": noise,
        "snr": snr,
        "coverage": coverage,
    }


def frame_stability(frames: np.ndarray, background: np.ndarray) -> dict:
    """Check how still the finger was during the frames.

    In a press the finger rests, so all frames should be nearly identical. If
    some deviate, the finger settled, moved, or the contact was unstable. Such
    frames make the mean worse rather than better.

    Each frame is compared against the median of all frames. The median is
    insensitive to single outliers and therefore a robust reference.
    """
    if frames.shape[0] < 3:
        return {"valid": False}

    signals = frames - background
    reference = np.median(signals, axis=0)

    reference_centred = reference - reference.mean()
    reference_norm = np.linalg.norm(reference_centred)

    correlations = []
    for frame in signals:
        centred = frame - frame.mean()
        norm = np.linalg.norm(centred)
        if norm == 0 or reference_norm == 0:
            correlations.append(0.0)
        else:
            correlations.append(
                float(np.dot(centred.ravel(), reference_centred.ravel())
                      / (norm * reference_norm))
            )

    values = np.array(correlations)

    # Outliers against the robust spread of the correlations.
    median_correlation = float(np.median(values))
    mad = float(np.median(np.abs(values - median_correlation)))
    threshold = median_correlation - max(3 * 1.4826 * mad, 0.02)
    outliers = [int(i) for i in np.nonzero(values < threshold)[0]]

    # If the finger settles over time, later frames should match the median
    # better than earlier ones. Single outliers would distort that comparison -
    # depending on which half they fall in, a trend appears that does not exist.
    # So clean them out first.
    keep = np.ones(len(values), dtype=bool)
    keep[outliers] = False
    indices = np.nonzero(keep)[0]

    trend = 0.0
    trend_significant = False
    if len(indices) >= 4:
        half = len(indices) // 2
        early, late = indices[:half], indices[half:]
        trend = float(values[late].mean() - values[early].mean())

        # A difference between two means only means something if it clearly
        # exceeds the noise of the individual values. With eight frames the
        # statistics are thin; without this check pure noise already reports an
        # apparent trend. Better to say nothing than something false.
        scatter = float(values[indices].std(ddof=1))
        standard_error = scatter * np.sqrt(1 / len(early) + 1 / len(late))
        trend_significant = bool(
            standard_error > 0 and abs(trend) > 2 * standard_error
        )

    return {
        "valid": True,
        "correlations": [float(v) for v in values],
        "worst": float(values.min()),
        "best": float(values.max()),
        "spread": float(values.max() - values.min()),
        "trend": trend,
        "trend_significant": trend_significant,
        "outliers": outliers,
    }


def ridge_frequency(signal: np.ndarray) -> dict:
    """Determine the dominant ridge frequency via a 2D Fourier transform.

    A fingerprint is approximately a periodic stripe pattern. In the power
    spectrum it appears as a ring around the origin, and that ring's radius
    tells how many pixels fall on one ridge period.
    """
    image = signal - signal.mean()

    # Windowing against edge artefacts: without it the hard image border
    # produces a cross in the spectrum that can mask the real ring.
    window = np.outer(np.hanning(image.shape[0]), np.hanning(image.shape[1]))
    spectrum = np.abs(np.fft.fftshift(np.fft.fft2(image * window))) ** 2

    height, width = spectrum.shape
    cy, cx = height // 2, width // 2
    yy, xx = np.ogrid[:height, :width]
    radius = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)

    max_radius = min(cy, cx)
    radial = np.zeros(max_radius)
    for r in range(max_radius):
        ring = (radius >= r) & (radius < r + 1)
        if ring.any():
            radial[r] = spectrum[ring].mean()

    # Very low frequencies are brightness gradient, not ridges; very high ones
    # are noise. At 80 px, r=3..24 covers ridge periods of roughly 23 to 3
    # pixels - the physically plausible range.
    lo, hi = 3, min(24, max_radius)
    band = radial[lo:hi]
    if band.size == 0 or not np.isfinite(band).any():
        return {"valid": False}

    peak_radius = int(np.argmax(band)) + lo

    # The ring radius is integral while the real frequency lies between bins.
    # Uncorrected, the quantisation error at a period of ten pixels is already
    # around eleven per cent. A parabola through the peak and its two
    # neighbours gives the intermediate position, on a log scale because power
    # spectra fall off exponentially.
    refined_radius = float(peak_radius)
    if lo < peak_radius < hi - 1:
        left, centre, right = (
            np.log(radial[peak_radius - 1] + 1e-30),
            np.log(radial[peak_radius] + 1e-30),
            np.log(radial[peak_radius + 1] + 1e-30),
        )
        denominator = left - 2 * centre + right
        if denominator != 0:
            offset = 0.5 * (left - right) / denominator
            # A sensible peak lies within its own bin.
            if abs(offset) <= 0.5:
                refined_radius = peak_radius + offset

    # Half-bin correction. Bin r collects all radii in [r, r+1) and therefore
    # represents radius r+0.5, not r. Without this the method systematically
    # overestimates the ridge period by 3 to 10 per cent; checked against
    # synthetic patterns of known period, the mean error drops from 6.9 to
    # 1.3 per cent.
    period_px = image.shape[0] / (refined_radius + 0.5)

    # How clearly does the ring stand out? Without a real ridge pattern the
    # spectrum is flat and the value near 1.
    prominence = float(radial[peak_radius] / np.median(band))

    def to_dpi(period_mm: float) -> float:
        return period_px / period_mm * MM_PER_INCH

    return {
        "valid": True,
        "peak_radius": refined_radius,
        "period_px": float(period_px),
        "prominence": prominence,
        "dpi": to_dpi(RIDGE_PERIOD_MM),
        "dpi_min": to_dpi(RIDGE_PERIOD_MM_MAX),
        "dpi_max": to_dpi(RIDGE_PERIOD_MM_MIN),
    }


def defective_pixels(background: np.ndarray, sigma: float = 6.0) -> dict:
    """Find pixels that are persistently out of line in the empty image.

    Such pixels produce the same structure at the same place in every image.
    To a matcher that looks like a genuine feature every person shares - a
    possible driver of false impostor matches.
    """
    median = np.median(background)
    # Robust spread: insensitive to the outliers we are looking for.
    mad = np.median(np.abs(background - median))
    robust_std = 1.4826 * mad

    if robust_std == 0:
        return {"count": 0, "fraction": 0.0, "positions": [], "robust_std": 0.0}

    deviation = np.abs(background - median) / robust_std
    mask = deviation > sigma
    ys, xs = np.nonzero(mask)

    height, width = background.shape

    # Geometry of the anomalies. Scattered single pixels are something quite
    # different from a systematic border: single pixels get interpolated away,
    # a border gets cropped off.
    row_counts = mask.sum(axis=1)
    column_counts = mask.sum(axis=0)

    # How many anomalies disappear if a border of width m is excluded all
    # round?
    margin_effect = []
    for margin in range(0, 9):
        if margin == 0:
            remaining = int(mask.sum())
        else:
            inner = mask[margin:height - margin, margin:width - margin]
            remaining = int(inner.sum())
        kept_area = (height - 2 * margin) * (width - 2 * margin)
        margin_effect.append(
            {
                "margin": margin,
                "remaining": remaining,
                "kept_pixels": kept_area,
                "kept_fraction": kept_area / (height * width),
            }
        )

    return {
        "count": int(mask.sum()),
        "fraction": float(mask.mean()),
        "robust_std": float(robust_std),
        "max_deviation": float(deviation.max()),
        "positions": [(int(y), int(x)) for y, x in zip(ys[:20], xs[:20])],
        "affected_rows": [int(i) for i in np.nonzero(row_counts)[0]],
        "affected_columns": [int(i) for i in np.nonzero(column_counts)[0]],
        "full_rows": [int(i) for i in np.nonzero(row_counts == width)[0]],
        "full_columns": [int(i) for i in np.nonzero(column_counts == height)[0]],
        "margin_effect": margin_effect,
    }


def export_png(signal: np.ndarray, destination: Path) -> None:
    """Write a greyscale image for visual inspection.

    Intended for the user's own check only. The file contains biometric data
    and is therefore written into the corpus directory with mode 0600.
    """
    from PIL import Image

    lo, hi = np.percentile(signal, [1, 99])
    scaled = np.clip((signal - lo) / max(hi - lo, 1e-9), 0, 1)
    # Ridges touch the sensor and give high values; inverted they read dark,
    # like a classic inked print.
    image = Image.fromarray(((1 - scaled) * 255).astype(np.uint8), mode="L")
    image = image.resize((signal.shape[1] * 4, signal.shape[0] * 4),
                         Image.NEAREST)
    image.save(destination)
    destination.chmod(0o600)


def report(path: Path, want_png: bool) -> None:
    frames, background, metadata = load_sample(path)
    signal = signal_image(frames, background)

    print()
    print(f"  capture: {path.name}")
    print(f"  subject {metadata['subject']}, finger {metadata['finger']}, "
          f"{metadata['frames']} frames")
    print()

    quality = quality_metrics(signal, frames)
    print("  Capture quality")
    print(f"    contrast:           {quality['contrast']:8.1f}")
    print(f"    noise:              {quality['noise']:8.1f}")
    print(f"    signal-to-noise:    {quality['snr']:8.1f}")
    print(f"    coverage:           {quality['coverage'] * 100:8.1f} per cent")
    print()

    stability = frame_stability(frames, background)
    print("  Frame stability (did the finger stay still?)")
    if not stability["valid"]:
        print("    Too few frames to say anything.")
    else:
        bars = " ".join(f"{v:.3f}" for v in stability["correlations"])
        print(f"    agreement per frame: {bars}")
        print(f"    worst {stability['worst']:.3f}, "
              f"best {stability['best']:.3f}, "
              f"span {stability['spread']:.3f}")

        if stability["outliers"]:
            print(f"    outliers: frame {stability['outliers']}")
        if not stability["trend_significant"]:
            print("    No demonstrable trend across the frames.")
        elif stability["trend"] > 0:
            print(f"    Trend: later frames are better "
                  f"(+{stability['trend']:.3f}). The finger is still settling;")
            print("    the first frames should be discarded.")
        else:
            print(f"    Trend: later frames are worse "
                  f"({stability['trend']:.3f}). The finger is slipping or")
            print("    the contact is fading; discard late frames.")
    print()

    ridge = ridge_frequency(signal)
    print("  Resolution from the ridge frequency")
    if not ridge["valid"]:
        print("    No usable pattern found.")
    elif ridge["prominence"] < 1.5:
        print(f"    No clear ridge pattern (prominence "
              f"{ridge['prominence']:.2f}, need > 1.5).")
        print("    Probably too weak a finger contact.")
    elif ridge["prominence"] < 50:
        # Empirical from the first measurements: good captures reach
        # prominences above 100. Below that the spectral peak drifts
        # noticeably into the noise and the period is underestimated.
        print(f"    Weak ridge pattern (prominence "
              f"{ridge['prominence']:.1f}; good captures exceed 100).")
        print(f"    period {ridge['period_px']:.1f} px, giving "
              f"{ridge['dpi']:.0f} dpi - but unreliable.")
        print("    Do not use this capture to determine resolution.")
    else:
        print(f"    ridge period:       {ridge['period_px']:8.1f} px")
        print(f"    prominence:         {ridge['prominence']:8.2f}")
        print(f"    estimated resolution: {ridge['dpi']:6.0f} dpi "
              f"(range {ridge['dpi_min']:.0f} to {ridge['dpi_max']:.0f})")
        print()
        deviation = abs(ridge["dpi"] - 500) / 500
        if deviation < 0.15:
            print("    -> consistent with the 500 dpi NBIS assumes.")
        else:
            print(f"    -> deviates from 500 dpi by {deviation * 100:.0f} per cent.")
            print("       That would detune the NBIS minutiae parameters.")
    print()

    defects = defective_pixels(background)
    print("  Anomalous pixels in the empty image")
    print(f"    count:              {defects['count']:8d} of "
          f"{background.size} ({defects['fraction'] * 100:.2f} per cent)")
    if defects["count"]:
        print(f"    largest deviation:  {defects['max_deviation']:8.1f} sigma")
        print()
        def summarise(name: str, indices: list[int], total: int) -> None:
            if not indices:
                print(f"      {name:<22} keine")
            elif len(indices) <= 12:
                print(f"      {name:<22} {indices}")
            else:
                print(f"      {name:<22} {len(indices)} of {total} "
                      f"(from {min(indices)} to {max(indices)})")

        height, width = background.shape
        print("    Geometry:")
        summarise("affected rows:", defects["affected_rows"], height)
        summarise("affected columns:", defects["affected_columns"], width)
        summarise("complete rows:", defects["full_rows"], height)
        summarise("complete columns:", defects["full_columns"], width)
        print()
        print("    Effect of excluding a border:")
        print(f"      {'border':>6} {'remaining':>12} {'area left':>12}")
        for entry in defects["margin_effect"]:
            print(f"      {entry['margin']:6d} {entry['remaining']:12d} "
                  f"{entry['kept_fraction'] * 100:11.1f}%")
        print()

        clean = next(
            (e for e in defects["margin_effect"] if e["remaining"] == 0), None
        )
        if clean:
            print(f"    -> A border of {clean['margin']} px removes every anomaly")
            print(f"       and keeps {clean['kept_fraction'] * 100:.1f} per cent "
                  f"of the area ({clean['kept_pixels']} px).")
            libfprint_pixels = 50 * background.shape[1]
            print(f"       For comparison, libfprint keeps "
                  f"{libfprint_pixels} px "
                  f"({libfprint_pixels / background.size * 100:.1f} per cent).")
        else:
            print("    -> No border up to 8 px clears everything.")
            print("       So there are scattered single defects inside as well,")
            print("       which need interpolating rather than cropping.")
    else:
        print("    None. The sensor is uniform.")
    print()

    if want_png:
        destination = path.with_suffix(".png")
        export_png(signal, destination)
        print(f"  visual check written: {destination}")
        print("  (in the protected corpus, not in the repository)")
        print()


def selftest() -> int:
    """Check the resolution measurement against patterns of known period.

    A measuring tool that was never checked against a known truth is not a
    measuring tool. This very test uncovered a systematic half-bin offset that
    reported the period as much as ten per cent too large.
    """
    rng = np.random.default_rng(42)
    periods = [5.0, 6.0, 7.0, 8.0, 9.0, 9.5, 10.0, 11.0, 12.0, 14.0]
    errors = []

    print()
    print("  Self-test of the ridge frequency measurement")
    print(f"  {'given':>12} {'measured':>10} {'error':>9}")

    for true_period in periods:
        y, x = np.mgrid[0:80, 0:80]
        angle = np.deg2rad(25)
        projection = x * np.cos(angle) + y * np.sin(angle)
        image = 500 * np.sin(2 * np.pi * projection / true_period)
        image = image + rng.normal(0, 80, image.shape)
        image = image + 300 * np.exp(-((x - 40) ** 2 + (y - 40) ** 2) / 2000)

        measured = ridge_frequency(image)["period_px"]
        error = abs(measured - true_period) / true_period * 100
        errors.append(error)
        print(f"  {true_period:12.1f} {measured:10.2f} {error:8.1f}%")

    noise_prominence = ridge_frequency(rng.normal(0, 100, (80, 80)))["prominence"]

    print()
    print(f"  mean error:    {np.mean(errors):.1f} per cent")
    print(f"  largest error: {max(errors):.1f} per cent")
    print(f"  noise control: prominence {noise_prominence:.2f} "
          f"(must be below 1.5)")
    print()

    ok = max(errors) < 5.0 and noise_prominence < 1.5
    print(f"  result: {'passed' if ok else 'FAILED'}")
    print()
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline analysis of a raw capture.")
    parser.add_argument("samples", nargs="*", type=Path)
    parser.add_argument("--png", action="store_true",
                        help="write a greyscale image for visual inspection")
    parser.add_argument("--selftest", action="store_true",
                        help="check the method against known patterns")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if not args.samples:
        parser.error("Give at least one capture, or --selftest.")

    for path in args.samples:
        if not path.exists():
            print(f"Not found: {path}", file=sys.stderr)
            return 1
        report(path, args.png)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
