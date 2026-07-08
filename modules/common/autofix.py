"""
Automatic ROM fix attempts for the ROM Audit Tool.

When a ROM fails the audit, this module tries a series of known-good
core and emulator combinations in batocera.conf before giving up and
marking the ROM as a genuine unrecoverable error.

The combinations are defined per system type. Each combination is
validated against installed libretro cores before being attempted —
combinations referencing unavailable cores are silently skipped,
preventing false GENUINE ERROR results from missing cores.

Fix workflow for each ROM:
    1. Filter combinations to installed cores only (checked once per session)
    2. Back up batocera.conf (once per autofix session)
    3. For each valid combination in order:
        a. Write the combination to batocera.conf
        b. Test the ROM using the audit's run_test() function
        c. If OK: leave the entry in place, mark as FIXED, stop
        d. If still failing: try the next combination
    4. If all combinations fail or none are available:
        a. Remove all per-game entries from batocera.conf
        b. Mark as GENUINE ERROR
"""

from __future__ import annotations  # Python 3.7 compatibility

import os
import glob
from modules.common.logging import log
from modules.common import configeditor
from modules.common import screenshot_analysis


# ---------------------------------------------------------------------------
# Libretro core path
# ---------------------------------------------------------------------------

LIBRETRO_CORE_PATH = "/usr/lib/libretro"

# Known standalone (non-libretro) emulator binary paths.
# Each entry maps emulator name → list of candidate binary locations.
# The first path that exists is treated as installed.
STANDALONE_EMULATOR_PATHS: dict[str, list[str]] = {
    'duckstation': [
        '/usr/bin/duckstation',
        '/usr/bin/duckstation-nogui',
        '/usr/games/duckstation',
    ],
    'drastic': [
        '/usr/bin/drastic',
        '/usr/games/drastic',
        '/opt/drastic/bin/drastic',
    ],
}


# ---------------------------------------------------------------------------
# Fix combinations
# ---------------------------------------------------------------------------

# Tuple format: (test_system, conf_prefix, core, emulator)
#
#   test_system:  System name passed to emulatorlauncher (-system arg)
#   conf_prefix:  System prefix used in the batocera.conf entry
#   core:         Libretro core name (without _libretro.so suffix)
#   emulator:     Emulator name (always libretro for these combinations)

MAME_FIX_COMBINATIONS = [
    ('mame',  'mame',  'mame',        'libretro'),
    ('mame',  'mame',  'mame078plus', 'libretro'),
    # mame0139 covers the MAME 0.139 ROM set — a large swathe of mid-era
    # arcade titles (bionicc, etc.) that need neither 0.78 nor current MAME.
    # Equivalent to lr-mame2010 on RetroPie.
    ('mame',  'mame',  'mame0139',    'libretro'),
    ('fbneo', 'fbneo', 'mame',        'libretro'),
    ('fbneo', 'fbneo', 'mame078plus', 'libretro'),
    ('fbneo', 'fbneo', 'mame0139',    'libretro'),
    # FBNeo core running under the mame system — tried LAST deliberately.
    # FBNeo silently shows a grey screen on bad/unsupported dumps with zero
    # error text logged, making a real failure indistinguishable from a
    # genuine pass (UNVERIFIED_CORES exists specifically because of this).
    # Previously sat at position 4/7, causing autofix to stop there on a
    # grey-screen result before the remaining combinations were ever tried.
    # Moving it last ensures every non-masking core is exhausted first.
    ('mame',  'mame',  'fbneo',       'libretro'),
]

PSX_FIX_COMBINATIONS = [
    ('psx', 'psx', 'pcsx_rearmed', 'libretro'),
    ('psx', 'psx', 'swanstation',  'libretro'),
    # duckstation standalone — better compatibility for many titles
    # that hang or fail under the libretro cores
    ('psx', 'psx', 'duckstation',  'duckstation'),
]

N64_FIX_COMBINATIONS = [
    ('n64', 'n64', 'mupen64plus-next', 'libretro'),
    ('n64', 'n64', 'glide64mk2',       'libretro'),
    ('n64', 'n64', 'parallel_n64',     'libretro'),
]

MEGADRIVE_FIX_COMBINATIONS = [
    ('megadrive', 'megadrive', 'genesis_plus_gx', 'libretro'),
    ('megadrive', 'megadrive', 'picodrive',        'libretro'),
    ('megadrive', 'megadrive', 'blastem',          'libretro'),
]

