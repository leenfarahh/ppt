"""Colour comparison helpers.

Hex equality is the wrong test for brand compliance: a deck pasted through
three tools ends up with 1F2A45 where the brand says 1F2A44, which is
invisible on screen and not worth reporting. Perceptual distance is.
"""

from __future__ import annotations

from typing import Optional

_D65 = (95.047, 100.000, 108.883)


def hex_to_rgb(value: str) -> Optional[tuple[int, int, int]]:
    """Parse "#1F2A44" / "1f2a44" into 0-255 components."""
    if not value:
        return None
    cleaned = value.strip().lstrip("#").upper()
    if len(cleaned) == 3:
        cleaned = "".join(c * 2 for c in cleaned)
    if len(cleaned) not in (6, 8):
        return None
    try:
        return (
            int(cleaned[0:2], 16),
            int(cleaned[2:4], 16),
            int(cleaned[4:6], 16),
        )
    except ValueError:
        return None


def srgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """sRGB (0-255, D65) to CIE L*a*b*."""
    linear = []
    for channel in rgb:
        c = channel / 255.0
        linear.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = linear

    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) * 100.0
    y = (r * 0.2126 + g * 0.7152 + b * 0.0722) * 100.0
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) * 100.0

    def f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > 0.008856 else (7.787 * t) + (16.0 / 116.0)

    fx, fy, fz = f(x / _D65[0]), f(y / _D65[1]), f(z / _D65[2])
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def delta_e(hex_a: str, hex_b: str) -> Optional[float]:
    """Perceptual distance between two hex colours.

    Currently CIE76 (Euclidean in Lab). Roughly: under 1 is imperceptible,
    2-3 is a trained eye at close range, over 5 reads as a different colour.

    TODO: swap in CIEDE2000. CIE76 overstates distance in the blues and
    understates it in the greens, which matters for palettes built around a
    single hue -- the default tolerance in Tolerances.color_delta_e is
    calibrated for CIEDE2000 and will need re-checking either way.
    """
    a, b = hex_to_rgb(hex_a), hex_to_rgb(hex_b)
    if a is None or b is None:
        return None
    la, lb = srgb_to_lab(a), srgb_to_lab(b)
    return sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5


def nearest_palette_entry(
    value: str,
    palette: dict[str, str],
) -> tuple[Optional[str], Optional[float]]:
    """Closest palette entry to `value`, as (label, distance).

    Drives the two halves of a colour finding: whether it is off-palette at
    all, and what it should probably have been.
    """
    best_label: Optional[str] = None
    best_distance: Optional[float] = None
    for label, palette_hex in palette.items():
        distance = delta_e(value, palette_hex)
        if distance is None:
            continue
        if best_distance is None or distance < best_distance:
            best_label, best_distance = label, distance
    return best_label, best_distance
