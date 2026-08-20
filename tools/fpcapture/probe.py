#!/usr/bin/env python3
"""Erstkontakt mit dem Sensor - liest nur, nimmt keinen Finger auf.

Dieses Skript beruehrt bewusst nur den harmlosen Teil des Protokolls:
Firmware, gemeldete Sensormasse und die Kalibrierungswerte des leeren Sensors.
Es fordert keinen Finger an, speichert nichts und veraendert nichts.

Zweck:

1. Bestaetigen, dass der direkte USB-Zugriff ohne libfprint funktioniert.
2. Pruefen, ob der Sensor wirklich 80x80 meldet.
3. Erste Rohdatenstatistik des Hintergrunds, als Grundlage fuer die
   Aufloesungsmessung (Hypothese H-02 im Forschungstagebuch).

Aufruf::

    sudo tools/.venv/bin/python tools/fpcapture/probe.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from elan0c63 import CALIBRATION_MAX_DELTA, Elan0c63, ElanError  # noqa: E402


def main() -> int:
    if os.geteuid() != 0:
        print("Der USB-Sensor ist nur fuer root zugaenglich. Bitte mit sudo starten:")
        print(f"  sudo {sys.executable} {__file__}")
        return 77

    print()
    print("  Sensorpruefung - es wird KEIN Finger aufgenommen.")
    print("  Bitte den Sensor waehrend des Tests frei lassen.")
    print()

    try:
        with Elan0c63() as sensor:
            print(f"  {sensor.info}")
            print()

            expected = sensor.info.width == 80 and sensor.info.height == 80
            print(f"  Gemeldete Masse:        {sensor.info.width} x {sensor.info.height}")
            print(f"  Erwartet laut Windows:  80 x 80  -> "
                  f"{'stimmt ueberein' if expected else 'WEICHT AB'}")
            print(f"  Rohframe-Groesse:       {sensor.info.raw_frame_bytes} Byte "
                  f"({sensor.info.width * sensor.info.height} Pixel a 2 Byte)")
            print()

            print("  Was libfprint davon benutzen wuerde:")
            used = min(sensor.info.height, 50)
            print(f"    Zeilen insgesamt:     {sensor.info.height}")
            print(f"    Zeilen nach Zuschnitt: {used}  "
                  f"(ELAN_MAX_FRAME_HEIGHT)")
            if used < sensor.info.height:
                lost = sensor.info.height - used
                print(f"    verworfen:            {lost} Zeilen "
                      f"= {100 * lost / sensor.info.height:.1f} Prozent")
            print()

            print("  Hintergrundmessung und Kalibrierung:")
            delta = sensor.calibrate()
            print(f"  Ergebnis: Differenz {delta} "
                  f"(Grenzwert {CALIBRATION_MAX_DELTA})")
            print()

            background = sensor.background
            assert background is not None
            print("  Statistik des leeren Sensors (14-Bit-Rohwerte):")
            print(f"    Minimum:      {int(background.min())}")
            print(f"    Median:       {int(np.median(background))}")
            print(f"    Maximum:      {int(background.max())}")
            print(f"    Standardabw.: {background.std():.1f}")
            print()

            noise = float(background.std())
            if noise > 300:
                print("  Hinweis: hohe Streuung im Leerbild. Entweder liegt")
                print("  etwas auf dem Sensor, oder der Sensor rauscht stark.")
            else:
                print("  Das Leerbild ist ruhig - der Sensor arbeitet sauber.")
            print()
            print("  Es wurde nichts gespeichert und nichts veraendert.")

    except ElanError as exc:
        print(f"  Fehler: {exc}")
        print()
        print("  Haeufigste Ursache: ein laufender fprintd haelt das Geraet.")
        print("  Pruefen mit:  systemctl status fprintd.service")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
