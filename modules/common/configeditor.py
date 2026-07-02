"""
batocera.conf editor for the ROM Audit Tool.

Provides safe read, write and removal of per-game configuration entries
in batocera.conf. Handles deduplication to ensure no ROM ends up with
conflicting entries after multiple autofix attempts.

Per-game entry format in batocera.conf:
    system["romname"].core=corename
    system["romname"].emulator=libretro

Where system is e.g. 'mame' or 'fbneo', and romname is the ROM filename
including extension e.g. 'pacman.7z'.
"""

from __future__ import annotations  # Python 3.7 compatibility

import os
import re
from modules.common.logging import log


def _game_pattern(romname: str) -> re.Pattern:
    """
    Build a regex pattern matching all per-game entries for a ROM.

    Matches any line of the form:
        anysystem["romname"].anykey=anyvalue

    Args:
        romname: ROM filename e.g. 'pacman.7z'

    Returns:
        Compiled regex pattern.
    """
    escaped = re.escape(romname)
    return re.compile(
        rf'^[a-zA-Z0-9_]+\["{escaped}"\]\.[a-zA-Z_.]+=.*$'
    )


def read_game_entries(conf_path: str, romname: str) -> list[str]:
    """
    Return all existing per-game config lines for a ROM.

    Useful for inspecting what is currently configured before making
    changes, and for restoring the original state if needed.

    Args:
        conf_path: Full path to batocera.conf.
        romname:   ROM filename e.g. 'pacman.7z'.

    Returns:
        List of matching line strings, preserving original content.
        Empty list if no entries exist or the file cannot be read.
    """
    pattern = _game_pattern(romname)
    try:
        with open(conf_path, 'r') as f:
            return [
                line for line in f
                if pattern.match(line.strip())
            ]
    except Exception:
        return []


def remove_game_entries(conf_path: str, romname: str) -> int:
    """
    Remove all per-game config entries for a ROM from batocera.conf.

    Removes entries for any system prefix (mame, fbneo, etc.) to ensure
    a clean slate before writing new entries. All other lines including
    comments and global settings are preserved exactly.

    Args:
        conf_path: Full path to batocera.conf.
        romname:   ROM filename e.g. 'pacman.7z'.

    Returns:
        Number of lines removed. 0 if file cannot be modified.
    """
    pattern = _game_pattern(romname)
    try:
        with open(conf_path, 'r') as f:
            lines = f.readlines()

        new_lines = [
            line for line in lines
            if not pattern.match(line.strip())
        ]
        removed = len(lines) - len(new_lines)

        with open(conf_path, 'w') as f:
            f.writelines(new_lines)

        return removed

    except Exception as e:
        log(f"  Warning: could not modify {conf_path}: {e}")
        return 0


def write_game_entries(
    conf_path: str,
    system: str,
    romname: str,
    core: str,
    emulator: str
) -> bool:
    """
    Write per-game core and emulator entries for a ROM.

    Removes all existing entries for the ROM first to prevent duplicates,
    then appends the new entries to the end of the file. The removal
    covers all system prefixes so switching from mame to fbneo entries
    does not leave stale mame entries behind.

    Args:
        conf_path: Full path to batocera.conf.
        system:    System prefix for the new entries e.g. 'mame', 'fbneo'.
        romname:   ROM filename e.g. 'pacman.7z'.
        core:      Core name e.g. 'mame', 'mame078plus', 'fbneo'.
        emulator:  Emulator name, typically 'libretro'.

    Returns:
        True if entries were written successfully, False otherwise.
    """
    remove_game_entries(conf_path, romname)

    entries = [
        f'{system}["{romname}"].core={core}\n',
        f'{system}["{romname}"].emulator={emulator}\n',
    ]

    try:
        with open(conf_path, 'a') as f:
            f.writelines(entries)
        return True
    except Exception as e:
        log(f"  Warning: could not write to {conf_path}: {e}")
        return False