NDS_FIX_COMBINATIONS = [
    # melonds first — better compatibility and correct dual-screen layout.
    # drastic standalone has OpenGL rendering issues on some hardware
    # (triangle artefact on one screen) so it is tried last as a fallback.
    ('nds', 'nds', 'melonds',   'libretro'),
    ('nds', 'nds', 'desmume',   'libretro'),
    ('nds', 'nds', 'desmume2015', 'libretro'),
    ('nds', 'nds', 'drastic',   'drastic'),
]

SNES_FIX_COMBINATIONS = [
    ('snes', 'snes', 'snes9x',      'libretro'),
    ('snes', 'snes', 'bsnes',       'libretro'),
    ('snes', 'snes', 'snes9x_next', 'libretro'),
]

GBA_FIX_COMBINATIONS = [
    ('gba', 'gba', 'mgba',  'libretro'),
    ('gba', 'gba', 'vba-m', 'libretro'),
]

GB_FIX_COMBINATIONS = [
    ('gb', 'gb', 'gambatte', 'libretro'),
    ('gb', 'gb', 'mgba',     'libretro'),
    ('gb', 'gb', 'tgbdual',  'libretro'),
]

GBC_FIX_COMBINATIONS = [
    ('gbc', 'gbc', 'gambatte', 'libretro'),
    ('gbc', 'gbc', 'mgba',     'libretro'),
]

NES_FIX_COMBINATIONS = [
    ('nes', 'nes', 'fceumm',   'libretro'),
    ('nes', 'nes', 'nestopia', 'libretro'),
    ('nes', 'nes', 'mesen',    'libretro'),
]

PCENGINE_FIX_COMBINATIONS = [
    ('pcengine', 'pcengine', 'mednafen_pce',      'libretro'),
    ('pcengine', 'pcengine', 'mednafen_pce_fast', 'libretro'),
    ('pcengine', 'pcengine', 'pce_fast',          'libretro'),
]

NAOMI_FIX_COMBINATIONS = [
    ('naomi', 'naomi', 'flycast', 'libretro'),  # Best compatibility
    ('naomi', 'naomi', 'mame',    'libretro'),  # Fallback
]

NAOMI2_FIX_COMBINATIONS = [
    ('naomi2', 'naomi2', 'flycast', 'libretro'),
    ('naomi2', 'naomi2', 'mame',    'libretro'),
]

# Map from system folder name to its fix combinations.
# Add entries here to support autofix for additional system types.
# Cores whose successful-launch criteria (clean exit, no error text,
# survives the full display window) is known to be unreliable as a
# verdict on its own. FBNeo's documented behaviour on an unrecognised
# or bad dump is to silently grey-screen rather than log anything —
# indistinguishable from genuine success using exactly the same
# detection every other candidate in this loop is judged by. Confirmed
# directly, reproduced live: autofix accepted FBNeo as a "fix" for a
# ROM that was visibly grey-screened with errors at the moment of
# capture. Any core in this set gets a forced screenshot for that
# specific attempt (regardless of the audit's own --screenshot
# setting) and an explicit "UNVERIFIED" flag in the result notes,
# rather than being trusted the same way a normal OK is.
UNVERIFIED_CORES = {'fbneo'}

FIX_COMBINATIONS = {
    # Batocera names
    'mame':         MAME_FIX_COMBINATIONS,
    'fbneo':        MAME_FIX_COMBINATIONS,
    'megadrive':    MEGADRIVE_FIX_COMBINATIONS,
    # RetroPie equivalents
    'arcade':       MAME_FIX_COMBINATIONS,
    'mame-libretro':MAME_FIX_COMBINATIONS,
    'fba':          MAME_FIX_COMBINATIONS,
    'genesis':      MEGADRIVE_FIX_COMBINATIONS,
    # Common to both
    'psx':          PSX_FIX_COMBINATIONS,
    'n64':          N64_FIX_COMBINATIONS,
    'snes':         SNES_FIX_COMBINATIONS,
    'gba':          GBA_FIX_COMBINATIONS,
    'gb':           GB_FIX_COMBINATIONS,
    'gbc':          GBC_FIX_COMBINATIONS,
    'nes':          NES_FIX_COMBINATIONS,
    'pcengine':     PCENGINE_FIX_COMBINATIONS,
    'nds':          NDS_FIX_COMBINATIONS,
    'naomi':        NAOMI_FIX_COMBINATIONS,
    'naomi2':       NAOMI_FIX_COMBINATIONS,
    'atomiswave':   NAOMI_FIX_COMBINATIONS,
}


