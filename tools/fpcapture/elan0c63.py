"""Direkter USB-Zugriff auf den ELAN 04f3:0c63.

Dieses Modul spricht das Sensorprotokoll unmittelbar über libusb an, ohne
libfprint. Das ist Absicht: Für die Forschungsarbeit brauchen wir die
unveraenderten Rohframes, bevor libfprint sie beschneidet, normalisiert und zu
einem kuenstlichen Swipe montiert.

Das Protokoll wurde aus dem quelloffenen libfprint-Treiber uebernommen
(libfprint/drivers/elan.c und elan.h, Version 1.94.10+tod1, LGPL-2.1+).

Wichtige Abweichungen zum libfprint-Pfad, alle bewusst:

* Es wird nichts auf 50 Zeilen beschnitten. Wir behalten alle 80 Zeilen.
* Es findet keine Frame-Montage statt. Jeder Frame bleibt ein eigener Frame.
* Es findet keine Normalisierung statt. Wir speichern die rohen 14-Bit-Werte.
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

# Kommandos aus elan.h. Jedes ist genau zwei Byte lang.
CMD_GET_SENSOR_DIM = b"\x00\x0c"
CMD_GET_FW_VER = b"\x40\x19"
CMD_GET_IMAGE = b"\x00\x09"
CMD_READ_SENSOR_STATUS = b"\x40\x13"
CMD_GET_CALIB_STATUS = b"\x40\x23"
CMD_GET_CALIB_MEAN = b"\x40\x24"
CMD_LED_ON = b"\x40\x31"
CMD_PRE_SCAN = b"\x40\x3f"
CMD_STOP = b"\x00\x0b"

# Aus elan.h uebernommen.
CALIBRATION_MAX_DELTA = 500
CMD_TIMEOUT_MS = 10_000

FINGER_PRESENT = 0x55
NOT_CALIBRATED = 0xFF


class ElanError(RuntimeError):
    """Fehler im Sensorprotokoll."""


class CalibrationError(ElanError):
    """Die Kalibrierung konnte nicht abgeschlossen werden."""


@dataclass(frozen=True)
class SensorInfo:
    """Was der Sensor ueber sich selbst meldet."""

    firmware: int
    width: int
    height: int

    @property
    def raw_frame_bytes(self) -> int:
        return self.width * self.height * 2

    def __str__(self) -> str:
        return (
            f"ELAN {VENDOR_ID:04x}:{PRODUCT_ID:04x}, "
            f"Firmware 0x{self.firmware:04x}, {self.width}x{self.height} Pixel"
        )


class Elan0c63:
    """Sitzung mit dem Sensor.

    Als Kontextmanager verwenden, damit die LED und der Wartezustand am Ende
    zuverlaessig abgeschaltet werden::

        with Elan0c63() as sensor:
            sensor.calibrate()
            frame = sensor.capture_frame()
    """

    def __init__(self) -> None:
        self._dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if self._dev is None:
            raise ElanError(
                f"Sensor {VENDOR_ID:04x}:{PRODUCT_ID:04x} nicht gefunden."
            )

        self._detached = False
        try:
            if self._dev.is_kernel_driver_active(0):
                self._dev.detach_kernel_driver(0)
                self._detached = True
        except (usb.core.USBError, NotImplementedError):
            # Auf diesem Geraet beansprucht kein Kernelmodul die Schnittstelle.
            pass

        try:
            self._dev.set_configuration()
        except usb.core.USBError as exc:
            raise ElanError(
                "Sensor laesst sich nicht konfigurieren. Laeuft parallel ein "
                "fprintd? Fehlen Rechte auf /dev/bus/usb/? Original: " + str(exc)
            ) from exc

        self.info = self._activate()
        self.background: np.ndarray | None = None

    # -- Verbindungsverwaltung ------------------------------------------------

    def __enter__(self) -> "Elan0c63":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """LED aus, Wartezustand beenden, Geraet freigeben."""
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

    # -- Protokollgrundlagen --------------------------------------------------

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

    # -- Aktivierung ----------------------------------------------------------

    def _activate(self) -> SensorInfo:
        firmware_raw = self._command(CMD_GET_FW_VER, 2)
        firmware = (firmware_raw[0] << 8) | firmware_raw[1]

        dim = self._command(CMD_GET_SENSOR_DIM, 4)
        # Der 0c63 gehoert zu den gedrehten Sensoren: Breite steht in Byte 2,
        # Hoehe in Byte 0 (siehe elan.c, ACTIVATE_SET_SENSOR_DIM).
        width, height = dim[2], dim[0]

        # Manche Sensoren melden einen Null-basierten Index statt der Anzahl.
        if width % 2 == 1 and height % 2 == 1:
            width += 1
            height += 1

        return SensorInfo(firmware=firmware, width=width, height=height)

    # -- Bildaufnahme ---------------------------------------------------------

    def _read_raw_frame(self, timeout_ms: int = CMD_TIMEOUT_MS) -> np.ndarray:
        """Einen vollstaendigen Rohframe lesen.

        Rueckgabe ist ein ``uint16``-Array der Form ``(height, width)`` mit den
        unveraenderten 14-Bit-ADC-Werten. Es wird nichts beschnitten und nichts
        normalisiert.
        """
        self._write(CMD_GET_IMAGE)
        payload = self._read(EP_IMG_IN, self.info.raw_frame_bytes, timeout_ms)

        if len(payload) != self.info.raw_frame_bytes:
            raise ElanError(
                f"Unvollstaendiger Frame: {len(payload)} statt "
                f"{self.info.raw_frame_bytes} Byte."
            )

        flat = np.frombuffer(payload, dtype="<u2")
        # Der Sensor liefert die Daten spaltenweise ("the frame is vertical",
        # elan.c). Wir drehen sie hier einmal in die uebliche Zeilenordnung.
        return flat.reshape(self.info.width, self.info.height).T.copy()

    def capture_background(self) -> np.ndarray:
        """Hintergrundbild ohne Finger aufnehmen und merken."""
        self.background = self._read_raw_frame()
        return self.background

    def calibration_mean(self) -> int:
        """Den vom Sensor gemeldeten Kalibrierungsmittelwert lesen."""
        raw = self._command(CMD_GET_CALIB_MEAN, 2)
        # Bewusst wie libfprint gerechnet, damit die Werte mit den bisherigen
        # Journalen vergleichbar bleiben. Siehe HINWEIS in README.md: der
        # Faktor 0xff statt 0x100 ist vermutlich ein Fehler in libfprint.
        return raw[0] * 0xFF + raw[1]

    def calibration_delta(self) -> tuple[int, int, int]:
        """(Kalibrierungsmittel, Hintergrundmittel, Differenz) bestimmen."""
        if self.background is None:
            self.capture_background()
        calib = self.calibration_mean()
        background = int(self.background.mean())
        return calib, background, abs(background - calib)

    def calibrate(self, max_attempts: int = 50, verbose: bool = True) -> int:
        """Sensor kalibrieren, bis die Differenz im gruenen Bereich liegt.

        ``max_attempts`` ist bewusst 50 statt der neun effektiven Versuche des
        Systemtreibers; das entspricht dem offenen Upstream-Vorschlag !217.

        Rueckgabe ist die erreichte Differenz.
        """
        for round_index in range(1, max_attempts + 1):
            calib, background, delta = self.calibration_delta()

            if verbose:
                print(
                    f"  Kalibrierung {round_index}: Mittel {calib}, "
                    f"Hintergrund {background}, Differenz {delta}"
                )

            if delta <= CALIBRATION_MAX_DELTA:
                return delta

            if round_index == 1 and delta > 4000 and verbose:
                print(
                    "  Hinweis: sehr grosse Differenz. Liegt ein Finger auf "
                    "dem Sensor? Bitte abheben."
                )

            # Auf den vollstaendigen Zyklus 0x01 -> 0x03 warten.
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
            f"Kalibrierung nach {max_attempts} Versuchen nicht stabil "
            f"(Differenz {delta}, erlaubt {CALIBRATION_MAX_DELTA})."
        )

    def wait_for_finger(self, timeout_ms: int | None = None) -> bool:
        """Blockieren, bis ein Finger aufliegt.

        ``timeout_ms=None`` wartet unbegrenzt. Rueckgabe ``True``, wenn ein
        Finger erkannt wurde.
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
            raise ElanError("Sensor meldet: nicht kalibriert (0xff).")
        return status[0] == FINGER_PRESENT

    def capture_press(self, frames: int = 8) -> np.ndarray:
        """Mehrere Rohframes eines ruhenden Fingerdrucks aufnehmen.

        Das ist der eigentliche Unterschied zum libfprint-Pfad: Der Finger
        bleibt liegen. Wir montieren nichts, sondern behalten alle Frames
        einzeln. Mehrere Frames desselben Drucks erlauben spaeter, das
        Sensorrauschen durch Mittelung zu senken.

        Rueckgabe ist ein Array der Form ``(frames, height, width)``.
        """
        captured = [self._read_raw_frame()]

        for _ in range(frames - 1):
            # Kurzes Zeitlimit: Wird der Finger abgehoben, brechen wir ab,
            # statt zu blockieren.
            if not self.wait_for_finger(timeout_ms=200):
                break
            try:
                captured.append(self._read_raw_frame(timeout_ms=2000))
            except (ElanError, usb.core.USBError):
                break

        return np.stack(captured)
