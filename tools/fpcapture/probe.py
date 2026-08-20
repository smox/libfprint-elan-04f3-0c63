#!/usr/bin/env python3
"""First contact with the sensor - reads only, captures no finger.

This script deliberately touches only the harmless part of the protocol:
firmware, the reported sensor dimensions, and the calibration values of the
empty sensor. It asks for no finger, stores nothing and changes nothing.

Purpose:

1. Confirm that direct USB access without libfprint works.
2. Check whether the sensor really reports 80x80.
3. A first raw statistic of the background, as the basis for the resolution
   measurement.

Usage::

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
        print("The USB device is root-only. Please run with sudo:")
        print(f"  sudo {sys.executable} {__file__}")
        return 77

    print()
    print("  Sensor check - NO finger is captured.")
    print("  Please keep the sensor clear during the test.")
    print()

    try:
        with Elan0c63() as sensor:
            print(f"  {sensor.info}")
            print()

            expected = sensor.info.width == 80 and sensor.info.height == 80
            print(f"  reported dimensions:   {sensor.info.width} x {sensor.info.height}")
            print(f"  expected:              80 x 80  -> "
                  f"{'matches' if expected else 'DIFFERS'}")
            print(f"  raw frame size:        {sensor.info.raw_frame_bytes} bytes "
                  f"({sensor.info.width * sensor.info.height} px at 2 bytes)")
            print()

            print("  What libfprint would use of that:")
            used = min(sensor.info.height, 50)
            print(f"    rows in total:       {sensor.info.height}")
            print(f"    rows after cropping: {used}  "
                  f"(ELAN_MAX_FRAME_HEIGHT)")
            if used < sensor.info.height:
                lost = sensor.info.height - used
                print(f"    discarded:           {lost} rows "
                      f"= {100 * lost / sensor.info.height:.1f} per cent")
            print()

            print("  Background measurement and calibration:")
            delta = sensor.calibrate()
            print(f"  result: difference {delta} "
                  f"(limit {CALIBRATION_MAX_DELTA})")
            print()

            background = sensor.background
            assert background is not None
            print("  Statistics of the empty sensor (raw 14-bit values):")
            print(f"    minimum:      {int(background.min())}")
            print(f"    median:       {int(np.median(background))}")
            print(f"    maximum:      {int(background.max())}")
            print(f"    std. dev.:    {background.std():.1f}")
            print()

            noise = float(background.std())
            if noise > 300:
                print("  Note: high spread in the empty image. Either something is")
                print("  resting on the sensor, or the sensor is noisy.")
            else:
                print("  The empty image is quiet - the sensor is behaving.")
            print()
            print("  Nothing was stored and nothing was changed.")

    except ElanError as exc:
        print(f"  Error: {exc}")
        print()
        print("  Most common cause: a running fprintd holds the device.")
        print("  Check with:  systemctl status fprintd.service")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
