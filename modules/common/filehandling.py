"""
File handling utilities for the ROM Audit Tool.

Covers CSV persistence, log file reading and clearing, ROM discovery
across system folders, error log archiving, and faulty ROM cleanup.
All file paths are passed in as arguments rather than hardcoded,
keeping this module platform-agnostic.
"""

from __future__ import annotations  # Python 3.7 compatibility

import os
import re
import glob
import csv
import shutil

from modules.common.logging import log


# ---------------------------------------------------------------------------
# ROM discovery configuration
# ---------------------------------------------------------------------------

# ROM file extensions to scan across all systems.
# Covers the majority of common retro formats. Only the top level of
# each system folder is scanned to avoid media subdirectories.
ROM_EXTENSIONS = [
    # ---------------------------------------------------------------
    # Universal archive and disc image formats
    # ---------------------------------------------------------------
    '*.7z', '*.zip', '*.chd', '*.iso', '*.cue',
    '*.toc', '*.nrg', '*.gdi', '*.cdr',            # CD/disc images

    # ---------------------------------------------------------------
    # Cartridge and ROM dump formats
    # ---------------------------------------------------------------
    '*.rom', '*.n64', '*.z64', '*.v64',            # Generic / N64
    '*.sfc', '*.smc',                              # SNES
    '*.smd', '*.md',                               # Mega Drive
    '*.gb', '*.gba', '*.gbc',                      # Game Boy family
    '*.nes', '*.fds', '*.nds',                     # Nintendo handhelds
    '*.pce', '*.ngp', '*.ngc',                     # PC Engine / Neo Geo Pocket
    '*.ws', '*.wsc', '*.vb',                       # WonderSwan / Virtual Boy
    '*.a26', '*.a52', '*.a78',                     # Atari cartridges
    '*.col', '*.int', '*.vec',                     # ColecoVision / Intellivision / Vectrex
    '*.sg', '*.sgg', '*.gg',                       # Sega SG-1000 / Game Gear
    '*.lnx',                                       # Atari Lynx
    '*.uze',                                       # Uzebox (AVR-based homebrew console)
    '*.pgm', '*.tvc',                              # VC4000
    '*.chf',                                       # Channel F
    '*.gam',                                       # Vectrex .gam
    '*.car',                                       # Atari cartridge image
    '*.tic',                                       # TIC-80 fantasy computer
    '*.wasm',                                      # WASM-4 fantasy console
    '*.solarus',                                   # Solarus game engine
    '*.hex',                                       # Arduboy / Intel HEX
    '*.p8',                                        # PICO-8 cartridge
    '*.nx',                                        # LowRes NX fantasy computer
    '*.lutro',                                     # Lutro game engine
    '*.ort',                                       # Oric Atmos format
    '*.gcm', '*.gcz', '*.wbfs', '*.dol', '*.ciso',# GameCube / Wii / Triforce

    # ---------------------------------------------------------------
    # Tape formats
    # ---------------------------------------------------------------
    '*.tap', '*.tzx',                              # ZX Spectrum / generic tape
    '*.z80', '*.rzx',                              # ZX Spectrum snapshots / recordings
    '*.scl', '*.trd',                              # ZX Spectrum TR-DOS disk
    '*.cas', '*.cdm',                              # Cassette / Atari tape
    '*.csw', '*.uef',                              # Acorn (CSW/UEF tape)
    '*.k7', '*.m7', '*.m5',                        # Thomson tape
    '*.t77',                                       # FM7 tape
    '*.vz',                                        # Laser 310 tape
    '*.p',                                         # ZX81 program

    # ---------------------------------------------------------------
    # Floppy disk image formats
    # ---------------------------------------------------------------
    '*.dsk', '*.adf', '*.ipf',                     # Generic / Amiga
    '*.uae', '*.hdf', '*.dms', '*.dmz', '*.adz',  # Amiga extended
    '*.d64', '*.d81',                              # Commodore 64 / 128
    '*.st', '*.msa', '*.stx',                      # Atari ST
    '*.sad', '*.mgt', '*.sdf', '*.td0', '*.sbt',  # Sam Coupe
    '*.cpm',                                       # Sam Coupe CP/M disk
    '*.d88', '*.d77', '*.xdf', '*.2hd', '*.88d',  # Japanese systems
    '*.hdm', '*.dup',                              # X68000 / PC98
    '*.mfi', '*.dfi', '*.hfe', '*.mfm', '*.imd',  # Universal floppy images
    '*.1dd', '*.cqm', '*.cqi',                    # Floppy images (cont.)
    '*.ima', '*.ufi', '*.360',                     # DOS / Archimedes disk
    '*.apd', '*.jfd', '*.ads', '*.adm', '*.adl',  # Archimedes disk
    '*.ssd', '*.bbc', '*.dsd', '*.fsd',           # BBC Micro disk
    '*.86f',                                       # Amstrad PCW disk
    '*.mx1', '*.mx2',                              # MSX ROM / disk
    '*.fd', '*.sap',                               # Thomson disk
    '*.woz', '*.nib', '*.do', '*.po',             # Apple II disk
    '*.dc42', '*.2mg',                             # Macintosh disk
    '*.dmk', '*.ccc',                              # Dragon / TRS80
    '*.2d', '*.dx1',                               # Sharp X1
    '*.d98', '*.98d', '*.fdi', '*.fdd', '*.tfd',  # PC98 / PC80
    '*.n80',                                       # PC80

    # ---------------------------------------------------------------
    # Hard disk / storage image formats
    # ---------------------------------------------------------------
    '*.hdv', '*.hdi', '*.hdn', '*.nhd', '*.thd',  # PC98 / Amiga HDD

    # ---------------------------------------------------------------
    # Other binary / program formats
    # ---------------------------------------------------------------
    '*.bin', '*.img',                              # Generic binary images
    '*.xex', '*.atr', '*.xfd', '*.atx',           # Atari 8-bit
    '*.prg', '*.crt', '*.t64',                    # Commodore 64
    '*.a0', '*.b0',                               # VIC-20 / PET ROM banks
    '*.pbp', '*.cso',                              # PSP formats
    '*.bas', '*.com',                              # Commander X16 / Enterprise
    '*.rti', '*.edd',                              # Apple II extended
    '*.bkd',                                       # Electronika BK
    '*.dtf', '*.trn',                             # Enterprise
    '*.mzf', '*.mzt', '*.m12',                    # Sharp MZ series
    '*.voc', '*.cdt', '*.sna',                    # Amstrad CPC
    '*.u88',                                       # PC88
    '*.bit',                                       # Sega SC-3000
    '*.ddp',                                       # Coleco Adam
    '*.rim', '*.drm',                             # PDP1
    '*.cmd',                                       # PC98 / X68000 command
    '*.lst', '*.dat',                             # Naomi / Sega System SP
    '*.game',                                      # Lindbergh / Pong
    '*.dmy',                                       # Dice

    # ---------------------------------------------------------------
    # Playlist / multi-disc
    # ---------------------------------------------------------------
    '*.m3u',                                       # Multi-disc playlists

    # ---------------------------------------------------------------
    # System-specific archives
    # ---------------------------------------------------------------
    '*.lha',                                       # Amiga LHA archive
    '*.mgw',                                       # Game and Watch
]

