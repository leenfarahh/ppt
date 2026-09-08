"""Colour comparison helpers.

Hex equality is the wrong test for brand compliance: a deck pasted through
three tools ends up with 1F2A45 where the brand says 1F2A44, which is
invisible on screen and not worth reporting. Perceptual distance is.
"""

from __future__ import annotations

from math import atan2, cos, degrees, exp, hypot, radians, sin, sqrt
from typing import Optional

_D65 = (95.047, 100.000, 108.883)

# Chroma below this is a grey, a black or a white. Set where PowerPoint's own
# neutrals fall: the Office theme greys measure under 3, and a colour a
# designer would call 'a blue' clears 15 comfortably. What sits between is
# genuinely ambiguous, and the cost of calling one of those a colour is that a
# fix stands down, which is the safe direction.
NEUTRAL_CHROMA = 8.0

# Degrees of hue two colours can differ by and still be the same family. A red
# and an orange are 40 apart and are not the same decision.
HUE_TOLERANCE = 35.0


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
    """Perceptual distance between two hex colours, CIEDE2000.

    Roughly: under 1 is imperceptible, 2-3 is a trained eye at close range,
    over 5 reads as a different colour. `Tolerances.color_delta_e` was
    calibrated for this metric all along.

    It replaced CIE76, which is a plain Euclidean distance in Lab and wrong in
    the way that mattered here: it overstates distance in the blues, so a
    brand blue measured further from every other blue than it really is, and
    the nearest entry to it came out a neutral. On a real deck that is what
    turned a page of blue headings and two-tone icons grey.
    """
    a, b = hex_to_rgb(hex_a), hex_to_rgb(hex_b)
    if a is None or b is None:
        return None
    return _ciede2000(srgb_to_lab(a), srgb_to_lab(b))


def _ciede2000(
    lab_a: tuple[float, float, float], lab_b: tuple[float, float, float]
) -> float:
    """CIEDE2000 between two Lab triples, with unit weights (kL=kC=kH=1).

    Written out rather than pulled in, because the whole dependency would be
    this function. Checked against the Sharma, Wu and Dalal reference pairs,
    which exist precisely because the hue-angle wrap-arounds in here are easy
    to get subtly wrong and hard to notice.
    """
    l1, a1, b1 = lab_a
    l2, a2, b2 = lab_b

    c1 = hypot(a1, b1)
    c2 = hypot(a2, b2)
    c_bar = (c1 + c2) / 2.0
    # The a* correction that pulls the near-neutral axis into shape. 25**7 is
    # the constant from the standard, not a tuning knob.
    g = 0.5 * (1.0 - sqrt(c_bar ** 7 / (c_bar ** 7 + 25.0 ** 7))) if c_bar else 0.0

    a1p, a2p = (1.0 + g) * a1, (1.0 + g) * a2
    c1p, c2p = hypot(a1p, b1), hypot(a2p, b2)
    h1p = _hue(b1, a1p)
    h2p = _hue(b2, a2p)

    dlp = l2 - l1
    dcp = c2p - c1p

    if c1p * c2p == 0.0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180.0:
        dhp = h2p - h1p
    elif h2p - h1p > 180.0:
        dhp = h2p - h1p - 360.0
    else:
        dhp = h2p - h1p + 360.0
    dHp = 2.0 * sqrt(c1p * c2p) * sin(radians(dhp) / 2.0)

    lp_bar = (l1 + l2) / 2.0
    cp_bar = (c1p + c2p) / 2.0

    if c1p * c2p == 0.0:
        hp_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180.0:
        hp_bar = (h1p + h2p) / 2.0
    elif h1p + h2p < 360.0:
        hp_bar = (h1p + h2p + 360.0) / 2.0
    else:
        hp_bar = (h1p + h2p - 360.0) / 2.0

    t = (
        1.0
        - 0.17 * cos(radians(hp_bar - 30.0))
        + 0.24 * cos(radians(2.0 * hp_bar))
        + 0.32 * cos(radians(3.0 * hp_bar + 6.0))
        - 0.20 * cos(radians(4.0 * hp_bar - 63.0))
    )
    d_theta = 30.0 * exp(-(((hp_bar - 275.0) / 25.0) ** 2))
    rc = 2.0 * sqrt(cp_bar ** 7 / (cp_bar ** 7 + 25.0 ** 7)) if cp_bar else 0.0
    sl = 1.0 + (0.015 * (lp_bar - 50.0) ** 2) / sqrt(20.0 + (lp_bar - 50.0) ** 2)
    sc = 1.0 + 0.045 * cp_bar
    sh = 1.0 + 0.015 * cp_bar * t
    rt = -sin(radians(2.0 * d_theta)) * rc

    return sqrt(
        (dlp / sl) ** 2
        + (dcp / sc) ** 2
        + (dHp / sh) ** 2
        + rt * (dcp / sc) * (dHp / sh)
    )