# ---------------------------------------------------------------------------
# Core availability checking
# ---------------------------------------------------------------------------

def _get_global_default_core(conf_path: str, system: str) -> str | None:
    """
    Read the global system-level default core from batocera.conf.

    Looks for entries of the form:  system.core=corename
    These are system-wide defaults (not per-game overrides).

    Used by attempt_autofix to avoid writing redundant per-game entries
    when the winning core already matches the global default. For example,
    if batocera.conf has 'mame.core=fbneo' and a ROM fixes with fbneo,
    the per-game entry is unnecessary — the global covers it already.

    Args:
        conf_path: Full path to batocera.conf.
        system:    System prefix e.g. 'mame', 'fbneo'.

    Returns:
        Core name string e.g. 'fbneo', or None if no global default set.
    """
    import re
    try:
        with open(conf_path, 'r') as f:
            content = f.read()
        match = re.search(
            rf'^{re.escape(system)}\.core=(\S+)',
            content,
            re.MULTILINE
        )
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def get_installed_cores(core_path: str = LIBRETRO_CORE_PATH) -> set[str]:
    """
    Return the set of installed libretro core names.

    Scans the libretro directory for .so files and extracts the core
    name by stripping the _libretro.so suffix. For example:
        mame_libretro.so        -> 'mame'
        snes9x_libretro.so      -> 'snes9x'
        mupen64plus-next_libretro.so -> 'mupen64plus-next'

    Args:
        core_path: Path to the libretro cores directory.

    Returns:
        Set of installed core name strings.
    """
    installed = set()
    try:
        for path in glob.glob(os.path.join(core_path, '*_libretro.so')):
            filename = os.path.basename(path)
            core_name = filename.replace('_libretro.so', '')
            installed.add(core_name)
    except Exception as e:
        log(f"  Warning: could not scan cores directory: {e}")
    return installed


def _is_standalone_available(emulator: str) -> bool:
    """
    Check whether a standalone (non-libretro) emulator is installed.

    Checks each candidate binary path from STANDALONE_EMULATOR_PATHS.
    Returns False for any emulator not listed in that dict.

    Args:
        emulator: Emulator name e.g. 'duckstation'.

    Returns:
        True if at least one candidate binary path exists.
    """
    candidates = STANDALONE_EMULATOR_PATHS.get(emulator, [])
    return any(os.path.exists(p) for p in candidates)


def filter_combinations(
    combinations: list[tuple],
    installed_cores: set[str]
) -> list[tuple]:
    """
    Filter a list of fix combinations to only those with installed cores.

    Args:
        combinations:    Full list of (test_system, conf_prefix, core, emu).
        installed_cores: Set of installed core names from get_installed_cores().

    Returns:
        Filtered list containing only combinations whose core is installed.
    """
    result = []
    for combo in combinations:
        _, _, core, emulator = combo
        if emulator == 'libretro':
            # Standard libretro core — check .so presence
            if core in installed_cores:
                result.append(combo)
        else:
            # Standalone emulator — check binary presence
            if _is_standalone_available(emulator):
                result.append(combo)
    return result


def get_combinations(
    system: str,
    installed_cores: set[str] = None
) -> list[tuple]:
    """
    Return fix combinations for a system, filtered to installed cores.

    If installed_cores is not provided, the check is skipped and all
    defined combinations are returned. Pass the result of
    get_installed_cores() for proper filtering.

    Args:
        system:          System folder name e.g. 'mame', 'snes'.
        installed_cores: Set of installed core names, or None to skip check.

    Returns:
        List of (test_system, conf_prefix, core, emulator) tuples,
        filtered to installed cores. Empty list if system not supported.
    """
    combinations = FIX_COMBINATIONS.get(system, [])
    if installed_cores is not None:
        combinations = filter_combinations(combinations, installed_cores)
    return combinations