# System folder names to skip entirely.
# These are either non-game systems, launcher utilities, or folders
# that store data in a format incompatible with simple file scanning.
SKIP_SYSTEMS = {
    'ports',        # Gamelist.xml-based discovery — handled separately by platform
    'kodi',         # Media centre, not a game system
    'moonlight',    # Game streaming client
    'prboom',       # Doom engine - requires specific WAD handling
    'scummvm',      # Games stored as folders not files
    'odcommander',  # File manager utility
    'devilutionx',  # Diablo engine port
    'screenshots',  # Media folder
    'tmp',          # Temporary files
    'daphne',       # Laser disc — directory-based, standalone emulator
    'gc',           # GameCube firmware/BIOS files — not games,
                    # belongs in /userdata/bios/. Actual GameCube
                    # games go in the 'gamecube' folder.
}

# ROM filenames to skip regardless of which system folder they appear in.
# These are known BIOS or firmware files that users commonly place in
# ROM directories — testing them as games always fails and clutters results.
SKIP_ROM_FILES = {
    '5200.rom',       # Atari 5200 BIOS
    'coleco.rom',     # ColecoVision BIOS
    'lynxboot.img',   # Atari Lynx BIOS
    'neogeo.zip',     # Neo Geo BIOS — present in MAME sets but not a game
    'pgm.zip',        # PGM (PolyGame Master) BIOS
    'skns.zip',       # Super Kaneko Nova System BIOS
    'nmk004.zip',     # NMK004 BIOS
    # MSX BIOS files — live in the msx1 ROMs folder but are not game ROMs
    'MSX.ROM', 'MSX2.ROM', 'MSX2EXT.ROM', 'MSX2P.ROM', 'MSX2PEXT.ROM',
    'DISK.ROM', 'FMPAC.ROM', 'FMPAC16.ROM', 'KANJI.ROM',
    'MSXDOS2.ROM', 'PAINTER.ROM', 'RS232.ROM',
    # Lowercase variants (some installs use lowercase)
    'msx.rom', 'msx2.rom', 'msx2ext.rom', 'msx2p.rom', 'msx2pext.rom',
    'disk.rom', 'fmpac.rom', 'fmpac16.rom', 'kanji.rom',
    'msxdos2.rom', 'painter.rom', 'rs232.rom',
}

# Pre-computed uppercase set for case-insensitive matching
_SKIP_ROM_UPPER = {f.upper() for f in SKIP_ROM_FILES}

# Documentation file stems that share extensions with ROM formats.
# .md = Mega Drive ROM AND Markdown; .txt appears in some ROM sets.
# Matched against the filename stem (name without extension),
# case-insensitively, so README.md, README.txt, readme etc. all match.
SKIP_DOC_STEMS_UPPER = {
    'README', 'LICENCE', 'LICENSE', 'CHANGELOG', 'CHANGES',
    'COPYING', 'CONTRIBUTING', 'AUTHORS', 'CREDITS',
    'INSTALL', 'TODO', 'NOTES', 'NOTICE', 'NOTICES',
    'CLAUDE',        # AI context file — not a ROM
    'HISTORY', 'NEWS', 'BUGS', 'HACKING', 'THANKS',
}


# ---------------------------------------------------------------------------
# Log file utilities
# ---------------------------------------------------------------------------

def read_log(path: str) -> str:
    """
    Read the contents of a log file safely.

    Args:
        path: Full path to the log file.

    Returns:
        File contents as a string, or empty string if unreadable.
    """
    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception:
        return ""


def clear_log(path: str) -> None:
    """
    Clear the contents of a log file by truncating it.

    Called before each ROM launch to ensure log content from a
    previous test does not bleed into the current one.

    Args:
        path: Full path to the log file to clear.
    """
    try:
        open(path, 'w').close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CSV persistence
# ---------------------------------------------------------------------------

def compute_checksum(rom_path: str, algorithm: str, progress_callback=None) -> str:
    """
    Compute a checksum of a ROM file.

    Reads the file in 64KB chunks to handle large CD images without
    loading them entirely into memory. Returns an empty string on error.

    Args:
        rom_path:           Full path to the ROM file.
        algorithm:          Hash algorithm — 'md5' or 'sha1'.
        progress_callback:  Optional callable(bytes_done, bytes_total, partial_hex)
                            invoked every 256 chunks (~16MB). partial_hex is the
                            in-progress digest at that point — not the final value,
                            but visually confirms hashing is advancing. Used by
                            callers to update a live dashboard during large file
                            reads without importing dashboard machinery here.

    Returns:
        Checksum string in the format 'algorithm:hexdigest', e.g.
        'md5:d41d8cd98f00b204e9800998ecf8427e', or '' on failure.
    """
    import hashlib
    CHUNK_SIZE     = 65536   # 64 KB
    CALLBACK_EVERY = 256     # call back every ~16 MB
    try:
        total = os.path.getsize(rom_path) if progress_callback else 0
        h = hashlib.new(algorithm)
        done        = 0
        chunk_count = 0
        with open(rom_path, 'rb') as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
                done += len(chunk)
                chunk_count += 1
                if progress_callback and chunk_count % CALLBACK_EVERY == 0:
                    progress_callback(done, total, h.hexdigest())
        return f"{algorithm}:{h.hexdigest()}"
    except Exception:
        return ''


