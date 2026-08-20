#!/usr/bin/env python3
"""Offline-Auswertung der Rohaufnahmen.

Arbeitet ausschliesslich auf bereits gespeicherten ``.npz``-Dateien und
beruehrt den Sensor nicht. Ausgegeben werden nur abgeleitete Kennzahlen,
niemals Bilddaten selbst.

Beantwortet drei Fragen:

1. Wie gut ist die Aufnahme ueberhaupt? (Kontrast, Rauschabstand)
2. Welche Aufloesung hat der Sensor wirklich? (Hypothese H-02)
3. Gibt es defekte Pixel, die in jedem Bild dieselbe Scheinstruktur
   erzeugen wuerden?

Aufruf::

    tools/.venv/bin/python tools/fpcapture/analyze.py /pfad/zur/sample-001.npz
    tools/.venv/bin/python tools/fpcapture/analyze.py --png /pfad/sample-001.npz
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# Rippenabstand eines erwachsenen Fingers. Die Literatur nennt ueblicherweise
# 0,4 bis 0,5 mm von Rippenmitte zu Rippenmitte. Wir rechnen mit der Mitte und
# geben die Spanne als Unsicherheit mit aus.
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
    """Aus mehreren Frames eines Drucks ein Bild machen.

    Ursprünglich wurde hier über alle Frames gemittelt, mit der Begruendung,
    das senke das Sensorrauschen um die Wurzel der Framezahl. Die Messung am
    Korpus vom 20. August widerlegt das: Der beste Einzelframe schlaegt den
    Mittelwert in 120 von 123 Aufnahmen, im Median um Faktor 1,26.

    Der Grund ist Bewegung. Waehrend der acht Frames verformt sich die Haut,
    und der Mittelwert verwischt die Rippen. Eine Ausrichtung per
    Kreuzkorrelation half nicht (Faktor 0,96) - die Verformung ist also nicht
    starr, sondern elastisch.

    ``mode``:

    * ``best``    - schaerfster Einzelframe, gemessen an der Rippenklarheit
    * ``mean``    - alter Mittelwert, nur noch fuer Vergleiche
    * ``median``  - robust gegen einzelne Ausreisser, aber ebenfalls unscharf
    """
    signals = frames - background

    if mode == "mean":
        return signals.mean(axis=0)
    if mode == "median":
        return np.median(signals, axis=0)
    if mode != "best":
        raise ValueError(f"Unbekannter Modus: {mode}")

    if signals.shape[0] == 1:
        return signals[0]

    scores = [ridge_frequency(frame).get("prominence", 0.0) for frame in signals]
    return signals[int(np.argmax(scores))]


def quality_metrics(signal: np.ndarray, frames: np.ndarray) -> dict:
    """Kennzahlen zur Aufnahmequalitaet."""
    p1, p99 = np.percentile(signal, [1, 99])
    contrast = float(p99 - p1)

    # Rauschen: Streuung zwischen den Frames an derselben Stelle. Der Finger
    # liegt still, also ist jede Abweichung Rauschen.
    if frames.shape[0] > 1:
        noise = float(frames.std(axis=0).mean())
    else:
        noise = float("nan")

    snr = contrast / noise if noise and noise == noise and noise > 0 else float("nan")

    # Abdeckung: Anteil der Flaeche mit nennenswertem Signal.
    threshold = p1 + 0.15 * contrast
    coverage = float((signal > threshold).mean())

    return {
        "contrast": contrast,
        "noise": noise,
        "snr": snr,
        "coverage": coverage,
    }


def frame_stability(frames: np.ndarray, background: np.ndarray) -> dict:
    """Pruefen, wie ruhig der Finger waehrend der acht Frames lag.

    Bei einem Press liegt der Finger still, alle Frames muessten also nahezu
    identisch sein. Weichen einzelne ab, hat sich der Finger gesetzt, bewegt
    oder der Kontakt war instabil. Solche Frames verschlechtern den Mittelwert,
    statt ihn zu verbessern.

    Verglichen wird jeder Frame mit dem Median aller Frames. Der Median ist
    unempfindlich gegen einzelne Ausreisser und damit ein robuster Bezug.
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

    # Ausreisser gegen die robuste Streuung der Korrelationen.
    median_correlation = float(np.median(values))
    mad = float(np.median(np.abs(values - median_correlation)))
    threshold = median_correlation - max(3 * 1.4826 * mad, 0.02)
    outliers = [int(i) for i in np.nonzero(values < threshold)[0]]

    # Setzt sich der Finger im Verlauf, muessten die spaeteren Frames besser
    # zum Median passen als die frueheren. Einzelne Ausreisser wuerden diesen
    # Vergleich verfaelschen - je nachdem in welcher Haelfte sie liegen, kaeme
    # ein Trend heraus, den es gar nicht gibt. Deshalb erst bereinigen.
    keep = np.ones(len(values), dtype=bool)
    keep[outliers] = False
    indices = np.nonzero(keep)[0]

    trend = 0.0
    trend_significant = False
    if len(indices) >= 4:
        half = len(indices) // 2
        early, late = indices[:half], indices[half:]
        trend = float(values[late].mean() - values[early].mean())

        # Ein Unterschied zweier Mittelwerte ist nur dann eine Aussage, wenn er
        # das Rauschen der Einzelwerte deutlich uebersteigt. Bei acht Frames
        # ist die Statistik duenn; ohne diese Pruefung meldet schon reines
        # Rauschen einen scheinbaren Trend. Lieber nichts sagen als etwas
        # Falsches.
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
    """Dominante Rippenfrequenz per 2D-Fouriertransformation bestimmen.

    Ein Fingerabdruck ist naeherungsweise ein periodisches Streifenmuster. Im
    Leistungsspektrum erscheint es als Ring um den Ursprung. Der Radius dieses
    Rings sagt, wie viele Pixel auf eine Rippenperiode entfallen.
    """
    image = signal - signal.mean()

    # Fensterung gegen Kantenartefakte: Ohne sie erzeugt der harte Bildrand
    # ein Kreuz im Spektrum, das den echten Ring ueberdecken kann.
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

    # Sehr niedrige Frequenzen sind Helligkeitsverlauf, nicht Rippen.
    # Sehr hohe sind Rauschen. Bei 80 Pixeln deckt r=3..24 Rippenperioden von
    # etwa 23 bis 3 Pixeln ab - der physikalisch sinnvolle Bereich.
    lo, hi = 3, min(24, max_radius)
    band = radial[lo:hi]
    if band.size == 0 or not np.isfinite(band).any():
        return {"valid": False}

    peak_radius = int(np.argmax(band)) + lo

    # Der Ringradius ist ganzzahlig, die echte Frequenz liegt aber dazwischen.
    # Ohne Ausgleich betraegt der Quantisierungsfehler bei einer Periode von
    # zehn Pixeln bereits rund elf Prozent. Eine Parabel durch den Gipfel und
    # seine beiden Nachbarn liefert die Zwischenstelle. In logarithmischer
    # Skala, weil Leistungsspektren exponentiell abfallen.
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
            # Ein sinnvoller Gipfel liegt im eigenen Bin.
            if abs(offset) <= 0.5:
                refined_radius = peak_radius + offset

    # Halb-Bin-Ausgleich. Bin r sammelt alle Radien aus [r, r+1) und
    # repraesentiert damit den Radius r+0.5, nicht r. Ohne diese Korrektur
    # ueberschaetzt das Verfahren die Rippenperiode systematisch um 3 bis 10
    # Prozent; gegen synthetische Muster mit bekannter Periode geprueft, sinkt
    # der mittlere Fehler dadurch von 6,9 auf 1,3 Prozent.
    period_px = image.shape[0] / (refined_radius + 0.5)

    # Wie deutlich ragt der Ring heraus? Ohne echtes Rippenmuster ist das
    # Spektrum flach und der Wert nahe 1.
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
    """Pixel finden, die im Leerbild dauerhaft aus der Reihe fallen.

    Solche Pixel erzeugen in jedem Bild dieselbe Struktur an derselben Stelle.
    Fuer einen Matcher sieht das aus wie ein echtes Merkmal, das jede Person
    gemeinsam hat - ein moeglicher Treiber fuer falsche Impostor-Treffer.
    """
    median = np.median(background)
    # Robuste Streuung: unempfindlich gegen die Ausreisser, die wir suchen.
    mad = np.median(np.abs(background - median))
    robust_std = 1.4826 * mad

    if robust_std == 0:
        return {"count": 0, "fraction": 0.0, "positions": [], "robust_std": 0.0}

    deviation = np.abs(background - median) / robust_std
    mask = deviation > sigma
    ys, xs = np.nonzero(mask)

    height, width = background.shape

    # Geometrie der Auffaelligkeiten. Verstreute Einzelpixel sind etwas ganz
    # anderes als ein systematischer Rand: Einzelpixel interpoliert man weg,
    # einen Rand schneidet man ab.
    row_counts = mask.sum(axis=1)
    column_counts = mask.sum(axis=0)

    # Wie viele der Auffaelligkeiten verschwinden, wenn wir einen Rand der
    # Breite m rundherum ausschliessen?
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
    """Sichtprüfbares Graustufenbild schreiben.

    Nur fuer die Kontrolle durch den Benutzer gedacht. Die Datei enthaelt
    biometrische Daten und wird deshalb im Korpusverzeichnis mit 0600 abgelegt.
    """
    from PIL import Image

    lo, hi = np.percentile(signal, [1, 99])
    scaled = np.clip((signal - lo) / max(hi - lo, 1e-9), 0, 1)
    # Rippen beruehren den Sensor und liefern hohe Werte; invertiert wirken
    # sie dunkel wie auf einem klassischen Abdruck.
    image = Image.fromarray(((1 - scaled) * 255).astype(np.uint8), mode="L")
    image = image.resize((signal.shape[1] * 4, signal.shape[0] * 4),
                         Image.NEAREST)
    image.save(destination)
    destination.chmod(0o600)