def log_available_combinations(installed_cores: set[str]) -> None:
    """
    Log which fix combinations are available on this system.

    Called once at startup when --autofix is active so the operator
    knows upfront which systems will benefit from autofix and which
    combinations have been excluded due to missing cores.

    Args:
        installed_cores: Set of installed core names.
    """
    log("Autofix core availability:")
    for system, combinations in FIX_COMBINATIONS.items():
        available = filter_combinations(combinations, installed_cores)
        skipped   = [c for c in combinations if c not in available]

        if available:
            # Deduplicate core names for display - system prefix is
            # an implementation detail not relevant to the operator
            cores = ', '.join(dict.fromkeys(c[2] for c in available))
            log(f"  [{system}] {len(available)} combination(s): {cores}")
        if skipped:
            missing = ', '.join(dict.fromkeys(c[2] for c in skipped))
            log(f"  [{system}] Skipped (core not installed): {missing}")


# ---------------------------------------------------------------------------
# Autofix orchestration
# ---------------------------------------------------------------------------

def verify_unverified_core(
    core: str,
    screenshot_path: str,
    heuristic: bool,
    dashboard=None,
    state: dict = None,
) -> tuple[str, str]:
    """
    Shared verification-flagging logic for any result that came from
    a core in UNVERIFIED_CORES.

    Used by both attempt_autofix()'s own loop AND the pre-check in
    rom_audit.py's main loop for the regular (non-autofix) test path.
    Deliberately extracted here rather than left inline in the
    autofix loop specifically because of bandit.zip: a ROM with an
    EXISTING per-game config entry pointing at fbneo sailed through
    the regular test path with a plain, untrusted OK — this logic
    only ever lived inside the autofix loop, never the everyday path
    most ROMs actually go through. One shared function means both
    call sites stay in sync rather than risk drifting the same way
    again later.

    Decision flow (the actual point of this function, not just the
    notes text):
        - core not in UNVERIFIED_CORES → no change at all, ('', '')
        - heuristic off (no pixel check possible) → NEEDS REVIEW.
          A log-only OK from one of these cores cannot be trusted on
          its own — that's the entire premise of UNVERIFIED_CORES —
          so with nothing to actually check it against, the honest
          default is "needs a human", not "assume fine".
        - heuristic on, pixel check flags it (mostly one flat light
          colour) → NEEDS REVIEW. This is the actual masked-failure
          signature.
        - heuristic on, pixel check does NOT flag it (real, varied
          content — including a mostly-black BIOS/init screen with a
          little text, confirmed safe against real examples) → no
          override, stays OK/FIXED. The heuristic clearing it is
          treated as positive confirmation, not just "no evidence of
          a problem".
        - heuristic on, pixel check itself errors (missing file,
          decode failure) → NEEDS REVIEW. Same reasoning as the
          heuristic-off case: no confirmation available, don't assume
          fine by default.

    Logs and updates the dashboard status before running pixel
    analysis specifically — the decode step can take several seconds
    at higher capture resolutions (pure-Python PNG decoding, no
    Pillow), and without this, the log goes quiet for that whole
    stretch with no indication anything is still happening rather
    than having hung.

    Returns:
        (status_override, notes_suffix) — status_override is
        'NEEDS REVIEW' or '' (meaning: don't change the caller's
        status). notes_suffix is always safe to append to existing
        notes text, '' when there's nothing to add.
    """
    if core not in UNVERIFIED_CORES:
        return '', ''

    status_override = 'NEEDS REVIEW'
    analysis_text = ''

    if not heuristic:
        analysis_text = (
            ' Run with --heuristic for an automatic pixel-based '
            'confidence check instead of a plain manual screenshot review.'
        )
    elif not (screenshot_path and os.path.exists(screenshot_path)):
        analysis_text = (
            ' --heuristic was set but no screenshot was available to '
            'analyse (capture may have failed) — check the log for a '
            'screenshot warning.'
        )
    else:
        log('  Checking display content for a likely blank/error '
            'screen — this can take several seconds depending on '
            'capture resolution...')
        if dashboard is not None and state is not None:
            state['current_status'] = 'Checking display (screen analysis)...'
            dashboard.update(state)
        try:
            result = screenshot_analysis.analyze(screenshot_path)
            if result['error']:
                analysis_text = (
                    f' Pixel analysis failed: {result["error"]}.'
                )
            elif result['flagged']:
                analysis_text = (
                    f' Pixel analysis: {result["grey_pct"]:.0f}% '
                    f'uniform light colour in the sampled region '
                    f'— LIKELY BLANK/ERROR SCREEN.'
                )
            else:
                analysis_text = (
                    f' Pixel analysis: {result["grey_pct"]:.0f}% '
                    f'uniform light colour in the sampled region '
                    f'— likely genuine content.'
                )
                status_override = ''
        except Exception as e:
            analysis_text = f' Pixel analysis error: {e}.'
            # status_override stays NEEDS REVIEW

    suffix = (
        f' — UNVERIFIED: {core} can report OK while showing '
        f'nothing usable on screen, with no error text logged. '
        f'Check the screenshot before trusting this result'
        + (f' ({screenshot_path})' if screenshot_path else '')
        + analysis_text
    )
    log(f'  WARNING: {core} reported OK but this cannot be '
        f'trusted from logs alone — visual confirmation needed.'
        + (f' Screenshot: {screenshot_path}' if screenshot_path else '')
        + analysis_text)
    return status_override, suffix


