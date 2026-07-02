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

Calibrated against a real captured example (see CHANGELOG / CLAUDE.md
for the full account): an FBNeo "Romset is unknown" error dialog,
1920x1080, light-grey dialog body at a flat (240,240,240) with a
blue title bar, centred over a decorative per-game/per-theme bezel
that varies wildly in colour (observed: pure black in one spot, vivid
magenta in another within the same frame). Checking the WHOLE frame
for uniformity gave only 68.1% grey — comfortably below any sane
threshold, a false negative, because the bezel pulls the average
down. Restricting to the center region (margins excluded on all four
sides, not just left/right — a CRT shader overlay or bezel can affect
top/bottom too) gave 99.1%. The margin is doing the real work here,
not the exact threshold chosen below it.
"""

from __future__ import annotations  # Python 3.9 compatibility

from modules.common import pngdecoder


# Default analysis parameters. All margins are FRACTIONS of width/
# height, not pixel counts — this is what makes the same defaults
# work unchanged on a 1080p capture and a 4K one. A 15% margin is
# 162px on a 1080p-tall frame and 324px on a 4K-tall one; the same
# proportion of the frame gets excluded either way.
DEFAULT_MARGIN_HORIZONTAL = 0.15   # exclude this fraction from BOTH left and right
DEFAULT_MARGIN_VERTICAL   = 0.15   # exclude this fraction from BOTH top and bottom
DEFAULT_SATURATION_TOLERANCE = 20  # max(R,G,B) - min(R,G,B) below this counts as "grey"
DEFAULT_MIN_BRIGHTNESS    = 200    # mean(R,G,B) above this counts as "light"
DEFAULT_THRESHOLD_PCT     = 85.0   # % of sampled center-region pixels that must
                                    # be near-grey to flag as likely blank/error.
                                    # Real calibration example measured 99.1% —
                                    # 85% leaves real headroom below that for
                                    # colour-profile drift across setups, and
                                    # for screens with several lines of black
                                    # text (more text = more non-grey pixels,
                                    # but text is sparse relative to the dialog
                                    # area even with multiple missing-file lines).
DEFAULT_SAMPLE_STRIDE     = 4       # check every Nth pixel in each direction —
                                    # 1/16 of total pixels, plenty for this and
                                    # much faster, especially relevant at 4K.


def _is_near_grey(
    px: tuple, saturation_tolerance: int, min_brightness: int
) -> bool:
    """Low saturation (R,G,B close together — some shade of grey,
    not a colour) AND high brightness (a LIGHT grey specifically,
    not catching dark backgrounds or black borders too)."""
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

    Samples a center region of the frame — margin_horizontal excluded
    from BOTH the left and right edges, margin_vertical excluded from
    BOTH the top and bottom — and checks what fraction of sampled
    pixels in that region are a light, low-saturation grey. Margins
    are fractions of the frame's own width/height, so the same
    defaults apply unchanged regardless of capture resolution.

    Args:
        png_path:              Path to the screenshot PNG.
        margin_horizontal:      Fraction (0.0-0.5) to exclude from
                                each of the left and right edges.
        margin_vertical:        Fraction (0.0-0.5) to exclude from
                                each of the top and bottom edges.
        saturation_tolerance:   Max channel spread to count as "grey".
        min_brightness:         Min mean channel value to count as
                                "light" rather than a dark/black area.
        threshold_pct:          % of sampled center pixels that must
                                be near-grey to flag as likely blank.
        sample_stride:          Check every Nth pixel per axis rather
                                than every pixel — for speed, and it's
                                more than enough resolution for this.

    Returns:
        dict with:
            'flagged':       bool — True if grey_pct >= threshold_pct
            'grey_pct':      float — actual percentage measured
            'sampled':       int — how many pixels were sampled
            'region':        (x1, y1, x2, y2) — actual pixel bounds analysed
            'width', 'height': the decoded image dimensions
            'error':         str or None — set if the PNG couldn't be
                             read/decoded; 'flagged' is always False
                             in that case rather than raising, since a
                             failed analysis should never be mistaken
                             for "definitely not blank"
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
            'error': 'Margins leave no region to sample '
                     '(image too small for these margins)',
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
