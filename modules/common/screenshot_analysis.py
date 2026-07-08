"""
modules/common/screenshot_analysis.py — Heuristic blank/error-screen
detection from a captured screenshot.

Built specifically to catch the case run_test()'s log-based detection
cannot: a core (FBNeo in the confirmed case) reports a clean OK while
the screen actually shows an error dialog or blank content, with
nothing recognisable in the logs to flag it. See UNVERIFIED_CORES in
autofix.py — this module is what a forced verification screenshot
gets analysed with, before a human ever needs to look at it.

Deliberately a FLAG, not a verdict. This heuristic can be wrong in
either direction — a legitimate minimalist or fading screen could
trip it, an error dialog with unusually little blank space around the
text could just barely miss it. The result is meant to be appended to
a result's notes for a human glance, exactly like UNVERIFIED_CORES
does, never to silently override a status on its own.

Calibrated against a real captured example: an FBNeo "Romset is unknown"
error dialog, 1920x1080, light-grey dialog body at a flat (240,240,240)
with a blue title bar, centred over a decorative per-game/per-theme bezel.
Checking the WHOLE frame gave only 68.1% grey — the bezel pulls it down.
Restricting to the center region gave 99.1%. The margin is doing the
real work here, not the exact threshold chosen below it.

NOTE: A uniform-colour check (for solid blue, black etc.) was prototyped
but reverted — MAME/arcade black startup screens and coloured BIOS screens
would produce widespread false positives without per-system calibration.
Reserved for Heuristics Level 2 once proper calibration examples exist.
"""

from __future__ import annotations  # Python 3.9 compatibility

from modules.common import pngdecoder


DEFAULT_MARGIN_HORIZONTAL = 0.15
DEFAULT_MARGIN_VERTICAL   = 0.15
DEFAULT_SATURATION_TOLERANCE = 20
DEFAULT_MIN_BRIGHTNESS    = 200
DEFAULT_THRESHOLD_PCT     = 85.0
DEFAULT_SAMPLE_STRIDE     = 4


def _is_near_grey(
    px: tuple, saturation_tolerance: int, min_brightness: int
) -> bool:
    """Low saturation AND high brightness — light grey specifically."""
    r, g, b = px[0], px[1], px[2]
    if max(r, g, b) - min(r, g, b) > saturation_tolerance:
        return False
    if (r + g + b) / 3 < min_brightness:
        return False
    return True


def analyze(
    png_path: str,
    margin_horizontal: float = DEFAULT_MARGIN_HORIZONTAL,
    margin_vertical: float = DEFAULT_MARGIN_VERTICAL,
    saturation_tolerance: int = DEFAULT_SATURATION_TOLERANCE,
    min_brightness: int = DEFAULT_MIN_BRIGHTNESS,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    sample_stride: int = DEFAULT_SAMPLE_STRIDE,
) -> dict:
    """
    Analyse a screenshot for a likely blank/error screen.

    Returns dict with: flagged, grey_pct, sampled, region,
    width, height, error.
    """
    try:
        with open(png_path, 'rb') as f:
            png_bytes = f.read()
        width, height, pixel_data, channels = pngdecoder.decode(png_bytes)
    except (OSError, pngdecoder.PNGDecodeError) as e:
        return {
            'flagged': False, 'grey_pct': 0.0, 'sampled': 0,
            'region': None, 'width': None, 'height': None,
            'error': str(e),
        }

    margin_x = int(width * margin_horizontal)
    margin_y = int(height * margin_vertical)
    x1, x2 = margin_x, width - margin_x
    y1, y2 = margin_y, height - margin_y

    if x2 <= x1 or y2 <= y1:
        return {
            'flagged': False, 'grey_pct': 0.0, 'sampled': 0,
            'region': (x1, y1, x2, y2), 'width': width, 'height': height,
            'error': 'Margins leave no region to sample',
        }

    total = 0
    grey_count = 0
    for y in range(y1, y2, sample_stride):
        for x in range(x1, x2, sample_stride):
            px = pngdecoder.get_pixel(pixel_data, width, channels, x, y)
            total += 1
            if _is_near_grey(px, saturation_tolerance, min_brightness):
                grey_count += 1

    grey_pct = (100.0 * grey_count / total) if total else 0.0

    return {
        'flagged': grey_pct >= threshold_pct,
        'grey_pct': grey_pct,
        'sampled': total,
        'region': (x1, y1, x2, y2),
        'width': width,
        'height': height,
        'error': None,
    }