def load_results(results_csv: str) -> dict[str, dict]:
    """
    Load previously recorded audit results from the CSV file.

    Results are keyed as 'system:romname' to handle cases where the
    same filename exists in multiple system folders.

    Args:
        results_csv: Full path to the CSV results file.

    Returns:
        Dictionary mapping 'system:romname' keys to result row dicts.
        Returns an empty dict if the CSV does not yet exist.
    """
    tested = {}
    if os.path.exists(results_csv):
        with open(results_csv, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = f"{row['system']}:{row['rom']}"
                tested[key] = row
    return tested


def _safe_path_component(name: str) -> str:
    """
    Sanitize a system name or ROM filename for safe use as a filesystem
    path component.

    Replaces any character that isn't a word character, dot, hyphen, or
    space with an underscore — matching the existing flat-mode screenshot
    naming convention, now applied consistently to *all* nested path
    construction so a crafted filename like '../../etc/passwd' can't
    traverse outside the intended audit_logs/ tree.

    This is the ONLY place this sanitization should be defined — both
    base.py's screenshot_path() and filehandling.py's path construction
    functions delegate here rather than each reimplementing the same
    pattern. If the character whitelist ever changes, change it here.

    Args:
        name: Raw system name or ROM filename from the filesystem.

    Returns:
        Sanitized string safe for use as a single path component.
    """
    return re.sub(r'[^\w.\- ]', '_', name)


def _csv_safe(value: str) -> str:
    """
    Escape CSV cell values that would be interpreted as spreadsheet
    formulas by Excel, LibreOffice Calc, and Google Sheets.

    A cell starting with =, @, +, or - is treated as a formula by most
    spreadsheet applications — a ROM named '=CMD|...' or '@SUM(...)' could
    trigger arbitrary formula execution when the CSV is opened. Prefixing
    with a tab character suppresses this without visibly corrupting the
    value (the tab is typically invisible in cells, and doesn't affect
    CSV round-tripping when the file is re-read by this tool). The field
    is only modified when the first character is one of the trigger chars
    — the vast majority of ROM names and notes are never touched.

    Args:
        value: Cell value as a string.

    Returns:
        Safe string, with a leading tab prepended if necessary.
    """
    if value and value[0] in ('=', '+', '-', '@', '\t', '\r'):
        return '\t' + value
    return value


def merge_rom_lists(*rom_lists: list) -> list:
    """
    Merge multiple ROM lists into a single sorted, deduplicated list.

    Each list is a sequence of (system, rom_path) tuples. The merged
    result is sorted system-contiguous alphabetically by system then
    filename — the ordering the dashboard counters and per-system
    progress tracking depend on.

    Deduplication is by (system, basename) — first occurrence wins,
    so the primary path takes priority over additional paths.

    Args:
        *rom_lists: Any number of (system, path) tuple lists.

    Returns:
        Sorted, deduplicated list of (system, path) tuples.
    """
    seen = set()
    merged = []
    for rom_list in rom_lists:
        for system, path in rom_list:
            key = (system.lower(), os.path.basename(path).lower())
            if key not in seen:
                seen.add(key)
                merged.append((system, path))
    merged.sort(key=lambda t: (t[0].lower(), os.path.basename(t[1]).lower()))
    return merged


def snapshot_results(results_csv: str) -> str | None:
    """
    Create a timestamped snapshot of the CSV before a run that will
    modify it. Returns the snapshot path, or None if the CSV doesn't
    exist yet (nothing to snapshot).

    Called once at the start of a meaningful run (new audit, recheck,
    autofix) — not on --test or --cleanup which don't bulk-modify rows.
    The snapshot preserves the state before the run so a failed or
    interrupted run can be rolled back manually.

    The per-write .bak created by save_results() is kept too — it
    covers the last-write safety net — but is not a useful rollback
    point for a run that modifies thousands of rows.

    Returns:
        Full path to the snapshot file, or None if no snapshot was made.
    """
    if not os.path.exists(results_csv):
        return None
    from datetime import datetime
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    base, ext = os.path.splitext(results_csv)
    snapshot_path = f'{base}_{ts}{ext}'
    try:
        shutil.copy2(results_csv, snapshot_path)
        return snapshot_path
    except Exception as e:
        log(f"Warning: could not create CSV snapshot: {e}")
        return None


def save_results(results_csv: str, results_dict: dict[str, dict]) -> None:
    """
    Write all audit results back to the CSV file.

    Overwrites the existing file completely to support in-place updates
    when rechecking previously failing ROMs.

    Args:
        results_csv:  Full path to the CSV results file.
        results_dict: Dictionary mapping 'system:romname' keys to
                      result row dicts.
    """
    tmp_path = results_csv + '.tmp'
    with open(tmp_path, 'w', newline='') as f:
        fieldnames = [
            'system', 'rom', 'status',
            'elapsed_seconds', 'tested_at', 'checksum', 'notes'
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in results_dict.values():
            # Ensure checksum field exists for rows loaded from older CSVs
            if 'checksum' not in row:
                row['checksum'] = ''
            safe_row = {
                k: _csv_safe(str(v)) if k in ('system', 'rom', 'notes')
                else v
                for k, v in row.items()
            }
            writer.writerow(safe_row)
    # Atomic replace — prevents truncation on crash
    if os.path.exists(results_csv):
        shutil.copy2(results_csv, results_csv + '.bak')
    os.replace(tmp_path, results_csv)


# ---------------------------------------------------------------------------
# Error log archiving
# ---------------------------------------------------------------------------

def save_error_logs(
    error_log_base: str,
    system: str,
    romname: str,
    stdout_log: str,
    stderr_log: str
) -> None:
    """
    Archive the launch logs for a failed ROM into a structured directory.

    Creates a directory structure of error_log_base/system/romname/
    and copies both the stdout and stderr log files into it. This gives
    the operator a persistent record of each failure for later diagnosis
    without needing to re-run the audit.

    Directory structure:
        audit_logs/
            mame/
                pacman.7z/
                    es_launch_stdout.log
                    es_launch_stderr.log

    Args:
        error_log_base: Root directory for error log archives.
        system:         System name (e.g. 'mame').
        romname:        ROM filename (e.g. 'pacman.7z').
        stdout_log:     Path to the current stdout log file.
        stderr_log:     Path to the current stderr log file.
    """
    dest = os.path.join(error_log_base, _safe_path_component(system), _safe_path_component(romname))
    os.makedirs(dest, exist_ok=True)
    try:
        shutil.copy(
            stdout_log,
            os.path.join(dest, "es_launch_stdout.log")
        )
        shutil.copy(
            stderr_log,
            os.path.join(dest, "es_launch_stderr.log")
        )
    except Exception as e:
        log(f"  Warning: could not save error logs: {e}")


def clear_error_logs(
    error_log_base: str,
    system: str,
    romname: str
) -> None:
    """
    Remove the archived logs for a ROM that has passed — UNLESS a
    screenshot or a diagnostic marker file is present anywhere in the
    directory, in which case the whole directory is left untouched,
    logs included.

    Called automatically when a previously failing ROM passes on
    recheck, normally removing its log directory as confirmation the
    issue is resolved.

    Checks "is there any .png file, or PARSE_FAILED.txt, in here at
    all" once, up front, rather than deciding per-file whether to keep
    it. This was originally matched against one specific hardcoded
    filename ("screenshot.png"), which silently deleted autofix's own
    forced verification screenshot the moment a naming convention it
    didn't recognise showed up — confirmed in practice, the file
    existed, then the whole directory was gone moments later because
    removing everything else left it empty. Broadened once already to
    match by extension instead of exact name, but that still meant
    deciding per file whether to delete it. Checking once at the top
    instead — any .png present means skip the entire folder, not just
    spare the image — means this never needs touching again for a new
    naming convention, current (autofix_verify.png, {system}_{romname}_
    review.png for NEEDS REVIEW results) or future. It's also a
    better outcome on its own merits: a screenshot worth keeping
    usually means a human may want to review the captured logs
    alongside it too, not just the image in isolation.

    PARSE_FAILED.txt is the same idea for a different case: when
    on_successful_test() can't find which core actually ran from the
    log (an anomaly worth a second look even though the overall result
    was OK), it preserves the logs and writes this marker so they
    don't immediately get wiped by this same function moments later in
    the normal OK-result flow. Without it, "why isn't there a log"
    would always be the answer here — the OK status alone, regardless
    of the anomaly underneath it, was always what triggered cleanup.

    Args:
        error_log_base: Root directory for error log archives.
        system:         System name.
        romname:        ROM filename.
    """
    dest = os.path.join(error_log_base, _safe_path_component(system), _safe_path_component(romname))
    if not os.path.exists(dest):
        return
    try:
        files = os.listdir(dest)
        if any(f.lower().endswith('.png') for f in files):
            return   # Screenshot present — leave the whole folder alone
        if 'PARSE_FAILED.txt' in files:
            return   # Diagnostic marker present — same reasoning
        for filename in files:
            filepath = os.path.join(dest, filename)
            if os.path.isfile(filepath):
                os.remove(filepath)
        # Remove directory only if now empty
        if not os.listdir(dest):
            shutil.rmtree(dest)
    except Exception as e:
        log(f"  Warning: could not clear error logs: {e}")


# ---------------------------------------------------------------------------
# ROM discovery
# ---------------------------------------------------------------------------

def discover_roms(
    roms_base: str,
    system_filter: list = None,
    exclude: list = None,
    subdir_markers: list = None,
) -> list:
    """
    Discover all ROM files across system folders under the roms base path.

    Scans only the top level of each system folder to avoid picking up
    media subdirectories (box art, videos, screenshots, manuals etc).
    Systems listed in SKIP_SYSTEMS are ignored entirely.

    When subdir_markers is provided, subdirectories containing any of
    the marker files are also scanned for ROMs under the parent system
    name. This covers cases like Recalbox's 'Commodore Plus4' subfolder
    within c64 — same system, different core controlled by .core.cfg /
    .recalbox.conf sidecar files picked up at launch time.

    Args:
        roms_base:      Base path where all system ROM folders live.
        system_filter:  If provided, only scan these system folders.
        exclude:        List of system names to skip in addition to
                        SKIP_SYSTEMS.
        subdir_markers: Filenames that mark a subdir as a system-override
                        folder to scan (e.g. ['.core.cfg', '.recalbox.conf']).

    Returns:
        Sorted list of (system_name, full_rom_path) tuples.
    """
    all_roms = []
    skip     = set(SKIP_SYSTEMS) | set(exclude or [])

    if not os.path.exists(roms_base):
        log(f"ERROR: Roms base path not found: {roms_base}")
        return []

    systems = sorted([
        d for d in os.listdir(roms_base)
        if os.path.isdir(os.path.join(roms_base, d))
        and not d.startswith('.')
        and d not in skip
    ])

    if system_filter:
        systems = [s for s in systems if s in system_filter]
        if not systems:
            log(f"WARNING: None of the specified systems were found: "
                f"{', '.join(system_filter)}")

    for system in systems:
        system_path = os.path.join(roms_base, system)
        system_roms = []

        # Build set of valid extensions in lowercase for case-insensitive
        # matching. Linux glob is case-sensitive so *.bin misses *.BIN etc.
        valid_exts = {
            ext.lstrip('*').lower()
            for ext in ROM_EXTENSIONS
        }

        # Pre-scan for .cue files so we can skip companion raw image files.
        # CD-based ROMs consist of a .cue sheet + one or more .bin/.img/.iso
        # data tracks. The .cue is the correct entry point for emulators;
        # testing the raw .bin directly produces "no core" errors because
        # gamelists and sidecars reference the .cue, not the .bin.
        #
        # We collect two things into cue_stems:
        #   1. The .cue filename stem itself (Track 1 usually shares this name)
        #   2. Every FILE "..." entry inside the .cue (catches Track 2, Track 3
        #      etc. which have different stems, e.g. "(Track 2).bin")
        CD_RAW_EXTENSIONS = {'.bin', '.img', '.iso', '.sub', '.raw', '.wav'}
        cue_stems: set[str] = set()
        try:
            for f in os.listdir(system_path):
                if not f.lower().endswith('.cue'):
                    continue
                cue_path = os.path.join(system_path, f)
                if not os.path.isfile(cue_path):
                    continue
                # Stem of the .cue file itself
                cue_stems.add(os.path.splitext(f)[0].lower())
                # Parse FILE "..." lines to catch all referenced tracks
                try:
                    with open(cue_path, 'r', errors='replace') as fh:
                        for line in fh:
                            line = line.strip()
                            if line.upper().startswith('FILE '):
                                parts = line.split('"')
                                if len(parts) >= 3:
                                    ref = parts[1]
                                    cue_stems.add(
                                        os.path.splitext(ref)[0].lower()
                                    )
                except Exception:
                    pass
        except PermissionError:
            pass

        # Pre-scan for .uae files so we can skip companion raw disk images.
        # Amiga ROMs distributed as a bare .adf will fail to launch with
        # the platform default config (wrong fastmem, wrong CPU speed,
        # wrong chipset for that specific game) — the .uae sidecar carries
        # the per-game hardware settings the game actually needs and is
        # what EmulationStation launches. Testing the raw .adf directly
        # produces a misleading crash (e.g. SIGABRT) that has nothing to
        # do with the ROM being bad. Same principle as the .cue handling
        # above, just for Amiga instead of CD-based systems.
        #
        # We collect two things into uae_stems, mirroring cue_stems:
        #   1. The .uae filename stem itself (common case: "Game.uae" +
        #      "Game.adf" share a stem)
        #   2. Every floppyN=/harddriveN=/cdimage0= path referenced inside
        #      the .uae (catches multi-disk games where the .uae's own
        #      name doesn't match every referenced .adf, e.g.
        #      "Game.uae" -> "Game (Disk 1 of 3).adf", "Game (Disk 2).adf")
        AMIGA_RAW_EXTENSIONS = {'.adf', '.hdf', '.dms', '.dmz', '.adz'}
        uae_stems: set[str] = set()
        try:
            for f in os.listdir(system_path):
                if not f.lower().endswith('.uae'):
                    continue
                uae_path = os.path.join(system_path, f)
                if not os.path.isfile(uae_path):
                    continue
                # Stem of the .uae file itself
                uae_stems.add(os.path.splitext(f)[0].lower())
                # Parse key=value lines for any reference to a raw disk
                # image, regardless of which key it's attached to —
                # floppy0/1/2/3, harddrive, cdimage0 etc. all use the same
                # "key=path" syntax with no quoting
                try:
                    with open(uae_path, 'r', errors='replace') as fh:
                        for line in fh:
                            line = line.strip()
                            if '=' not in line:
                                continue
                            _, _, value = line.partition('=')
                            value = value.strip()
                            value_ext = os.path.splitext(value)[1].lower()
                            if value_ext in AMIGA_RAW_EXTENSIONS:
                                ref_stem = os.path.splitext(
                                    os.path.basename(value)
                                )[0].lower()
                                uae_stems.add(ref_stem)
                except Exception:
                    pass
        except PermissionError:
            pass

        # Top-level ROMs — case-insensitive extension check
        zero_length = []
        try:
            for filename in os.listdir(system_path):
                full_path = os.path.join(system_path, filename)
                if not os.path.isfile(full_path):
                    continue
                stem_lower, ext = (
                    os.path.splitext(filename)[0].lower(),
                    os.path.splitext(filename)[1].lower()
                )
                if ext not in valid_exts:
                    continue
                if filename.upper() in _SKIP_ROM_UPPER:
                    continue
                # Skip documentation files that share extensions with ROM
                # formats (.md = Mega Drive, .txt appears in some sets).
                # Match on stem so README.md, README.txt, readme all skip.
                if stem_lower.upper() in SKIP_DOC_STEMS_UPPER:
                    continue
                # Skip macOS resource fork / metadata files (._filename)
                if filename.startswith('._'):
                    continue
                # Skip raw CD image tracks that have a companion .cue sheet.
                # The .cue is the correct emulator entry point; testing the
                # raw .bin/.img/.iso produces "no core" errors and duplicates.
                if ext in CD_RAW_EXTENSIONS and stem_lower in cue_stems:
                    continue
                # Skip raw Amiga disk images that have a companion .uae
                # config. The .uae is the correct emulator entry point —
                # see the pre-scan comment above for why testing the raw
                # .adf/.hdf directly produces misleading crashes.
                if ext in AMIGA_RAW_EXTENSIONS and stem_lower in uae_stems:
                    continue
                if os.path.getsize(full_path) == 0:
                    zero_length.append(filename)
                    continue
                system_roms.append(full_path)
        except PermissionError:
            pass

        if zero_length:
            log(f"  WARNING: {len(zero_length)} zero-length file(s) "
                f"skipped in [{system}] — incomplete download or bad "
                f"transfer:")
            for zf in sorted(zero_length):
                log(f"    {zf}")

        # CHD files in one-level subdirectories.
        # MAME/arcade hard drive dumps: system/gamename/gamename.chd
        #
        # Exception: flycast-based systems (naomi, naomi2, atomiswave, dc)
        # store games as a directory containing a CHD, but emulatorlauncher
        # expects DIRECTORY_NAME.7z — not the raw CHD path. ES treats
        # directories as fake .7z archives; we do the same so flycast
        # receives proper BIOS context. Passing the raw CHD causes
        # 'Boot file not found' because flycast loses its bios search path.
        FLYCAST_DIR_SYSTEMS = {'naomi', 'naomi2', 'atomiswave', 'dc'}
        # Real archive extensions that take priority over a same-named directory
        ARCHIVE_EXTS = {'.7z', '.zip', '.chd'}

        try:
            for subdir in os.listdir(system_path):
                subdir_path = os.path.join(system_path, subdir)
                if not os.path.isdir(subdir_path):
                    continue
                if system.lower() in FLYCAST_DIR_SYSTEMS:
                    # Return directory as fake .7z — emulatorlauncher strips
                    # the extension, finds the directory, and passes the CHD
                    # to flycast with the correct bios path.
                    #
                    # Skip if a real archive with the same stem already exists
                    # (e.g. azumanga/ + azumanga.zip → use the .zip, not .7z).
                    real_archive_exists = any(
                        os.path.isfile(os.path.join(system_path, subdir + ext))
                        for ext in ARCHIVE_EXTS
                    )
                    if real_archive_exists:
                        continue
                    if any(f.lower().endswith('.chd')
                           for f in os.listdir(subdir_path)):
                        fake_path = os.path.join(system_path, subdir + '.7z')
                        system_roms.append(fake_path)
                else:
                    for filename in os.listdir(subdir_path):
                        if filename.lower().endswith('.chd'):
                            full_path = os.path.join(subdir_path, filename)
                            if os.path.isfile(full_path) and \
                                    os.path.getsize(full_path) > 0:
                                system_roms.append(full_path)
        except PermissionError:
            pass

        # Platform marker subdirectories — e.g. Recalbox's 'Commodore Plus4'
        # within c64, detected by the presence of .core.cfg or .recalbox.conf.
        # ROMs inside are treated as part of the parent system; their core
        # override is handled at launch time by the platform's sidecar logic.
        subdir_markers_list = subdir_markers or []
        if subdir_markers_list:
            try:
                for entry in os.listdir(system_path):
                    entry_path = os.path.join(system_path, entry)
                    if not os.path.isdir(entry_path):
                        continue
                    if entry.startswith('.') or entry.lower() == 'media':
                        continue
                    # Only scan if the subdirectory contains a marker file
                    dir_files = os.listdir(entry_path)
                    if not any(m in dir_files for m in subdir_markers_list):
                        continue
                    log(f"  Scanning system-override subdir: {entry}/")
                    for filename in dir_files:
                        full_path = os.path.join(entry_path, filename)
                        if not os.path.isfile(full_path):
                            continue
                        ext = os.path.splitext(filename)[1].lower()
                        if ext not in valid_exts:
                            continue
                        if filename.startswith('._') or \
                                filename.startswith('.'):
                            continue
                        if os.path.getsize(full_path) == 0:
                            continue
                        system_roms.append(full_path)
            except PermissionError:
                pass

        if system_roms:
            log(f"  Found {len(system_roms)} roms in [{system}]")
        all_roms.extend(
            (system, rom) for rom in sorted(system_roms)
        )

    return all_roms

# ---------------------------------------------------------------------------
# Faulty ROM cleanup
# ---------------------------------------------------------------------------

def get_error_roms(
    results_dict: dict[str, dict],
    system_filter: str = None,
    include_imperfect: bool = False,
    include_needs_review: bool = False
) -> list[tuple[str, str]]:
    """
    Return a list of ROMs marked as ERROR or GENUINE ERROR in the audit
    results. Optionally includes IMPERFECT ROMs when include_imperfect
    is True — these are playable but emulation-imperfect and are excluded
    from cleanup by default. Optionally includes NEEDS REVIEW ROMs when
    include_needs_review is True — these are an UNVERIFIED_CORES result
    the pixel heuristic flagged (or couldn't check at all), not confirmed
    broken, so excluded from cleanup by default until a human has
    actually looked at the screenshot and confirmed it's genuinely bad.

    Args:
        results_dict:          Loaded audit results dictionary.
        system_filter:         If provided, only return ROMs from this system.
        include_imperfect:     Also include IMPERFECT status ROMs.
        include_needs_review:  Also include NEEDS REVIEW status ROMs.

    Returns:
        List of (system, romname) tuples.
    """
    target = {'ERROR', 'GENUINE ERROR'}
    if include_imperfect:
        target.add('IMPERFECT')
    if include_needs_review:
        target.add('NEEDS REVIEW')

    error_roms = []
    for row in results_dict.values():
        if row.get('status') not in target:
            continue
        system  = row.get('system', '')
        romname = row.get('rom', '')
        if system_filter and system != system_filter:
            continue
        error_roms.append((system, romname))
    return sorted(error_roms)


def move_faulty_rom(
    rom_path: str,
    faulty_base: str,
    system: str
) -> bool:
    """
    Move a faulty ROM to the quarantine folder.

    Creates the quarantine directory structure if it does not exist.
    The quarantine mirrors the source system structure for clarity.

    Quarantine structure:
        /userdata/faultyroms/
            mame/
                pacman.7z
                40love.7z

    Args:
        rom_path:    Full path to the ROM file to move.
        faulty_base: Root path of the quarantine folder.
        system:      System name (used as subdirectory).

    Returns:
        True if the move succeeded, False otherwise.
    """
    dest_dir = os.path.join(faulty_base, system)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, os.path.basename(rom_path))
    try:
        shutil.move(rom_path, dest_path)
        return True
    except Exception as e:
        log(f"  Warning: could not move {rom_path}: {e}")
        return False


def delete_faulty_rom(rom_path: str) -> bool:
    """
    Permanently delete a faulty ROM file.

    This action is irreversible. The caller is responsible for
    presenting a confirmation prompt before calling this function.

    Args:
        rom_path: Full path to the ROM file to delete.

    Returns:
        True if deletion succeeded, False otherwise.
    """
    try:
        os.remove(rom_path)
        return True
    except Exception as e:
        log(f"  Warning: could not delete {rom_path}: {e}")
        return False

def get_cpu_temp() -> str:
    """
    Read CPU temperature from the thermal zone sysfs interface.

    Returns:
        Formatted temperature string e.g. '54.3°C', or '---'
        if the temperature cannot be read.
    """
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            temp = int(f.read().strip()) / 1000
            return f"{temp:.1f}°C"
    except Exception:
        return '---'


def discover_ports(
    ports_path: str
) -> list[tuple[str, str, str]]:
    """
    Discover launchable ports via gamelist.xml manifests.

    Unlike standard ROM discovery (which scans for known file extensions),
    ports are self-contained game directories each with a gamelist.xml that
    identifies the launchable file via a <path> tag. This function parses
    those manifests and returns the actual files emulatorlauncher needs.

    Ports that have no gamelist.xml, a roms_needed.txt (user must supply
    proprietary files), or whose <path> targets don't exist on disk are
    silently skipped.

    Args:
        ports_path: Directory containing port subdirectories
                    (e.g. /recalbox/share/roms/ports).

    Returns:
        Sorted list of (system, actual_path, display_name) triples where:
            system       = 'ports'
            actual_path  = resolved path to the launchable file
            display_name = <name> tag from gamelist.xml (e.g. 'Quake'),
                           falls back to port directory name if absent.
    """
    import xml.etree.ElementTree as ET

    results: list[tuple[str, str, str]] = []

    if not os.path.isdir(ports_path):
        log(f"  Ports: directory not found: {ports_path}")
        return results

    for port_name in sorted(os.listdir(ports_path)):
        port_dir = os.path.join(ports_path, port_name)
        if not os.path.isdir(port_dir):
            continue

        # Port explicitly requires user-supplied proprietary files
        if os.path.exists(os.path.join(port_dir, 'roms_needed.txt')):
            log(f"  Ports: skipping '{port_name}' — roms_needed.txt present")
            continue

        gamelist = os.path.join(port_dir, 'gamelist.xml')
        if not os.path.exists(gamelist):
            log(f"  Ports: skipping '{port_name}' — no gamelist.xml")
            continue

        try:
            root = ET.parse(gamelist).getroot()
        except ET.ParseError as e:
            # Some gamelists have encoding issues or malformed tags.
            # Try a lenient re-parse stripping the offending bytes.
            try:
                with open(gamelist, 'rb') as _f:
                    raw = _f.read()
                # Remove any non-UTF-8 bytes and retry
                cleaned = raw.decode('utf-8', errors='replace')
                # Strip characters that break the XML parser
                import re as _re
                cleaned = _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]',
                                  '', cleaned)
                root = ET.fromstring(cleaned)
            except Exception:
                log(f"  Ports: could not parse {gamelist}: {e}")
                continue

        for game in root.findall('game'):
            path_elem = game.find('path')
            name_elem = game.find('name')
            hidden_elem = game.find('hidden')

            if path_elem is None or not (path_elem.text or '').strip():
                continue

            # Respect ES's own <hidden> flag — hidden entries are not
            # shown or launched by EmulationStation and should not be
            # tested. This is the mechanism Recalbox uses to include
            # engine/tool files (ecwolf.pk3, CATALOG.EXE) in the
            # directory without surfacing them as launchable games.
            if (hidden_elem is not None
                    and (hidden_elem.text or '').strip().lower() == 'true'):
                continue

            if path_elem is None or not (path_elem.text or '').strip():
                continue

            # Resolve relative path.
            # Gamelist paths may be './relative/path' or just 'relative'.
            raw = path_elem.text.strip()
            if raw.startswith('./'):
                rel = raw[2:]
            elif raw.startswith('/'):
                rel = raw.lstrip('/')   # absolute — make relative
            else:
                rel = raw
            actual = os.path.join(port_dir, rel)
            disp   = (name_elem.text or '').strip() or port_name

            if os.path.isfile(actual):
                results.append(('ports', actual, disp))
            elif os.path.isdir(actual):
                # Directory target — port likely needs user-supplied
                # proprietary data files. Skip rather than attempting.
                log(f"  Ports: '{port_name}' target is a directory "
                    f"(proprietary data required?): {rel}")
            else:
                log(f"  Ports: '{port_name}' path not on disk: {actual}")

    return sorted(results, key=lambda t: t[2].lower())

