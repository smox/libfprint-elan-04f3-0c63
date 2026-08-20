#!/usr/bin/env python3
"""Rohaufnahmen fuer den Offline-Korpus erfassen.

Nimmt native 80x80-Rohframes des ELAN 04f3:0c63 auf und legt sie ausserhalb
des Projektverzeichnisses ab. Es wird nichts beschnitten, nichts montiert und
nichts normalisiert.

Aufruf::

    sudo tools/.venv/bin/python tools/fpcapture/capture_corpus.py \\
        --subject alice --samples 15 \\
        --fingers right-index right-middle left-index left-middle

Datenschutzregeln, die dieses Skript technisch erzwingt:

* Der Ausgabepfad darf niemals innerhalb des Projektverzeichnisses liegen.
* Das Zielverzeichnis wird mit 0700 angelegt und gehoert dem aufrufenden
  Benutzer, nicht root.
* Jede Person muss vor der ersten Aufnahme ausdruecklich zustimmen.
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
PROJECT_DIR = Path("/home/michael/rootserver")

FINGERS = (
    "left-thumb", "left-index", "left-middle", "left-ring", "left-little",
    "right-thumb", "right-index", "right-middle", "right-ring", "right-little",
)


def invoking_user() -> tuple[int, int] | None:
    """Die Kennung des Benutzers ermitteln, der ``sudo`` aufgerufen hat.

    Das Skript braucht root, weil das USB-Geraet nur root gehoert. Die
    erzeugten Dateien sollen aber dem eigentlichen Benutzer gehoeren, sonst
    kommt er ohne ``sudo`` nicht mehr an seine eigenen Aufnahmen.
    """
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return None
    return int(uid), int(gid)


def hand_over(path: Path, owner: tuple[int, int] | None) -> None:
    """Datei oder Verzeichnis dem aufrufenden Benutzer uebereignen."""
    if owner is None:
        return
    try:
        os.chown(path, owner[0], owner[1])
    except OSError:
        # Nicht kritisch: Die Daten sind gespeichert, nur der Zugriff ist
        # dann weiterhin auf root beschraenkt.
        pass


def guard_output_path(path: Path) -> Path:
    """Sicherstellen, dass biometrische Daten nie im Projektordner landen."""
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_DIR.resolve())
    except ValueError:
        return resolved

    raise SystemExit(
        f"Abbruch: {resolved} liegt im Projektverzeichnis.\n"
        "Rohaufnahmen duerfen niemals dorthin, weil sie sonst in einem\n"
        "Git-Repository landen koennten. Bitte einen Pfad ausserhalb waehlen."
    )


def confirm_consent(subject: str, corpus: Path) -> None:
    """Einmalige, dokumentierte Einwilligung pro Person einholen."""
    marker = corpus / subject / "EINWILLIGUNG.json"
    if marker.exists():
        return

    print()
    print(f"  Erstaufnahme fuer '{subject}'. Bitte bestaetigen:")
    print()
    print("  - Es werden Rohbilder deines Fingerabdrucks gespeichert.")
    print(f"  - Ablage ausschliesslich lokal unter {corpus}, nur fuer dich lesbar.")
    print("  - Keine Uebertragung ins Internet, in kein Git-Repository,")
    print("    an keinen externen Dienst.")
    print("  - Verwendung ausschliesslich zum Test dieses Sensors.")
    print("  - Loeschung auf Wunsch jederzeit und nach Projektabschluss.")
    print()

    answer = input(f"  Stimmt {subject} zu? [ja/nein] ").strip().lower()
    if answer not in ("ja", "j", "yes", "y"):
        raise SystemExit("Ohne Einwilligung wird nichts aufgenommen. Abbruch.")

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
    print("  Einwilligung dokumentiert.\n")


def next_index(target: Path) -> int:
    existing = sorted(target.glob("sample-*.npz"))
    if not existing:
        return 1
    return int(existing[-1].stem.split("-")[1]) + 1


def live_quality(frames: np.ndarray, background: np.ndarray) -> dict:
    """Sofortbewertung einer frischen Aufnahme.

    Die Messungen vom 20. August zeigten eine Schwankung der Rippenklarheit um
    Faktor neun zwischen aufeinanderfolgenden Drucken desselben Fingers. Ohne
    Rueckmeldung waehrend der Aufnahme merkt niemand, dass ein Druck schlecht
    war - und der Korpus fuellt sich mit unbrauchbaren Aufnahmen.
    """
    signal = frames.astype(np.float64).mean(axis=0) - background.astype(np.float64)
    metrics = quality_metrics(signal, frames.astype(np.float64))
    ridge = ridge_frequency(signal)

    metrics["prominence"] = ridge["prominence"] if ridge.get("valid") else 0.0

    # Schwellen aus den ersten Messungen. Gute Aufnahmen erreichten
    # Deutlichkeiten von 122 und 164, eine schwache nur 18. Bewusst grosszuegig
    # gewaehlt: Der Korpus soll auch schlechtere Aufnahmen enthalten, denn erst
    # daraus laesst sich spaeter eine belastbare Qualitaetsschwelle ableiten.
    # Diese Anzeige dient der Rueckmeldung, nicht der Auswahl.
    if metrics["prominence"] >= 100 and metrics["snr"] >= 8:
        metrics["verdict"] = "gut"
    elif metrics["prominence"] >= 40:
        metrics["verdict"] = "brauchbar"
    else:
        metrics["verdict"] = "schwach"

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
    """Eine Serie fuer genau einen Finger aufnehmen."""
    start = next_index(target)
    saved = 0
    verdicts = {"gut": 0, "brauchbar": 0, "schwach": 0}

    for offset in range(samples):
        index = start + offset
        print(f"  [{offset + 1}/{samples}] {finger}: auflegen und ruhig halten ...")

        try:
            if not sensor.wait_for_finger():
                print("      kein Finger erkannt, Runde uebersprungen.")
                continue
            frames = sensor.capture_press(frames=frames_per_sample)
        except ElanError as exc:
            print(f"      Aufnahmefehler: {exc}")
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
                    "note": "Rohframes, ungeschnitten und unnormalisiert",
                }
            ),
        )
        os.chmod(path, 0o600)
        hand_over(path, owner)
        saved += 1
        verdicts[quality["verdict"]] += 1

        print(
            f"      {frames.shape[0]} Frames - Klarheit "
            f"{quality['prominence']:5.0f}, Rauschabstand {quality['snr']:4.1f}, "
            f"Abdeckung {quality['coverage'] * 100:4.1f}%  -> {quality['verdict']}"
        )
        print("      Finger abheben.")

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

    total = {"saved": 0, "gut": 0, "brauchbar": 0, "schwach": 0}

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

            # Vor jedem Finger neu kalibrieren. Der Hintergrund driftet mit der
            # Sensortemperatur, und nach vielen Drucken koennen Rueckstaende auf
            # der Flaeche liegen.
            print("  Sensor bitte frei lassen - Hintergrundmessung laeuft.")
            try:
                delta = sensor.calibrate(verbose=False)
            except CalibrationError as exc:
                print(f"  Kalibrierung fehlgeschlagen: {exc}")
                return 1
            print(f"  Kalibrierung stabil, Differenz {delta}.")
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
                input("  Weiter mit dem naechsten Finger - Eingabetaste ... ")
                print()

    print(f"  Sitzung beendet: {total['saved']} Aufnahmen gespeichert.")
    print(f"  Verteilung: gut {total['gut']}, brauchbar {total['brauchbar']}, "
          f"schwach {total['schwach']}")
    print()
    print("  Alle Aufnahmen bleiben im Korpus, auch die schwachen. Erst aus")
    print("  der vollen Bandbreite laesst sich eine Qualitaetsschwelle ableiten.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rohaufnahmen des ELAN 04f3:0c63 fuer den Offline-Korpus.",
    )
    parser.add_argument("--subject", required=True,
                        help="Kennung der Person, zum Beispiel 'alice'")
    parser.add_argument("--fingers", required=True, nargs="+", choices=FINGERS,
                        metavar="FINGER",
                        help="ein oder mehrere Finger, nacheinander aufgenommen")
    parser.add_argument("--samples", type=int, default=15,
                        help="Anzahl der Aufnahmen in dieser Sitzung")
    parser.add_argument("--frames", type=int, default=8,
                        help="Rohframes pro Aufnahme")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS),
                        help="Zielverzeichnis, muss ausserhalb des Projekts liegen")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("Der USB-Sensor ist nur fuer root zugaenglich. Bitte mit sudo starten:")
        print(f"  sudo {sys.executable} {' '.join(sys.argv)}")
        return 77

    try:
        return capture_session(args)
    except ElanError as exc:
        print(f"Fehler: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nAbgebrochen. Bereits gespeicherte Aufnahmen bleiben erhalten.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