def verify_dump_quality(
    notes: str,
    screenshot_path: str,
    heuristic: bool,
    dashboard=None,
    state: dict = None,
) -> tuple[str, str]:
    """
    Verification logic for IMPERFECT results triggered by dump-quality
    markers (ROM NEEDS REDUMP, WARNING: the game might not run correctly).

    Unlike verify_unverified_core() — where the core itself might mask
    failures — here the ambiguity is about the dump, not the core.
    The same two runtime outcomes produce the same log text:
        (a) game loads and runs despite the bad dump  → keep IMPERFECT
        (b) game fails to display anything meaningful → escalate to ERROR

    A screenshot heuristic resolves this:
        - Content visible (not mostly flat/light) → '' (keep IMPERFECT)
        - Grey/blank screen                       → 'ERROR'
        - No heuristic / no screenshot / error    → 'NEEDS REVIEW'

    The notes sentinel '[DUMP_QUALITY] ' is stripped here; this function
    is the only consumer of that tag, so it never reaches the CSV raw.

    Returns:
        (status_override, notes_suffix) — status_override is '', 'ERROR',
        or 'NEEDS REVIEW'. '' means keep the caller's IMPERFECT status.
    """
    # Strip the sentinel tag so the underlying marker text is clean
    clean_notes = notes.replace('[DUMP_QUALITY] ', '', 1)

    status_override = 'NEEDS REVIEW'
    analysis_text   = ''

    if not heuristic:
        analysis_text = (
            ' Run with --heuristic for an automatic pixel-based '
            'check — this can confirm whether the game genuinely '
            'loaded (IMPERFECT) or showed nothing useful (ERROR).'
        )
    elif not (screenshot_path and os.path.exists(screenshot_path)):
        analysis_text = (
            ' --heuristic was set but no screenshot was available '
            'to analyse (capture may have failed).'
        )
    else:
        log('  Checking display content — dump-quality marker '
            'detected, confirming whether the game actually loaded...')
        if dashboard is not None and state is not None:
            state['current_status'] = 'Checking display (screen analysis)...'
            dashboard.update(state)
        try:
            result = screenshot_analysis.analyze(screenshot_path)
            if result['error']:
                analysis_text = (
                    f' Pixel analysis failed: {result["error"]}.'
                )
            elif result['flagged']:
                analysis_text = (
                    f' Pixel analysis: {result["grey_pct"]:.0f}% '
                    f'uniform light colour — game did not load despite '
                    f'the dump-quality warning.'
                )
                status_override = 'ERROR'
            else:
                analysis_text = (
                    f' Pixel analysis: {result["grey_pct"]:.0f}% '
                    f'uniform light colour — game loaded; '
                    f'running with known dump-quality issues.'
                )
                status_override = ''
        except Exception as e:
            analysis_text = f' Pixel analysis error: {e}.'

    suffix = (
        f' — DUMP QUALITY WARNING: {clean_notes}. Load status '
        f'unconfirmed from logs alone — a ROM with this warning '
        f'may run fine or may show nothing.'
        + (f' Screenshot: {screenshot_path}' if screenshot_path else '')
        + analysis_text
    )
    log(f'  Dump-quality marker detected: {clean_notes}.'
        + analysis_text)
    return status_override, suffix


