#!/usr/bin/env python3
"""Descriptor-based matcher for the ELAN 04f3:0c63.

Replaces the minutiae-based NBIS/Bozorth path, which is demonstrably impossible
on this sensor: all 123 corpus captures yield between 0 and 4 minutiae, while
Bozorth needs ten.

The approach follows the architecture that analysis of the Windows driver for
this exact device revealed:

1. native press captures instead of synthetically assembled swipes
2. local scale- and rotation-robust keypoints instead of minutiae
3. geometric verification of correspondences via RANSAC
4. score from the number of geometrically confirmed correspondences

Every preprocessing decision was measured against the corpus, not guessed:

* sharpest single frame instead of the mean (factor 1.26; 120 of 123 captures)
* a 3 px border removed (eliminates all 231 sensor artefacts)
* no alignment before combining (does not help; the deformation is elastic)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from analyze import load_sample, signal_image

# From the defect survey: rows 0 and 1 are fully affected, 2 and 3 partially.
# A 3 px border removes all 231 anomalies and keeps 5476 of 6400 pixels.
BORDER = 3


@dataclass
class MatcherConfig:
    """Every knob in one place, so measurement series stay reproducible."""

    # EVERY value here was measured against the corpus, not chosen. Selection
    # was by cross-validation: parameters picked on one subject, verified on the
    # other. Both directions produced the same core values.

    # After the border crop the native capture is 74x74. A little upscaling
    # gives SIFT extra octaves. More is not better: 4x dropped from 82.1 % to
    # 69.9 % against 2x.
    upscale: int = 2

    # CLAHE lifts local contrast without amplifying global brightness
    # differences. Measured: 2.0 and 3.0 equivalent, 5.0 worse.
    clahe_clip: float = 3.0
    clahe_grid: int = 8

    sift_features: int = 0          # 0 = unbegrenzt
    sift_octave_layers: int = 3

    # CAUTION - this was a mistake. An earlier version lowered this to 0.02,
    # reasoned as "the image is small and low contrast". That is wrong: a low
    # threshold admits keypoints made of noise, which carry no identity and
    # match a stranger's finger as readily as one's own, so they only lift the
    # impostor scores.
    #   0.02 -> recognition 19.5 per cent
    #   0.04 -> recognition 69.9 per cent   (OpenCV default)
    sift_contrast: float = 0.04
    sift_edge: float = 12.0
    sift_sigma: float = 1.2

    # Lowe ratio test. The SIGFM draft !530 uses 0.75; 0.70 measured better here
    # (84.6 against 82.1 per cent at zero false accepts). From 0.85 the
    # separation collapses to 25.2 per cent.
    ratio: float = 0.70

    # Geometric verification. A finger is placed shifted and rotated but not
    # perspectively distorted, so a similarity transform (rotation, translation,
    # slight scale) is the right model. The threshold is equivalent anywhere
    # between 3 and 10 pixels.
    ransac_threshold: float = 3.0
    ransac_min_matches: int = 4

    def fingerprint(self) -> str:
        """Short identifier of the configuration, for logs."""
        return (f"up{self.upscale}_cl{self.clahe_clip}_ct{self.sift_contrast}"
                f"_ed{self.sift_edge}_sg{self.sift_sigma}_r{self.ratio}"
                f"_rt{self.ransac_threshold}")


@dataclass
class Sample:
    """One prepared capture with its descriptors."""

    subject: str
    finger: str
    index: int
    keypoints: tuple = field(repr=False, default=())
    descriptors: np.ndarray | None = field(repr=False, default=None)

    @property
    def identity(self) -> tuple[str, str]:
        """Subject and finger - two captures of these form a genuine pair."""
        return (self.subject, self.finger)

    @property
    def label(self) -> str:
        return f"{self.subject}/{self.finger}/{self.index:03d}"

    @property
    def keypoint_count(self) -> int:
        return 0 if self.descriptors is None else len(self.descriptors)


def preprocess(signal: np.ndarray, config: MatcherConfig) -> np.ndarray:
    """Turn the raw signal into a greyscale image for feature detection."""
    if BORDER:
        signal = signal[BORDER:-BORDER, BORDER:-BORDER]

    lo, hi = np.percentile(signal, [1, 99])
    scaled = np.clip((signal - lo) / max(hi - lo, 1e-9), 0, 1)
    image = (scaled * 255).astype(np.uint8)

    if config.upscale > 1:
        image = cv2.resize(
            image, None,
            fx=config.upscale, fy=config.upscale,
            interpolation=cv2.INTER_CUBIC,
        )

    clahe = cv2.createCLAHE(
        clipLimit=config.clahe_clip,
        tileGridSize=(config.clahe_grid, config.clahe_grid),
    )
    return clahe.apply(image)


def extract(image: np.ndarray, config: MatcherConfig):
    """Compute SIFT keypoints and descriptors."""
    sift = cv2.SIFT_create(
        nfeatures=config.sift_features,
        nOctaveLayers=config.sift_octave_layers,
        contrastThreshold=config.sift_contrast,
        edgeThreshold=config.sift_edge,
        sigma=config.sift_sigma,
    )
    keypoints, descriptors = sift.detectAndCompute(image, None)
    return keypoints, descriptors


def load_corpus(root: Path, config: MatcherConfig) -> list[Sample]:
    """Read every capture and compute its descriptors once."""
    samples: list[Sample] = []

    for path in sorted(root.glob("*/*/sample-*.npz")):
        frames, background, metadata = load_sample(path)
        signal = signal_image(frames, background, "best")
        image = preprocess(signal, config)
        keypoints, descriptors = extract(image, config)

        samples.append(
            Sample(
                subject=metadata["subject"],
                finger=metadata["finger"],
                index=metadata["index"],
                keypoints=keypoints,
                descriptors=descriptors,
            )
        )

    return samples


def compare(left: Sample, right: Sample, config: MatcherConfig) -> int:
    """Compare two captures.

    Returns the number of geometrically confirmed correspondences. That is the
    score; the acceptance threshold is derived from the measured distributions
    later and deliberately not fixed here.
    """
    if left.descriptors is None or right.descriptors is None:
        return 0
    if len(left.descriptors) < 2 or len(right.descriptors) < 2:
        return 0

    matcher = cv2.BFMatcher(cv2.NORM_L2)
    pairs = matcher.knnMatch(left.descriptors, right.descriptors, k=2)

    # Lowe ratio test: a match counts only if it is clearly better than the
    # runner-up. Otherwise the assignment is ambiguous.
    good = [m for m, n in (p for p in pairs if len(p) == 2)
            if m.distance < config.ratio * n.distance]

    if len(good) < config.ransac_min_matches:
        return 0

    source = np.float32([left.keypoints[m.queryIdx].pt for m in good])
    target = np.float32([right.keypoints[m.trainIdx].pt for m in good])

    # Without this stage any coincidentally similar texture counts. It is the
    # step the SIGFM draft !530 lacks and which the analysis
    # of the Windows engine exposes as `RANSAC_diff` and `AcceptRANSACCnt`.
    _, inliers = cv2.estimateAffinePartial2D(
        source, target,
        method=cv2.RANSAC,
        ransacReprojThreshold=config.ransac_threshold,
        maxIters=5000,
        confidence=0.99,
    )

    if inliers is None:
        return 0
    return int(inliers.sum())


def all_pairs(samples: list[Sample], config: MatcherConfig):
    """Compare every pair and split into genuine and impostor.

    Genuine  - two captures of the same finger of the same subject
    Impostor - everything else, including different fingers of one subject
    """
    genuine: list[int] = []
    impostor: list[int] = []
    impostor_cross: list[int] = []   # additionally: different subjects

    for i, left in enumerate(samples):
        for right in samples[i + 1:]:
            score = compare(left, right, config)
            if left.identity == right.identity:
                genuine.append(score)
            else:
                impostor.append(score)
                if left.subject != right.subject:
                    impostor_cross.append(score)

    return {
        "genuine": np.array(genuine),
        "impostor": np.array(impostor),
        "impostor_cross": np.array(impostor_cross),
    }


def evaluate(scores: dict) -> dict:
    """Assess how well the distributions separate."""
    genuine, impostor = scores["genuine"], scores["impostor"]
    if genuine.size == 0 or impostor.size == 0:
        return {"valid": False}

    thresholds = range(0, int(max(genuine.max(), impostor.max())) + 2)
    rows = []
    for t in thresholds:
        # A score at or above the threshold counts as a match.
        frr = float((genuine < t).mean())      # genuine wrongly rejected
        far = float((impostor >= t).mean())    # impostor wrongly accepted
        rows.append({"threshold": t, "far": far, "frr": frr})

    # Equal error rate: where both error types occur equally often.
    eer_row = min(rows, key=lambda r: abs(r["far"] - r["frr"]))

    # The security-relevant question: what recognition rate remains if not a
    # single impostor may be accepted?
    zero_far = next((r for r in rows if r["far"] == 0.0), None)

    return {
        "valid": True,
        "rows": rows,
        "eer": eer_row,
        "zero_far": zero_far,
        "genuine_median": float(np.median(genuine)),
        "impostor_median": float(np.median(impostor)),
        "impostor_max": int(impostor.max()),
        "genuine_max": int(genuine.max()),
    }
