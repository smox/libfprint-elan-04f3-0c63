#!/usr/bin/env python3
"""Record raw captures for the offline corpus.

Captures native 80x80 raw frames from the ELAN 04f3:0c63 and stores them
outside any repository. Nothing is cropped, assembled or normalised.

Aufruf::

    sudo tools/.venv/bin/python tools/fpcapture/capture_corpus.py \\
        --subject alice --samples 15 \\
        --fingers right-index right-middle left-index left-middle

Data-protection rules this script enforces in code:

* The output path must never lie inside a git repository.
* The target directory is created 0700 and owned by the calling user, not root.
* Every person must consent explicitly before their first capture.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze import quality_metrics, ridge_frequency  # noqa: E402
from elan0c63 import CalibrationError, Elan0c63, ElanError  # noqa: E402

DEFAULT_CORPUS = Path("/var/lib/fprint-research/corpus")

FINGERS = (
    "left-thumb", "left-index", "left-middle", "left-ring", "left-little",
    "right-thumb", "right-index", "right-middle", "right-ring", "right-little",
)


def invoking_user() -> tuple[int, int] | None:
    """Identify the user who invoked ``sudo``.

    The script needs root because the USB device is root-owned, but the files
    it creates should belong to the actual user - otherwise they cannot reach
    their own captures without ``sudo``.
    """
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return None
    return int(uid), int(gid)


def hand_over(path: Path, owner: tuple[int, int] | None) -> None:
    """Hand a file or directory over to the calling user."""
    if owner is None:
        return
    try:
        os.chown(path, owner[0], owner[1])
    except OSError:
        # Not critical: the data is stored, only access stays root-only.
        pass


def enclosing_repository(path: Path) -> Path | None:
    """Return the nearest enclosing git repository, or None.

    Checks the path itself and every parent. Also catches a path that does not
    exist yet, since the directory is created later.
    """
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def guard_output_path(path: Path) -> Path:
    """Refuse to write raw captures anywhere inside a git repository.

    Do not compare against a fixed project path: that only protects the machine
    it was written on, and fails silently everywhere else. Detect a repository
    instead, wherever the user points this.
    """
    resolved = path.resolve()

    repository = enclosing_repository(resolved)
    if repository is not None:
        raise SystemExit(
            f"Refusing to write to {resolved}.\n"
            f"It lies inside the git repository at {repository}.\n"
            "Raw fingerprint captures must never end up somewhere they could be\n"
            "committed. Choose a path outside any repository."
        )

    # Second line of defence: the directory this script lives in.
    script_repository = enclosing_repository(Path(__file__).resolve().parent)
    if script_repository is not None:
        try:
            resolved.relative_to(script_repository)
        except ValueError:
            pass
        else:
            raise SystemExit(
                f"Refusing to write to {resolved}.\n"
                f"It lies below this project at {script_repository}.\n"
                "Choose a path outside it."
            )

    return resolved


def confirm_consent(subject: str, corpus: Path) -> None:
    """Obtain and record a one-time consent per person."""
    marker = corpus / subject / "CONSENT.json"
    if marker.exists():
        return

    print()
    print(f"  First capture for '{subject}'. Please confirm:")
    print()
    print("  - Raw images of your fingerprint will be stored.")
    print(f"  - Stored locally under {corpus} only, readable only by you.")
    print("  - No transfer to the internet, to any git repository,")
    print("    or to any external service.")
    print("  - Used solely for testing this sensor.")
    print("  - Deletable on request at any time, and when the project ends.")
    print()

    answer = input(f"  Does {subject} consent? [yes/no] ").strip().lower()
    if answer not in ("yes", "y", "ja", "j"):
        raise SystemExit("Nothing is captured without consent. Aborting.")

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "subject": subject,
                "granted_at": datetime.now(timezone.utc).isoformat(),
                "scope": "Lokaler Sensortest ELAN 04f3:0c63",
                "storage": str(corpus),
                "revocable": True,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)
    hand_over(marker, invoking_user())
    hand_over(marker.parent, invoking_user())
    print("  Consent recorded.\n")


def next_index(target: Path) -> int:
    existing = sorted(target.glob("sample-*.npz"))
    if not existing:
        return 1
    return int(existing[-1].stem.split("-")[1]) + 1


def live_quality(frames: np.ndarray, background: np.ndarray) -> dict:
    """Score a fresh capture immediately.

    Measurements showed ridge clarity varying by a factor of nine between
    consecutive presses of the same finger. Without feedback during the
    session nobody notices a bad press, and the corpus fills up with
    unusable captures.
    """
    signal = frames.astype(np.float64).mean(axis=0) - background.astype(np.float64)
    metrics = quality_metrics(signal, frames.astype(np.float64))
    ridge = ridge_frequency(signal)

    metrics["prominence"] = ridge["prominence"] if ridge.get("valid") else 0.0

    # Thresholds from the first measurements: good captures reached clarities of
    # 122 and 164, a weak one only 18. Deliberately generous - the corpus should
    # contain weaker captures too, because only they allow a defensible quality
    # threshold to be derived later. This display is feedback, not selection.
    if metrics["prominence"] >= 100 and metrics["snr"] >= 8:
        metrics["verdict"] = "good"
    elif metrics["prominence"] >= 40:
        metrics["verdict"] = "usable"
    else:
        metrics["verdict"] = "weak"

    return metrics


def capture_finger(
    sensor: Elan0c63,
    subject: str,
    finger: str,
    target: Path,
    samples: int,
    frames_per_sample: int,
    delta: int,
    owner: tuple[int, int] | None,
) -> dict:
    """Record a series for exactly one finger."""
    start = next_index(target)
    saved = 0
    verdicts = {"good": 0, "usable": 0, "weak": 0}

    for offset in range(samples):
        index = start + offset
        print(f"  [{offset + 1}/{samples}] {finger}: place finger and hold still ...")

        try:
            if not sensor.wait_for_finger():
                print("      no finger detected, round skipped.")
                continue
            frames = sensor.capture_press(frames=frames_per_sample)
        except ElanError as exc:
            print(f"      capture error: {exc}")
            continue

        quality = live_quality(frames, sensor.background)

        path = target / f"sample-{index:03d}.npz"
        np.savez_compressed(
            path,
            frames=frames,
            background=sensor.background,
            metadata=json.dumps(
                {
                    "subject": subject,
                    "finger": finger,
                    "index": index,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "firmware": sensor.info.firmware,
                    "width": sensor.info.width,
                    "height": sensor.info.height,
                    "frames": int(frames.shape[0]),
                    "calibration_delta": delta,
                    "contrast": quality["contrast"],
                    "snr": quality["snr"],
                    "coverage": quality["coverage"],
                    "ridge_prominence": quality["prominence"],
                    "note": "raw frames, uncropped and unnormalised",
                }
            ),
        )
        os.chmod(path, 0o600)
        hand_over(path, owner)
        saved += 1
        verdicts[quality["verdict"]] += 1

        print(
            f"      {frames.shape[0]} frames - clarity "
            f"{quality['prominence']:5.0f}, SNR {quality['snr']:4.1f}, "
            f"coverage {quality['coverage'] * 100:4.1f}%  -> {quality['verdict']}"
        )
        print("      Lift the finger.")

        while sensor.wait_for_finger(timeout_ms=300):
            pass

    return {"saved": saved, "verdicts": verdicts}


def capture_session(args: argparse.Namespace) -> int:
    owner = invoking_user()

    corpus = guard_output_path(Path(args.corpus))
    corpus.mkdir(parents=True, exist_ok=True)
    os.chmod(corpus, 0o700)
    hand_over(corpus, owner)
    hand_over(corpus.parent, owner)

    confirm_consent(args.subject, corpus)

    total = {"saved": 0, "good": 0, "usable": 0, "weak": 0}

    with Elan0c63() as sensor:
        print(f"  {sensor.info}")
        if sensor.info.width != 80 or sensor.info.height != 80:
            print(f"  ACHTUNG: erwartet 80x80, gemeldet "
                  f"{sensor.info.width}x{sensor.info.height}.")
        print()

        for position, finger in enumerate(args.fingers, start=1):
            print(f"  === Finger {position} von {len(args.fingers)}: "
                  f"{finger} ===")

            target = corpus / args.subject / finger
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target.parent, 0o700)
            os.chmod(target, 0o700)
            hand_over(target.parent, owner)
            hand_over(target, owner)

            # Recalibrate before each finger: the background drifts with sensor
            # temperature, and residue can build up on the surface.
            print("  Keep the sensor clear - background measurement running.")
            try:
                delta = sensor.calibrate(verbose=False)
            except CalibrationError as exc:
                print(f"  Calibration failed: {exc}")
                return 1
            print(f"  Calibration stable, difference {delta}.")
            print()

            result = capture_finger(
                sensor, args.subject, finger, target,
                args.samples, args.frames, delta, owner,
            )

            total["saved"] += result["saved"]
            for name, count in result["verdicts"].items():
                total[name] += count

            print()
            print(f"  {finger}: {result['saved']} Aufnahmen "
                  f"(gut {result['verdicts']['gut']}, "
                  f"brauchbar {result['verdicts']['brauchbar']}, "
                  f"schwach {result['verdicts']['schwach']})")
            print()

            if position < len(args.fingers):
                input("  Continue with the next finger - press Enter ... ")
                print()

    print(f"  Session finished: {total['saved']} captures stored.")
    print(f"  distribution: good {total['gut']}, usable {total['brauchbar']}, "
          f"schwach {total['schwach']}")
    print()
    print("  All captures stay in the corpus, including the weak ones. Only the")
    print("  full range allows a quality threshold to be derived.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record raw ELAN 04f3:0c63 captures for the offline corpus.",
    )
    parser.add_argument("--subject", required=True,
                        help="identifier for the person, for example 'alice'")
    parser.add_argument("--fingers", required=True, nargs="+", choices=FINGERS,
                        metavar="FINGER",
                        help="one or more fingers, recorded in sequence")
    parser.add_argument("--samples", type=int, default=15,
                        help="number of captures in this session")
    parser.add_argument("--frames", type=int, default=8,
                        help="raw frames per capture")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS),
                        help="target directory, must lie outside any repository")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("The USB device is root-only. Please run with sudo:")
        print(f"  sudo {sys.executable} {' '.join(sys.argv)}")
        return 77

    try:
        return capture_session(args)
    except ElanError as exc:
        print(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nAborted. Captures already stored are kept.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