def report(path: Path, want_png: bool) -> None:
    frames, background, metadata = load_sample(path)
    signal = signal_image(frames, background)

    print()
    print(f"  Aufnahme: {path.name}")
    print(f"  Person {metadata['subject']}, Finger {metadata['finger']}, "
          f"{metadata['frames']} Frames")
    print()

    quality = quality_metrics(signal, frames)
    print("  Aufnahmequalitaet")
    print(f"    Kontrast:            {quality['contrast']:8.1f}")
    print(f"    Rauschen:            {quality['noise']:8.1f}")
    print(f"    Rauschabstand:       {quality['snr']:8.1f}")
    print(f"    Flaechenabdeckung:   {quality['coverage'] * 100:8.1f} Prozent")
    print()

    stability = frame_stability(frames, background)
    print("  Stabilitaet der acht Frames (lag der Finger still?)")
    if not stability["valid"]:
        print("    Zu wenige Frames fuer eine Aussage.")
    else:
        bars = " ".join(f"{v:.3f}" for v in stability["correlations"])
        print(f"    Uebereinstimmung je Frame: {bars}")
        print(f"    schlechtester {stability['worst']:.3f}, "
              f"bester {stability['best']:.3f}, "
              f"Spanne {stability['spread']:.3f}")

        if stability["outliers"]:
            print(f"    Ausreisser: Frame {stability['outliers']}")
        if not stability["trend_significant"]:
            print("    Kein belegbarer Trend ueber die Frames.")
        elif stability["trend"] > 0:
            print(f"    Trend: spaetere Frames sind besser "
                  f"(+{stability['trend']:.3f}). Der Finger setzt sich noch;")
            print("    die ersten Frames sollten verworfen werden.")
        else:
            print(f"    Trend: spaetere Frames sind schlechter "
                  f"({stability['trend']:.3f}). Der Finger rutscht oder")
            print("    der Kontakt laesst nach; spaete Frames verwerfen.")
    print()

    ridge = ridge_frequency(signal)
    print("  Aufloesung aus der Rippenfrequenz (Hypothese H-02)")
    if not ridge["valid"]:
        print("    Kein auswertbares Muster gefunden.")
    elif ridge["prominence"] < 1.5:
        print(f"    Kein klares Rippenmuster (Deutlichkeit "
              f"{ridge['prominence']:.2f}, noetig > 1.5).")
        print("    Vermutlich zu schwacher Fingerkontakt.")
    elif ridge["prominence"] < 50:
        # Erfahrungswert aus den ersten Messungen: gute Aufnahmen erreichen
        # Deutlichkeiten ueber 100. Darunter wandert der Spektralgipfel
        # merklich ins Rauschen ab und die Periode wird zu kurz geschaetzt.
        print(f"    Schwaches Rippenmuster (Deutlichkeit "
              f"{ridge['prominence']:.1f}; gute Aufnahmen liegen ueber 100).")
        print(f"    Periode {ridge['period_px']:.1f} Pixel, daraus "
              f"{ridge['dpi']:.0f} dpi - aber unzuverlaessig.")
        print("    Diese Aufnahme nicht zur Aufloesungsbestimmung heranziehen.")
    else:
        print(f"    Rippenperiode:       {ridge['period_px']:8.1f} Pixel")
        print(f"    Deutlichkeit:        {ridge['prominence']:8.2f}")
        print(f"    Geschaetzte Aufloesung: {ridge['dpi']:6.0f} dpi "
              f"(Spanne {ridge['dpi_min']:.0f} bis {ridge['dpi_max']:.0f})")
        print()
        deviation = abs(ridge["dpi"] - 500) / 500
        if deviation < 0.15:
            print("    -> vertraeglich mit der von NBIS angenommenen 500 dpi.")
        else:
            print(f"    -> weicht um {deviation * 100:.0f} Prozent von 500 dpi ab.")
            print("       Das wuerde die NBIS-Minutienparameter verstimmen.")
    print()

    defects = defective_pixels(background)
    print("  Auffaellige Pixel im Leerbild")
    print(f"    Anzahl:              {defects['count']:8d} von "
          f"{background.size} ({defects['fraction'] * 100:.2f} Prozent)")
    if defects["count"]:
        print(f"    groesste Abweichung: {defects['max_deviation']:8.1f} Sigma")
        print()
        def summarise(name: str, indices: list[int], total: int) -> None:
            if not indices:
                print(f"      {name:<22} keine")
            elif len(indices) <= 12:
                print(f"      {name:<22} {indices}")
            else:
                print(f"      {name:<22} {len(indices)} von {total} "
                      f"(von {min(indices)} bis {max(indices)})")

        height, width = background.shape
        print("    Geometrie:")
        summarise("betroffene Zeilen:", defects["affected_rows"], height)
        summarise("betroffene Spalten:", defects["affected_columns"], width)
        summarise("komplette Zeilen:", defects["full_rows"], height)
        summarise("komplette Spalten:", defects["full_columns"], width)
        print()
        print("    Wirkung eines Randausschlusses:")
        print(f"      {'Rand':>6} {'verbleibend':>12} {'Restflaeche':>12}")
        for entry in defects["margin_effect"]:
            print(f"      {entry['margin']:6d} {entry['remaining']:12d} "
                  f"{entry['kept_fraction'] * 100:11.1f}%")
        print()

        clean = next(
            (e for e in defects["margin_effect"] if e["remaining"] == 0), None
        )
        if clean:
            print(f"    -> Ein Rand von {clean['margin']} Pixeln entfernt alle "
                  f"Auffaelligkeiten")
            print(f"       und behaelt {clean['kept_fraction'] * 100:.1f} Prozent "
                  f"der Flaeche ({clean['kept_pixels']} Pixel).")
            libfprint_pixels = 50 * background.shape[1]
            print(f"       Zum Vergleich: libfprint behaelt "
                  f"{libfprint_pixels} Pixel "
                  f"({libfprint_pixels / background.size * 100:.1f} Prozent).")
        else:
            print("    -> Kein Randausschluss bis 8 Pixel raeumt alles ab.")
            print("       Es gibt also auch verstreute Einzeldefekte im Inneren,")
            print("       die interpoliert statt abgeschnitten werden muessen.")
    else:
        print("    Keine. Der Sensor ist gleichmaessig.")
    print()

    if want_png:
        destination = path.with_suffix(".png")
        export_png(signal, destination)
        print(f"  Sichtkontrolle gespeichert: {destination}")
        print("  (liegt im root-geschuetzten Korpus, nicht im Projekt)")
        print()