def _hue(b: float, ap: float) -> float:
    """Hue angle in degrees, 0-360, and 0 for a colour with no hue at all."""
    if ap == 0.0 and b == 0.0:
        return 0.0
    angle = degrees(atan2(b, ap))
    return angle + 360.0 if angle < 0.0 else angle


def chroma_of(hex_value: str) -> Optional[float]:
    """How much colour a colour has. Near zero is a grey, a black or a white.

    The number that decides whether an entry is a candidate at all: a heading
    blue and a dark grey can sit close in Lab, and swapping one for the other
    is not a correction, it is a different design.
    """
    rgb = hex_to_rgb(hex_value)
    if rgb is None:
        return None
    _l, a, b = srgb_to_lab(rgb)
    return hypot(a, b)


def hue_of(hex_value: str) -> Optional[float]:
    """Hue angle in degrees, or None for a colour that has no hue."""
    rgb = hex_to_rgb(hex_value)
    if rgb is None:
        return None
    _l, a, b = srgb_to_lab(rgb)
    if hypot(a, b) < NEUTRAL_CHROMA:
        return None
    return _hue(b, a)


def nearest_palette_entry(
    value: str,
    palette: dict[str, str],
) -> tuple[Optional[str], Optional[float]]:
    """Closest palette entry to `value`, as (label, distance).

    Answers one question only: is this colour on the palette. It is the wrong
    function to ask what a colour should become -- see `intended_palette_entry`
    -- and conflating the two is what recoloured a red to an orange 33 delta-E
    away, because "nearest" is still nearest when nothing is near.
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


def intended_palette_entry(
    value: str,
    palette: dict[str, str],
    limit: float,
) -> tuple[Optional[str], Optional[float]]:
    """The entry this colour was probably meant to be, or (None, None).

    A different question from "which is nearest", and one that is allowed to
    have no answer. Three things disqualify an entry however close it measures:

    - It is a neutral and the colour is not, or the other way round. A grey and
      a desaturated blue sit close in Lab and are not the same decision; on a
      real deck this is what turned a page of blue headings grey.
    - It is a different hue family. Nearest put a red on an orange because the
      palette held no red; the honest answer there is that the palette holds
      no red.
    - It is further away than `limit`, which is the distance past which the
      rule already declined to recommend anything. Applying a target the rule
      would not name is the bug this exists to close.

    None means a designer picks. That is a better answer than a colour chosen
    by arithmetic that had nothing suitable to choose from.
    """
    source_chroma = chroma_of(value)
    if source_chroma is None:
        return None, None
    source_neutral = source_chroma < NEUTRAL_CHROMA
    source_hue = hue_of(value)

    best_label: Optional[str] = None
    best_distance: Optional[float] = None
    for label, palette_hex in palette.items():
        distance = delta_e(value, palette_hex)
        if distance is None or distance > limit:
            continue
        entry_chroma = chroma_of(palette_hex)
        if entry_chroma is None:
            continue
        if (entry_chroma < NEUTRAL_CHROMA) != source_neutral:
            continue
        if not source_neutral:
            entry_hue = hue_of(palette_hex)
            if entry_hue is None or _hue_gap(source_hue, entry_hue) > HUE_TOLERANCE:
                continue
        if best_distance is None or distance < best_distance:
            best_label, best_distance = label, distance
    return best_label, best_distance


def _hue_gap(a: Optional[float], b: Optional[float]) -> float:
    """Degrees between two hue angles the short way round the wheel."""
    if a is None or b is None:
        return 360.0
    gap = abs(a - b) % 360.0
    return 360.0 - gap if gap > 180.0 else gap
