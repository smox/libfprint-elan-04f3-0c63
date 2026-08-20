"""Direct USB access to the ELAN 04f3:0c63.

This module speaks the sensor protocol over libusb, without libfprint. That is
deliberate: the analysis needs the unaltered raw frames, before libfprint crops,
normalises and stitches them into a synthetic swipe.

The protocol is taken from the open-source libfprint driver
(libfprint/drivers/elan.c and elan.h, version 1.94.10+tod1, LGPL-2.1+).

Three deliberate departures from the libfprint path:

* Nothing is cropped to 50 rows. All 80 rows are kept.
* No frame assembly. Every frame stays its own frame.
* No normalisation. The raw 14-bit values are stored.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import usb.core
import usb.util

VENDOR_ID = 0x04F3
PRODUCT_ID = 0x0C63

EP_CMD_OUT = 0x01
EP_CMD_IN = 0x83
EP_IMG_IN = 0x82

# Commands from elan.h. Each is exactly two bytes.
CMD_GET_SENSOR_DIM = b"\x00\x0c"
CMD_GET_FW_VER = b"\x40\x19"
CMD_GET_IMAGE = b"\x00\x09"
CMD_READ_SENSOR_STATUS = b"\x40\x13"
CMD_GET_CALIB_STATUS = b"\x40\x23"
CMD_GET_CALIB_MEAN = b"\x40\x24"
CMD_LED_ON = b"\x40\x31"
CMD_PRE_SCAN = b"\x40\x3f"
CMD_STOP = b"\x00\x0b"

# Taken from elan.h.
CALIBRATION_MAX_DELTA = 500
CMD_TIMEOUT_MS = 10_000

FINGER_PRESENT = 0x55
NOT_CALIBRATED = 0xFF


class ElanError(RuntimeError):
    """An error in the sensor protocol."""


class CalibrationError(ElanError):
    """Calibration could not be completed."""


@dataclass(frozen=True)
class SensorInfo:
    """What the sensor reports about itself."""

    firmware: int
    width: int
    height: int

    @property
    def raw_frame_bytes(self) -> int:
        return self.width * self.height * 2

    def __str__(self) -> str:
        return (
            f"ELAN {VENDOR_ID:04x}:{PRODUCT_ID:04x}, "
            f"firmware 0x{self.firmware:04x}, {self.width}x{self.height} px"
        )


class Elan0c63:
    """A session with the sensor.

    Use as a context manager so the LED and the wait state are reliably turned
    off at the end::

        with Elan0c63() as sensor:
            sensor.calibrate()
            frame = sensor.capture_frame()
    """

    def __init__(self) -> None:
        self._dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if self._dev is None:
            raise ElanError(
                f"Sensor {VENDOR_ID:04x}:{PRODUCT_ID:04x} not found."
            )

        self._detached = False
        try:
            if self._dev.is_kernel_driver_active(0):
                self._dev.detach_kernel_driver(0)
                self._detached = True
        except (usb.core.USBError, NotImplementedError):
            # No kernel module claims the interface on this device.
            pass

        try:
            self._dev.set_configuration()
        except usb.core.USBError as exc:
            raise ElanError(
                "Cannot configure the sensor. Is an fprintd running? Are the "
                "permissions on /dev/bus/usb/ missing? Original: " + str(exc)
            ) from exc

        self.info = self._activate()
        self.background: np.ndarray | None = None

    # -- connection handling --------------------------------------------------

    def __enter__(self) -> "Elan0c63":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """LED off, leave the wait state, release the device."""
        try:
            self._write(CMD_STOP)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(self._dev)
        if self._detached:
            try:
                self._dev.attach_kernel_driver(0)
            except (usb.core.USBError, NotImplementedError):
                pass

    # -- protocol basics ------------------------------------------------------

    def _write(self, cmd: bytes) -> None:
        self._dev.write(EP_CMD_OUT, cmd, timeout=CMD_TIMEOUT_MS)

    def _read(self, endpoint: int, length: int, timeout_ms: int) -> bytes:
        return bytes(self._dev.read(endpoint, length, timeout=timeout_ms))

    def _command(
        self,
        cmd: bytes,
        response_len: int,
        endpoint: int = EP_CMD_IN,
        timeout_ms: int = CMD_TIMEOUT_MS,
    ) -> bytes:
        self._write(cmd)
        if response_len == 0:
            return b""
        return self._read(endpoint, response_len, timeout_ms)

    # -- activation -----------------------------------------------------------

    def _activate(self) -> SensorInfo:
        firmware_raw = self._command(CMD_GET_FW_VER, 2)
        firmware = (firmware_raw[0] << 8) | firmware_raw[1]

        dim = self._command(CMD_GET_SENSOR_DIM, 4)
        # The 0c63 is one of the rotated sensors: width in byte 2, height in
        # byte 0 (see elan.c, ACTIVATE_SET_SENSOR_DIM).
        width, height = dim[2], dim[0]

        # Some sensors report a zero-based index instead of a count.
        if width % 2 == 1 and height % 2 == 1:
            width += 1
            height += 1

        return SensorInfo(firmware=firmware, width=width, height=height)

    # -- image capture --------------------------------------------------------

    def _read_raw_frame(self, timeout_ms: int = CMD_TIMEOUT_MS) -> np.ndarray:
        """Read one complete raw frame.

        Returns a ``uint16`` array of shape ``(height, width)`` holding the
        unaltered 14-bit ADC values. Nothing is cropped and nothing is
        normalised.
        """
        self._write(CMD_GET_IMAGE)
        payload = self._read(EP_IMG_IN, self.info.raw_frame_bytes, timeout_ms)

        if len(payload) != self.info.raw_frame_bytes:
            raise ElanError(
                f"Incomplete frame: {len(payload)} instead of "
                f"{self.info.raw_frame_bytes} bytes."
            )

        flat = np.frombuffer(payload, dtype="<u2")
        # The sensor sends the data column-major ("the frame is vertical" in
        # elan.c). Rotate it once into the usual row order.
        return flat.reshape(self.info.width, self.info.height).T.copy()

    def capture_background(self) -> np.ndarray:
        """Capture and remember the empty-sensor background."""
        self.background = self._read_raw_frame()
        return self.background

    def calibration_mean(self) -> int:
        """Read the calibration mean the sensor reports."""
        raw = self._command(CMD_GET_CALIB_MEAN, 2)
        # Deliberately computed the way libfprint does, so the values stay
        # comparable with earlier journals. Note that the factor 0xff instead of
        # 0x100 is a bug in libfprint: the mapping is not injective.
        return raw[0] * 0xFF + raw[1]

    def calibration_delta(self) -> tuple[int, int, int]:
        """Return (calibration mean, background mean, difference)."""
        if self.background is None:
            self.capture_background()
        calib = self.calibration_mean()
        background = int(self.background.mean())
        return calib, background, abs(background - calib)

    def calibrate(self, max_attempts: int = 50, verbose: bool = True) -> int:
        """Calibrate until the difference is within range.

        ``max_attempts`` is deliberately 50 rather than the nine effective
        attempts of the system driver, matching open upstream proposal !217.

        Returns the difference reached.
        """
        for round_index in range(1, max_attempts + 1):
            calib, background, delta = self.calibration_delta()

            if verbose:
                print(
                    f"  calibration {round_index}: mean {calib}, "
                    f"background {background}, difference {delta}"
                )

            if delta <= CALIBRATION_MAX_DELTA:
                return delta

            if round_index == 1 and delta > 4000 and verbose:
                print(
                    "  Note: very large difference. Is a finger on the sensor? "
                    "Please lift it."
                )

            # Wait for the full 0x01 -> 0x03 cycle.
            seen_retry = False
            for _ in range(max_attempts):
                status = self._command(CMD_GET_CALIB_STATUS, 1)[0]
                if status == 0x01:
                    seen_retry = True
                elif status == 0x03 and seen_retry:
                    break
                time.sleep(0.05)

            self.capture_background()

        _, _, delta = self.calibration_delta()
        raise CalibrationError(
            f"Calibration not stable after {max_attempts} attempts "
            f"(difference {delta}, allowed {CALIBRATION_MAX_DELTA})."
        )

    def wait_for_finger(self, timeout_ms: int | None = None) -> bool:
        """Block until a finger is present.

        ``timeout_ms=None`` waits indefinitely. Returns ``True`` if a finger was
        detected.
        """
        self._write(CMD_LED_ON)
        self._write(CMD_PRE_SCAN)
        try:
            status = self._read(EP_CMD_IN, 1, timeout_ms if timeout_ms else 0)
        except usb.core.USBError:
            return False

        if not status:
            return False
        if status[0] == NOT_CALIBRATED:
            raise ElanError("Sensor reports: not calibrated (0xff).")
        return status[0] == FINGER_PRESENT

    def capture_press(self, frames: int = 8) -> np.ndarray:
        """Capture several raw frames of one resting finger press.

        This is the real difference from the libfprint path: the finger stays
        put. Nothing is assembled; every frame is kept separately. Several
        frames of the same press allow later selection of the sharpest one.

        Returns an array of shape ``(frames, height, width)``.
        """
        captured = [self._read_raw_frame()]

        for _ in range(frames - 1):
            # Short timeout: if the finger is lifted, stop rather than block.
            if not self.wait_for_finger(timeout_ms=200):
                break
            try:
                captured.append(self._read_raw_frame(timeout_ms=2000))
            except (ElanError, usb.core.USBError):
                break

        return np.stack(captured)