# ---------------------------------------------------------------------------
# Result recording
# ---------------------------------------------------------------------------

def record_result(
    already_tested: dict,
    platform,
    system: str,
    romname: str,
    status: str,
    notes: str,
    elapsed: float,
    rom: str = '',
    checksum_algorithm: str = '',
    dashboard=None,
    state: dict | None = None,
) -> str:
    """
    Record a single test result to the in-memory dict and CSV file.

    Archives error logs for failing ROMs; clears archived logs for ROMs
    that now pass. Optionally records a file checksum — sticky across
    runs: if checksum_algorithm is empty this call, the existing
    checksum (if any) from a prior run is kept rather than blanked.

    Checksum validation and recording are opt-in via checksum_algorithm.
    When provided, the ROM file is always read fresh, compared against any
    existing checksum on record (mismatch = file changed since last run),
    and the new value written. When absent, the existing checksum is kept
    as-is with no file I/O — plain recheck/autofix runs incur no hashing
    cost. Previously validation ran unconditionally whenever a checksum was
    on record, causing silent 30-60s stalls on large files (e.g. Dreamcast
    CHDs) during runs where --checksum was never requested.

    Args:
        already_tested:     In-memory results dictionary to update.
        platform:           Platform instance providing paths.
        system:             System name (e.g. 'mame').
        romname:            ROM filename (e.g. 'pacman.zip').
        status:             Result status string.
        notes:               Error notes or empty string.
        elapsed:             Time taken in seconds.
        rom:                 Full ROM path (needed for checksum).
        checksum_algorithm:  'md5', 'sha1', or '' to keep the existing
                             checksum unchanged rather than recompute
                             (mismatch validation still runs either way
                             if a checksum is already on record).
    """
    from datetime import datetime

    key = f'{system}:{romname}'

    if status != 'OK':
        save_error_logs(
            platform.error_log_base, system, romname,
            platform.stdout_log, platform.stderr_log
        )
    else:
        clear_error_logs(platform.error_log_base, system, romname)

    existing_checksum = already_tested.get(key, {}).get('checksum', '')
    mismatch_note   = ''
    checksum_result = ''   # set to 'OK' or 'MISMATCH' if validation runs

    def _make_progress_cb(label: str, algo: str):
        """Return a dashboard progress callback for a checksum read, or None."""
        if dashboard is None or state is None:
            return None
        def _cb(done: int, total: int, partial_hex: str):
            pct = f'{done * 100 // total}%' if total else '...'
            state['current_status'] = f'{label} ({pct})'
            state['checksum_info']  = f'{algo}:{partial_hex}'
            state['elapsed'] = __import__('time').time() - state['start_time']
            dashboard.update(state)
        return _cb

    # Checksum handling — only when --checksum is explicitly passed this run.
    #
    # Validation and recording are deliberately unified: when checksum_algorithm
    # is provided we always recompute from the file, compare against any existing
    # checksum on record (mismatch = file changed since last recorded), then
    # write the fresh value. This is the right time to catch a changed file —
    # the user explicitly asked for hashing this run, so the cost is expected.
    #
    # When checksum_algorithm is absent the existing checksum is kept as-is.
    # Previously validation ran unconditionally whenever a checksum was on
    # record, regardless of whether --checksum was passed — silent 30-60s
    # stalls on large CHDs (full file read with no dashboard feedback) were
    # the result. Validation is now opt-in via --checksum, same as recording.
    if checksum_algorithm and rom and os.path.exists(rom):
        algo = checksum_algorithm
        fresh = compute_checksum(
            rom, algo,
            progress_callback=_make_progress_cb('Computing checksum', algo),
        )
        if fresh:
            if existing_checksum and fresh != existing_checksum:
                mismatch_note = (
                    f' CHECKSUM MISMATCH: file changed since last recorded '
                    f'(was {existing_checksum}, now {fresh})'
                )
                log(f'  ⚠ CHECKSUM MISMATCH: {romname} — was '
                    f'{existing_checksum}, now {fresh}. '
                    f'File may have been modified, corrupted, or replaced.')
                checksum_result = 'MISMATCH'
            elif existing_checksum:
                log(f'  Checksum OK: {algo.upper()} {fresh} verified for {romname}')
                checksum_result = 'OK'
            else:
                log(f'  Checksum recorded: {fresh}')
            checksum = fresh
        else:
            checksum = existing_checksum
    else:
        checksum = existing_checksum

    notes = f'{notes}{mismatch_note}' if mismatch_note else notes

    already_tested[key] = {
        'system':          system,
        'rom':             romname,
        'status':          status,
        'elapsed_seconds': f'{elapsed:.1f}',
        'tested_at':       datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'checksum':        checksum,
        'notes':           notes
    }
    save_results(platform.results_csv, already_tested)
    return checksum_result