def attempt_autofix(
    system: str,
    rom: str,
    romname: str,
    conf_path: str,
    platform,
    dashboard,
    state: dict,
    installed_cores: set[str] = None,
    heuristic: bool = False
) -> tuple[str, str, str]:
    """
    Attempt to fix a failing ROM by trying known core/emulator combinations.

    Iterates through the fix combinations for the ROM's system, skipping
    any whose core is not installed. For each valid combination, updates
    batocera.conf and calls platform.run_test() to check whether the ROM loads.

    If a working combination is found, the batocera.conf entry is left in
    place and FIXED is returned. If all combinations fail, all per-game
    entries are removed from batocera.conf and GENUINE ERROR is returned.
    If no combinations are available (system unsupported or all cores
    missing), returns NO COMBINATIONS immediately without touching conf.

    Args:
        system:          System folder name e.g. 'mame'.
        rom:             Full path to the ROM file.
        romname:         ROM filename e.g. 'finalizr.7z'.
        conf_path:       Full path to batocera.conf.
        platform:        Platform instance providing run_test().
        dashboard:       Dashboard instance for live updates.
        state:           Shared state dict for dashboard updates.
        installed_cores: Set of installed core names for filtering.
                         Pass None to skip core availability check.
        heuristic:       When True, also run pixel analysis on the
                         forced verification screenshot for any
                         UNVERIFIED_CORES result (see
                         screenshot_analysis.analyze()), adding the
                         finding to the notes. When False (default),
                         the screenshot is still captured and flagged
                         UNVERIFIED, just without the extra analysis
                         pass — real time cost per occurrence, a pure-
                         Python PNG decode, several seconds at 4K
                         capture resolution. Only relevant the rare
                         times a core in UNVERIFIED_CORES actually
                         wins a combination, not on every ROM.

    Returns:
        Tuple of (status, notes, fix_description) where:
            status:          'FIXED', 'GENUINE ERROR', or 'NO COMBINATIONS'
            notes:           Brief description of outcome
            fix_description: The winning combination string, or empty string
    """
    combinations = get_combinations(system, installed_cores)

    if not combinations:
        if system not in FIX_COMBINATIONS:
            log(f"  No fix combinations defined for system [{system}]")
            return 'NO COMBINATIONS', f"No autofix support for [{system}]", ""
        else:
            log(f"  No installed cores available for autofix [{system}]")
            return (
                'NO COMBINATIONS',
                f"Required cores not installed for [{system}]",
                ""
            )

    log(f"  Attempting autofix — trying "
        f"{len(combinations)} combination(s)...")

    for i, (test_system, conf_prefix, core, emulator) in \
            enumerate(combinations, 1):

        fix_desc = (
            f"{conf_prefix}[\"{romname}\"].core={core} / "
            f"emulator={emulator}"
        )
        log(f"  [{i}/{len(combinations)}] Trying: {fix_desc}")

        if not configeditor.write_game_entries(
            conf_path, conf_prefix, romname, core, emulator
        ):
            log(f"    Could not write to batocera.conf, skipping.")
            continue

        state['current_status'] = (
            f"Autofix {i}/{len(combinations)}: {core} / {test_system}"
        )
        dashboard.update(state)

        # Some cores can report a clean OK using exactly the same
        # criteria every other candidate is judged by (launches,
        # displays for the full window, clean kill, no error text)
        # while genuinely showing nothing usable on screen — FBNeo's
        # known behaviour on an unrecognised/bad dump is to silently
        # grey-screen rather than log anything. Confirmed in practice,
        # reproduced live: autofix accepted FBNeo as a fix for a ROM
        # that was visibly grey-screened with errors at the moment of
        # capture. A plain log-based OK from one of these cores cannot
        # be trusted the same way a normal candidate's OK can — force
        # a screenshot for this specific attempt regardless of the
        # audit's own --screenshot setting, so there's something to
        # actually look at before trusting the result.
        needs_verification = core in UNVERIFIED_CORES
        verify_screenshot = None
        if needs_verification:
            shot_dir = os.path.join(platform.error_log_base, system, romname)
            try:
                os.makedirs(shot_dir, exist_ok=True)
                verify_screenshot = os.path.join(
                    shot_dir, f'{system}_{romname}_review.png'
                )
            except Exception:
                verify_screenshot = None

        status, notes, elapsed = platform.run_test(
            test_system, rom, dashboard, state,
            screenshot_path=verify_screenshot
        )

        log(f"    Result: {status} ({elapsed:.1f}s) {notes}")

        if status in ('OK', 'IMPERFECT'):
            is_imperfect = (status == 'IMPERFECT')
            imperfect_suffix = ''
            verify_suffix    = ''   # set here; only reassigned in the OK branch

            # If the IMPERFECT result carries a [DUMP_QUALITY] sentinel,
            # the load status is ambiguous — game may or may not be showing
            # real content. Run verify_dump_quality() to resolve it the same
            # way post_process_result() would on the normal (non-autofix)
            # path. Strip the sentinel first so it never reaches the CSV.
            if is_imperfect and notes.startswith('[DUMP_QUALITY]'):
                dq_shot = verify_screenshot
                if not dq_shot:
                    shot_dir = os.path.join(
                        platform.error_log_base, system, romname
                    )
                    try:
                        os.makedirs(shot_dir, exist_ok=True)
                        dq_shot = os.path.join(
                            shot_dir, f'{system}_{romname}_review.png'
                        )
                    except Exception:
                        dq_shot = None
                dq_status, dq_suffix = verify_dump_quality(
                    notes, dq_shot, heuristic, dashboard, state
                )
                clean_notes = notes.replace('[DUMP_QUALITY] ', '', 1)
                if dq_status == 'ERROR':
                    # Pixel check confirmed blank/grey — not a real fix,
                    # try the next combination.
                    log(f'    Dump-quality check: blank screen — '
                        f'not a genuine fix, continuing...')
                    continue
                elif dq_status == 'NEEDS REVIEW':
                    # No heuristic or screenshot — can't confirm either
                    # way. Accept as FIXED with NEEDS REVIEW caveat so
                    # the user can verify manually rather than discarding
                    # a potentially working combination.
                    final_status  = 'NEEDS REVIEW'
                    imperfect_suffix = (
                        f" (note: {clean_notes}){dq_suffix}"
                    )
                else:
                    # Pixel check confirmed content — genuinely running.
                    final_status  = 'FIXED'
                    imperfect_suffix = (
                        f" (note: {clean_notes}){dq_suffix}"
                    )
                notes = clean_notes

            elif is_imperfect:
                # Confirmed-running IMPERFECT (accuracy warning, not dump
                # quality) — accept as FIXED, preserve the caveat in notes.
                final_status     = 'FIXED'
                imperfect_suffix = f" (note: {notes})" if notes else " (IMPERFECT)"

            else:
                # OK — run UNVERIFIED_CORES check as before.
                verify_status, verify_suffix = verify_unverified_core(
                    core, verify_screenshot, heuristic, dashboard, state
                )
                final_status     = verify_status or 'FIXED'
                imperfect_suffix = verify_suffix

            # Check if the winning core matches the global system-level
            # default in batocera.conf (e.g. mame.core=fbneo).
            # If so, the per-game entry is redundant — remove it and
            # let the global handle it. Only keep per-game entries for
            # ROMs that need a DIFFERENT core from the system default.
            # This prevents config file bloat on cabinets where the
            # global default covers the majority of ROMs.
            global_default = _get_global_default_core(
                conf_path, conf_prefix
            )
            if global_default and global_default == core:
                removed = configeditor.remove_game_entries(
                    conf_path, romname
                )
                log(f"  Fixed with: {core} (matches system default "
                    f"— no per-game entry needed)")
                return (
                    final_status,
                    f"Fixed: {conf_prefix} default core ({core})"
                    f"{verify_suffix}{imperfect_suffix}",
                    fix_desc
                )
            log(f"  Fixed with: {fix_desc}")
            return (
                final_status,
                f"Fixed: {fix_desc}{verify_suffix}{imperfect_suffix}",
                fix_desc
            )

    # All combinations exhausted — clean up batocera.conf
    log(f"  All combinations failed. Removing entries from batocera.conf.")
    removed = configeditor.remove_game_entries(conf_path, romname)
    if removed:
        log(f"  Removed {removed} "
            f"entr{'y' if removed == 1 else 'ies'} from batocera.conf.")

    return 'GENUINE ERROR', "All fix combinations failed", ""