def deduplicate_conf(conf_path: str) -> int:
    """
    Remove exact duplicate lines from batocera.conf.

    Preserves the first occurrence of each line and removes subsequent
    duplicates. Comments, blank lines and ordering are preserved.
    Called automatically by backup_conf so both the working file and
    the backup are clean before any autofix session begins.

    Args:
        conf_path: Full path to batocera.conf.

    Returns:
        Number of duplicate lines removed. 0 if nothing to do or
        the file cannot be read.
    """
    try:
        with open(conf_path, 'r') as f:
            lines = f.readlines()

        seen     = set()
        new_lines = []
        removed  = 0

        for line in lines:
            key = line.rstrip('\n')
            if key in seen and key.strip():
                # Skip non-blank duplicate lines
                removed += 1
                log(f"  Removed duplicate entry: {key.strip()}")
            else:
                seen.add(key)
                new_lines.append(line)

        if removed:
            with open(conf_path, 'w') as f:
                f.writelines(new_lines)
            log(f"  Removed {removed} duplicate line(s) from batocera.conf")

        return removed

    except Exception as e:
        log(f"  Warning: could not deduplicate {conf_path}: {e}")
        return 0


def remove_system_entries(conf_path: str, system: str) -> int:
    """
    Remove global system-level entries for a system from batocera.conf.

    Matches lines of the form:
        system.core=...
        system.emulator=...

    These are system-wide overrides that affect all ROMs in a system.
    Per-game entries (system["romname"].core=...) are NOT affected.

    Useful for cleaning up global defaults that were inadvertently
    written to batocera.conf (e.g. mame.core=fbneo appearing globally
    rather than per-game).

    Args:
        conf_path: Full path to batocera.conf.
        system:    System name e.g. 'mame', 'fbneo'.

    Returns:
        Number of lines removed. 0 if none found or file unreadable.
    """
    # Matches: system.anykey=anyvalue
    # Does NOT match: system["romname"].anykey=anyvalue
    pattern = re.compile(
        rf'^{re.escape(system)}\.[a-zA-Z_]+=.*$'
    )
    try:
        with open(conf_path, 'r') as f:
            lines = f.readlines()

        new_lines = []
        removed   = 0
        for line in lines:
            if pattern.match(line.strip()):
                removed += 1
                log(f"  Removed global entry: {line.strip()}")
            else:
                new_lines.append(line)

        if removed:
            with open(conf_path, 'w') as f:
                f.writelines(new_lines)
            log(f"  Removed {removed} global [{system}] "
                f"entr{'y' if removed == 1 else 'ies'} from batocera.conf")

        return removed

    except Exception as e:
        log(f"  Warning: could not modify {conf_path}: {e}")
        return 0


def backup_conf(conf_path: str) -> str:
    """
    Create a backup of batocera.conf before autofix begins.

    Deduplicates the conf file first so the backup and working copy
    are both clean. Backs up to <conf_path>.autofix.bak, overwriting
    any previous backup.

    Args:
        conf_path: Full path to batocera.conf.

    Returns:
        Path to the backup file, or empty string if backup failed.
    """
    # Clean up duplicates before backing up
    deduplicate_conf(conf_path)

    backup_path = conf_path + ".autofix.bak"
    try:
        import shutil
        shutil.copy2(conf_path, backup_path)
        log(f"  Config backed up to: {backup_path}")
        return backup_path
    except Exception as e:
        log(f"  Warning: could not back up {conf_path}: {e}")
        return ""

# ---------------------------------------------------------------------------
# Temporary suspension of global system overrides during testing
# ---------------------------------------------------------------------------

def get_system_overrides(conf_path: str, systems: set) -> dict:
    """
    Find global system-level core/emulator overrides in batocera.conf.

    Matches lines of the form:
        system.core=corename
        system.emulator=libretro

    These are system-wide defaults that apply to ALL ROMs in that system.
    They can mask genuine failures when a core exits silently without logging
    errors (e.g. mame.core=fbneo producing false OK results for unsupported
    ROMs).

    Args:
        conf_path: Full path to batocera.conf.
        systems:   Set of system names to look for e.g. {'mame', 'fbneo'}.

    Returns:
        Dict of {system: {key: value}} for each override found.
        e.g. {'mame': {'core': 'fbneo', 'emulator': 'libretro'}}
    """
    pattern = re.compile(
        '^(' + '|'.join(re.escape(s) for s in systems) + r')\.(core|emulator)=(.+)$'
    )
    found: dict = {}
    try:
        with open(conf_path, 'r') as f:
            for line in f:
                m = pattern.match(line.strip())
                if m:
                    system, key, value = m.group(1), m.group(2), m.group(3)
                    found.setdefault(system, {})[key] = value
    except Exception as e:
        log(f"  Warning: could not read {conf_path}: {e}")
    return found