# ---------------------------------------------------------------------------
# Cleanup pass
# ---------------------------------------------------------------------------

def run_cleanup(
    platform,
    args,
    specific_rom: str = None
) -> None:
    """
    Move or delete ROMs that remain marked as ERROR after remediation.

    When specific_rom is provided, processes just that one ROM regardless
    of its CSV status. When None, processes all ERROR ROMs from the CSV.

    Args:
        platform:     Platform instance providing paths and ROM base.
        args:         Parsed argument namespace (action, dry_run, system).
        specific_rom: Optional ROM filename to process individually.
    """
    already_tested = load_results(platform.results_csv)

    if specific_rom:
        log(f'Single ROM cleanup: {specific_rom}')
        system   = (args.system[0] if args.system else 'unknown')
        rom_path = os.path.join(platform.roms_path, system, specific_rom)

        if not os.path.exists(rom_path):
            if not args.system:
                matches = glob.glob(
                    os.path.join(platform.roms_path, '*', specific_rom)
                )
                if matches:
                    rom_path = matches[0]
                    system   = os.path.basename(os.path.dirname(rom_path))
                    log(f'Found in system: [{system}]')
                else:
                    log(f'ERROR: {specific_rom} not found on disk.')
                    return
            else:
                log(f'ERROR: {specific_rom} not found in [{system}].')
                return

        roms_to_process = [(system, specific_rom, rom_path)]

    else:
        include_imperfect = getattr(args, 'include_imperfect', False)
        include_needs_review = getattr(args, 'include_needs_review', False)
        error_roms = get_error_roms(
            already_tested,
            args.system[0] if args.system and len(args.system) == 1 else None,
            include_imperfect=include_imperfect,
            include_needs_review=include_needs_review
        )

        if not error_roms:
            log('No ERROR ROMs found in audit results. Nothing to clean up.')
            log('To move a specific ROM regardless of status, pass its '
                'filename as an argument:')
            log('  python3 rom_audit.py --cleanup --action move '
                '--system mame aerofgts.7z')
            return

        log(f'Found {len(error_roms)} ERROR ROM(s):')
        roms_to_process = []
        for system, romname in error_roms:
            rom_path = os.path.join(platform.roms_path, system, romname)
            if os.path.exists(rom_path):
                roms_to_process.append((system, romname, rom_path))
            else:
                log(f'  [{system}] {romname} — not found on disk, skipping')

        if not roms_to_process:
            log('No ROM files found on disk to process.')
            return

    log(f'\nROMs to {args.action} ({len(roms_to_process)}):')
    for system, romname, rom_path in roms_to_process:
        if args.action == 'move':
            dest = os.path.join(platform.faulty_roms_path, system, romname)
            log(f'  [{system}] {romname}')
            log(f'           -> {dest}')
        else:
            log(f'  [{system}] {romname}  *** PERMANENT DELETE ***')

    if args.dry_run:
        log(f'\nDry run complete — no changes made.')
        log('Remove --dry-run to apply.')
        return

    if args.action == 'delete':
        log(f'\nWARNING: This will permanently delete '
            f'{len(roms_to_process)} ROM file(s).')
        log('This cannot be undone. Consider --action move instead.')
        try:
            confirm = input('\nType DELETE to confirm, anything else cancels: ')
        except (EOFError, KeyboardInterrupt):
            log('\nCancelled.')
            return
        if confirm.strip() != 'DELETE':
            log('Cancelled.')
            return

    processed = 0
    for system, romname, rom_path in roms_to_process:
        key = f'{system}:{romname}'
        if args.action == 'move':
            if move_faulty_rom(rom_path, platform.faulty_roms_path, system):
                log(f'  Moved: [{system}] {romname}')
                already_tested.setdefault(key, {'system': system, 'rom': romname})
                already_tested[key]['status'] = 'QUARANTINED'
                already_tested[key]['notes']  = (
                    f'Moved to {platform.faulty_roms_path}/{system}/'
                )
                processed += 1
        else:
            if delete_faulty_rom(rom_path):
                log(f'  Deleted: [{system}] {romname}')
                already_tested.setdefault(key, {'system': system, 'rom': romname})
                already_tested[key]['status'] = 'DELETED'
                already_tested[key]['notes']  = 'Permanently deleted'
                processed += 1

    save_results(platform.results_csv, already_tested)
    log(f'\nCleanup complete. {processed}/{len(roms_to_process)} ROM(s) {args.action}d.')
    log('CSV updated.')