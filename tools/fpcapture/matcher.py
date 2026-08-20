#!/usr/bin/env python3
"""Deskriptorbasierter Matcher fuer den ELAN 04f3:0c63.

Ersetzt den minutienbasierten NBIS/Bozorth-Pfad, der auf diesem Sensor
nachweislich unmoeglich ist: Alle 123 Korpusaufnahmen liefern 0 bis 4
Minutien, waehrend Bozorth zehn benoetigt. Siehe Tagebucheintrag H-07.

Der Ansatz folgt der Architektur, die die Analyse des Windows-Treibers fuer
exakt dieses Geraet gezeigt hat:

1. native Press-Aufnahmen statt kuenstlich montierter Swipes
2. lokale skalen- und rotationsrobuste Keypoints statt Minutien
3. geometrische Pruefung der Korrespondenzen per RANSAC
4. Score aus der Zahl geometrisch bestaetigter Korrespondenzen

Jede Vorverarbeitungsentscheidung ist am Korpus gemessen, nicht geraten:

* bester Einzelframe statt Mittelwert (Faktor 1,26; 120 von 123 Aufnahmen)
* Randstreifen von 3 Pixeln entfernt (beseitigt alle 231 Sensorartefakte)
* keine Ausrichtung vor der Kombination (bringt nichts, Verformung ist elastisch)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from analyze import load_sample, signal_image

# Aus der Defektvermessung: Zeilen 0 und 1 sind vollstaendig gestoert,
# 2 und 3 teilweise. Ein Rand von 3 Pixeln entfernt alle 231 Auffaelligkeiten
# und behaelt 5476 der 6400 Pixel.
BORDER = 3


@dataclass
class MatcherConfig:
    """Alle Stellschrauben an einem Ort, damit Messreihen reproduzierbar sind."""

    # ALLE Werte hier sind am Korpus gemessen, nicht gewaehlt. Die Auswahl
    # erfolgte per Kreuzvalidierung: Parameter je an einer Person bestimmt,
    # an der anderen geprueft. Beide Richtungen ergaben dieselben Kernwerte.

    # Die native Aufnahme ist nach dem Randschnitt 74x74 Pixel gross. Etwas
    # Hochskalierung verschafft SIFT zusaetzliche Oktaven. Mehr ist nicht
    # besser: 4-fach fiel gegenueber 2-fach von 82,1 auf 69,9 Prozent zurueck.
    upscale: int = 2

    # CLAHE hebt lokalen Kontrast an, ohne globale Helligkeitsunterschiede zu
    # verstaerken. Gemessen: 2,0 und 3,0 gleichwertig, 5,0 schlechter.
    clahe_clip: float = 3.0
    clahe_grid: int = 8

    sift_features: int = 0          # 0 = unbegrenzt
    sift_octave_layers: int = 3

    # ACHTUNG - hier lag ein Irrtum. Eine erste Fassung senkte diesen Wert auf
    # 0.02 mit der Begruendung, das Bild sei klein und kontrastarm. Das ist
    # falsch: Ein niedriger Schwellwert laesst Keypoints aus Rauschen zu. Die
    # tragen keine Identitaet und matchen bei fremden Fingern genauso oft wie
    # bei eigenen, heben also nur die Impostor-Scores.
    #   0.02 -> Erkennung 19,5 Prozent
    #   0.04 -> Erkennung 69,9 Prozent   (OpenCV-Standard)
    sift_contrast: float = 0.04
    sift_edge: float = 12.0
    sift_sigma: float = 1.2

    # Lowe-Verhaeltnistest. Der SIGFM-Entwurf !530 verwendet 0,75; gemessen ist
    # 0,70 hier besser (84,6 gegen 82,1 Prozent bei null Fehlakzeptanzen).
    # Ab 0,85 bricht die Trennung ein: 25,2 Prozent.
    ratio: float = 0.70

    # Geometrische Pruefung. Der Finger wird verschoben und gedreht aufgelegt,
    # aber nicht perspektivisch verzerrt - eine Aehnlichkeitstransformation
    # (Drehung, Verschiebung, leichte Skalierung) ist das passende Modell.
    # Der Schwellwert ist zwischen 3 und 10 Pixeln wirkungsgleich.
    ransac_threshold: float = 3.0
    ransac_min_matches: int = 4

    def fingerprint(self) -> str:
        """Kurzkennung der Konfiguration fuer Protokolle."""
        return (f"up{self.upscale}_cl{self.clahe_clip}_ct{self.sift_contrast}"
                f"_ed{self.sift_edge}_sg{self.sift_sigma}_r{self.ratio}"
                f"_rt{self.ransac_threshold}")


@dataclass
class Sample:
    """Eine aufbereitete Aufnahme mit ihren Deskriptoren."""

    subject: str
    finger: str
    index: int
    keypoints: tuple = field(repr=False, default=())
    descriptors: np.ndarray | None = field(repr=False, default=None)

    @property
    def identity(self) -> tuple[str, str]:
        """Person und Finger - zwei Aufnahmen davon sind ein Genuine-Paar."""
        return (self.subject, self.finger)

    @property
    def label(self) -> str:
        return f"{self.subject}/{self.finger}/{self.index:03d}"

    @property
    def keypoint_count(self) -> int:
        return 0 if self.descriptors is None else len(self.descriptors)


def preprocess(signal: np.ndarray, config: MatcherConfig) -> np.ndarray:
    """Rohsignal in ein Graustufenbild fuer die Merkmalssuche wandeln."""
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
    """SIFT-Keypoints und Deskriptoren berechnen."""
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
    """Alle Aufnahmen einlesen und ihre Deskriptoren einmalig berechnen."""
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
    """Zwei Aufnahmen vergleichen.

    Rueckgabe ist die Zahl der geometrisch bestaetigten Korrespondenzen. Dieser
    Wert ist der Score; die Annahmeschwelle wird spaeter aus den gemessenen
    Verteilungen abgeleitet und hier bewusst nicht festgelegt.
    """
    if left.descriptors is None or right.descriptors is None:
        return 0
    if len(left.descriptors) < 2 or len(right.descriptors) < 2:
        return 0

    matcher = cv2.BFMatcher(cv2.NORM_L2)
    pairs = matcher.knnMatch(left.descriptors, right.descriptors, k=2)

    # Lowe-Verhaeltnistest: Ein Treffer zaehlt nur, wenn er deutlich besser
    # ist als der zweitbeste. Sonst ist die Zuordnung mehrdeutig.
    good = [m for m, n in (p for p in pairs if len(p) == 2)
            if m.distance < config.ratio * n.distance]

    if len(good) < config.ransac_min_matches:
        return 0

    source = np.float32([left.keypoints[m.queryIdx].pt for m in good])
    target = np.float32([right.keypoints[m.trainIdx].pt for m in good])

    # Ohne diese Stufe zaehlt jede zufaellig aehnliche Textur mit. Sie ist der
    # Schritt, den der SIGFM-Entwurf !530 nicht besitzt und den die Analyse
    # der Windows-Engine als `RANSAC_diff` und `AcceptRANSACCnt` zeigt.
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
    """Alle Paare vergleichen und nach Genuine/Impostor trennen.

    Genuine  - zwei Aufnahmen desselben Fingers derselben Person
    Impostor - alles andere, auch verschiedene Finger derselben Person
    """
    genuine: list[int] = []
    impostor: list[int] = []
    impostor_cross: list[int] = []   # zusaetzlich: verschiedene Personen

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
    """Trennschaerfe der Verteilungen bewerten."""
    genuine, impostor = scores["genuine"], scores["impostor"]
    if genuine.size == 0 or impostor.size == 0:
        return {"valid": False}

    thresholds = range(0, int(max(genuine.max(), impostor.max())) + 2)
    rows = []
    for t in thresholds:
        # Ein Score ab der Schwelle gilt als Treffer.
        frr = float((genuine < t).mean())      # echte faelschlich abgewiesen
        far = float((impostor >= t).mean())    # fremde faelschlich akzeptiert
        rows.append({"threshold": t, "far": far, "frr": frr})

    # Gleichfehlerpunkt: dort, wo beide Fehlerarten gleich haeufig sind.
    eer_row = min(rows, key=lambda r: abs(r["far"] - r["frr"]))

    # Die sicherheitsrelevante Frage: Welche Erkennungsrate bleibt, wenn kein
    # einziger Impostor akzeptiert werden darf?
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