def cleanup_stale_markers(conf_path: str) -> int:
    """
    Restore any #ROMAUDIT# markers already present in the file,
    regardless of whether THIS session suspended them.

    restore_system_overrides() only restores what its `suspended`
    argument says THIS session suspended — by design, since the
    common case (nothing to suspend) should skip file I/O entirely.
    But that means a run killed abruptly (SIGKILL, power loss, a
    crashed SSH session — anything that skips the `finally` block)
    leaves its #ROMAUDIT# markers in the file indefinitely: the next
    run's own suspend_system_overrides() won't find anything to
    re-suspend for that system (the line's already commented, so it
    no longer matches the active-line pattern), and that run has no
    reason to call restore for a system it never touched. The global
    override stays disabled on the real system until something
    happens to notice — which could be a long time, including outside
    the audit tool's own context (e.g. EmulationStation gameplay
    behaving differently than the cabinet owner configured).

    Call this BEFORE this session's own suspend_system_overrides(),
    so every run starts from a guaranteed-clean baseline no matter how
    the previous one ended. Safe to call even when there's nothing to
    clean up — returns 0 and makes no file changes in that case.

    Returns the number of stale markers found and restored.
    """
    try:
        with open(conf_path, 'r') as f:
            lines = f.readlines()
    except Exception as e:
        log(f"  Warning: could not read {conf_path} for marker "
            f"cleanup: {e}")
        return 0

    restored_lines = []
    count = 0
    for line in lines:
        if line.startswith('#ROMAUDIT#'):
            restored_lines.append(line[len('#ROMAUDIT#'):])
            count += 1
        else:
            restored_lines.append(line)

    if count == 0:
        return 0

    try:
        with open(conf_path, 'w') as f:
            f.writelines(restored_lines)
    except Exception as e:
        log(f"  Warning: could not write {conf_path} during marker "
            f"cleanup: {e}")
        return 0

    return count


def suspend_system_overrides(conf_path: str, systems: set) -> dict:
    """
    Temporarily comment out global system.core and system.emulator lines.

    Lines are prefixed with '#ROMAUDIT#' so restore_system_overrides() can
    find and uncomment exactly what was suspended. The original lines remain
    in the file in commented form so the file survives a tool crash cleanly.

    Args:
        conf_path: Full path to batocera.conf.
        systems:   Set of system names to suspend e.g. {'mame', 'fbneo'}.

    Returns:
        Dict of what was suspended (passed directly to restore_system_overrides).
        Empty dict if nothing was suspended or the file could not be read.
    """
    pattern = re.compile(
        '^(' + '|'.join(re.escape(s) for s in systems) + r')\.(core|emulator)=(.+)$'
    )
    suspended: dict = {}
    try:
        with open(conf_path, 'r') as f:
            lines = f.readlines()

        new_lines = []
        for line in lines:
            m = pattern.match(line.strip())
            if m:
                system, key, value = m.group(1), m.group(2), m.group(3)
                suspended.setdefault(system, {})[key] = value
                # Comment the line out with a restorable marker
                new_lines.append(f'#ROMAUDIT#{line.rstrip()}\n')
            else:
                new_lines.append(line)

        if suspended:
            with open(conf_path, 'w') as f:
                f.writelines(new_lines)

    except Exception as e:
        log(f"  Warning: could not suspend overrides in {conf_path}: {e}")
    return suspended


def restore_system_overrides(conf_path: str, suspended: dict) -> None:
    """
    Restore global system override lines that were suspended by
    suspend_system_overrides().

    Finds lines prefixed with '#ROMAUDIT#' and uncomments them. Safe to call
    even if the tool crashed mid-audit — the commented lines are still in the
    file and will be restored cleanly.

    Args:
        conf_path: Full path to batocera.conf.
        suspended: Dict returned by suspend_system_overrides() (may be empty).
    """
    if not suspended:
        return
    try:
        with open(conf_path, 'r') as f:
            lines = f.readlines()

        new_lines = []
        for line in lines:
            if line.startswith('#ROMAUDIT#'):
                # Restore the original line
                new_lines.append(line[len('#ROMAUDIT#'):])
            else:
                new_lines.append(line)

        with open(conf_path, 'w') as f:
            f.writelines(new_lines)

    except Exception as e:
        log(f"  Warning: could not restore overrides in {conf_path}: {e}")