def selftest() -> int:
    """Die Aufloesungsmessung gegen Muster mit bekannter Periode pruefen.

    Ein Messwerkzeug, das nie gegen eine bekannte Wahrheit geprueft wurde, ist
    kein Messwerkzeug. Genau dieser Test hat einen systematischen Halb-Bin-
    Versatz aufgedeckt, der die Periode um bis zu zehn Prozent zu gross
    ausgab.
    """
    rng = np.random.default_rng(42)
    periods = [5.0, 6.0, 7.0, 8.0, 9.0, 9.5, 10.0, 11.0, 12.0, 14.0]
    errors = []

    print()
    print("  Selbsttest der Rippenfrequenz-Messung")
    print(f"  {'vorgegeben':>12} {'gemessen':>10} {'Fehler':>9}")

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
    print(f"  mittlerer Fehler: {np.mean(errors):.1f} Prozent")
    print(f"  groesster Fehler: {max(errors):.1f} Prozent")
    print(f"  Rauschkontrolle:  Deutlichkeit {noise_prominence:.2f} "
          f"(muss unter 1.5 liegen)")
    print()

    ok = max(errors) < 5.0 and noise_prominence < 1.5
    print(f"  Ergebnis: {'bestanden' if ok else 'NICHT BESTANDEN'}")
    print()
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline-Auswertung einer Rohaufnahme.")
    parser.add_argument("samples", nargs="*", type=Path)
    parser.add_argument("--png", action="store_true",
                        help="Graustufenbild zur Sichtkontrolle schreiben")
    parser.add_argument("--selftest", action="store_true",
                        help="Messmethode gegen bekannte Muster pruefen")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if not args.samples:
        parser.error("Bitte mindestens eine Aufnahme angeben oder --selftest.")

    for path in args.samples:
        if not path.exists():
            print(f"Nicht gefunden: {path}", file=sys.stderr)
            return 1
        report(path, args.png)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
