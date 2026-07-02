#!/usr/bin/env python3
"""
ROM Audit Tool — Prune Orphaned CSV Entries

The discovery fix in 1.4.1 (and the earlier .cue/.bin fix in 1.3.0)
means certain files that used to be discovered and tested as
independent ROMs are now correctly recognised as companions of a
controlling file (.cue or .uae) and excluded from discovery. Existing
CSV rows for those files don't disappear on their own — discovery
just stops re-adding them — so without this script they sit there
forever, inflating totals in summary.py and the dashboard.

This script finds two distinct categories of orphaned row and reports
them separately, since they mean different things:

  COMPANION  — the file still exists, but is now correctly recognised
               as a companion of a .cue/.uae (the case this script was
               written for). Safe to remove with --remove.

  MISSING    — the file no longer exists on disk at all (deleted,
               renamed, or moved out of the collection). A different
               situation — removed only if you also pass
               --remove-missing, since you may want to investigate
               rather than silently lose that history.

Usage:
    python3 scripts/prune_orphaned_entries.py                  # report only
    python3 scripts/prune_orphaned_entries.py --remove          # remove COMPANION rows
    python3 scripts/prune_orphaned_entries.py --remove --remove-missing  # remove both
    python3 scripts/prune_orphaned_entries.py --csv /path/to/rom_audit.csv --roms /path/to/roms
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import csv
import sys
import argparse


DEFAULT_CSV_PATHS = [
    '/home/pi/RetroPie/rom_audit/rom_audit.csv',
    '/userdata/system/rom_audit/rom_audit.csv',
    '/recalbox/share/system/rom_audit/rom_audit.csv',
    'rom_audit.csv',
]

DEFAULT_ROM_PATHS = [
    '/home/pi/RetroPie/roms',
    '/userdata/roms',
    '/recalbox/share/roms',
]

# Raw data-file extensions that are companions of a controlling file
# when one exists in the same folder — mirrors filehandling.py's
# discover_roms() exactly. See CLAUDE.md "Companion-file discovery
# pattern" for the full explanation.
CD_RAW_EXTENSIONS = {'.bin', '.img', '.iso', '.sub', '.raw', '.wav'}
AMIGA_RAW_EXTENSIONS = {'.adf', '.hdf', '.dms', '.dmz', '.adz'}

RED    = '\033[31m'
YELLOW = '\033[33m'
GREEN  = '\033[32m'
CYAN   = '\033[36m'
BOLD   = '\033[1m'
RESET  = '\033[0m'


def find_csv() -> str:
    for path in DEFAULT_CSV_PATHS:
        if os.path.exists(path):
            return path
    return None


def find_roms_path() -> str:
    for path in DEFAULT_ROM_PATHS:
        if os.path.isdir(path):
            return path
    return None


def build_companion_stems(system_path: str) -> set:
    """
    Returns the set of filename stems (lowercase, no extension) that
    are companions of a .cue or .uae controlling file in this system
    folder — the controlling file's own stem plus every raw file it
    references internally.
    """
    stems = set()
    try:
        entries = os.listdir(system_path)
    except (PermissionError, FileNotFoundError):
        return stems

    for f in entries:
        lower = f.lower()
        full_path = os.path.join(system_path, f)
        if not os.path.isfile(full_path):
            continue

        if lower.endswith('.cue'):
            stems.add(os.path.splitext(f)[0].lower())
            try:
                with open(full_path, 'r', errors='replace') as fh:
                    for line in fh:
                        line = line.strip()
                        if line.upper().startswith('FILE '):
                            parts = line.split('"')
                            if len(parts) >= 3:
                                stems.add(
                                    os.path.splitext(parts[1])[0].lower()
                                )
            except Exception:
                pass

        elif lower.endswith('.uae'):
            stems.add(os.path.splitext(f)[0].lower())
            try:
                with open(full_path, 'r', errors='replace') as fh:
                    for line in fh:
                        line = line.strip()
                        if '=' not in line:
                            continue
                        _, _, value = line.partition('=')
                        value = value.strip()
                        ext = os.path.splitext(value)[1].lower()
                        if ext in AMIGA_RAW_EXTENSIONS:
                            ref_stem = os.path.splitext(
                                os.path.basename(value)
                            )[0].lower()
                            stems.add(ref_stem)
            except Exception:
                pass

    return stems


def classify_row(roms_path: str, system: str, rom: str,
                  companion_cache: dict) -> str:
    """
    Returns 'OK' (still a legitimate standalone ROM), 'COMPANION'
    (now correctly excluded — file exists but belongs to a .cue/.uae),
    or 'MISSING' (file no longer exists at all).
    """
    system_path = os.path.join(roms_path, system)
    full_path = os.path.join(system_path, rom)

    if not os.path.exists(full_path):
        return 'MISSING'

    ext = os.path.splitext(rom)[1].lower()
    if ext not in CD_RAW_EXTENSIONS and ext not in AMIGA_RAW_EXTENSIONS:
        return 'OK'

    if system not in companion_cache:
        companion_cache[system] = build_companion_stems(system_path)

    stem = os.path.splitext(rom)[0].lower()
    if stem in companion_cache[system]:
        return 'COMPANION'

    return 'OK'


def main():
    parser = argparse.ArgumentParser(
        description='ROM Audit Tool — Prune Orphaned CSV Entries'
    )
    parser.add_argument('--csv', help='Path to rom_audit.csv (auto-detected if not specified)')
    parser.add_argument('--roms', help='ROM base directory (auto-detected if not specified)')
    parser.add_argument('--remove', action='store_true',
                         help='Remove COMPANION rows from the CSV (default: report only)')
    parser.add_argument('--remove-missing', action='store_true',
                         help='Also remove MISSING rows (file no longer exists at all) — use with --remove')
    args = parser.parse_args()

    csv_path = args.csv or find_csv()
    if not csv_path or not os.path.exists(csv_path):
        print(f"{RED}ERROR: Could not find rom_audit.csv. Specify with --csv{RESET}")
        sys.exit(1)

    roms_path = args.roms or find_roms_path()
    if not roms_path or not os.path.isdir(roms_path):
        print(f"{RED}ERROR: Could not find ROM directory. Specify with --roms{RESET}")
        sys.exit(1)

    print(f"CSV:  {csv_path}")
    print(f"ROMs: {roms_path}\n")

    with open(csv_path, 'r', newline='') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    companion_cache = {}
    companion_rows = []
    missing_rows = []
    kept_rows = []

    for row in rows:
        system = row.get('system', '')
        rom = row.get('rom', '')
        if system == 'ports':
            # Ports use a different discovery path (gamelist.xml) with
            # display names that don't map to a file the same way —
            # not in scope for this check.
            kept_rows.append(row)
            continue

        classification = classify_row(roms_path, system, rom, companion_cache)
        if classification == 'COMPANION':
            companion_rows.append(row)
        elif classification == 'MISSING':
            missing_rows.append(row)
        else:
            kept_rows.append(row)

    if not companion_rows and not missing_rows:
        print(f"{GREEN}No orphaned entries found — CSV is clean.{RESET}")
        return

    if companion_rows:
        print(f"{YELLOW}{BOLD}COMPANION{RESET} — file exists, now correctly excluded "
              f"as a companion of a .cue/.uae ({len(companion_rows)}):")
        for row in sorted(companion_rows, key=lambda r: (r['system'], r['rom'])):
            print(f"  [{row['system']}] {row['rom']}  (was: {row.get('status', '')})")
        print()

    if missing_rows:
        print(f"{CYAN}{BOLD}MISSING{RESET} — file no longer exists on disk at all "
              f"({len(missing_rows)}):")
        for row in sorted(missing_rows, key=lambda r: (r['system'], r['rom'])):
            print(f"  [{row['system']}] {row['rom']}  (was: {row.get('status', '')})")
        print()

    if not args.remove:
        print(f"Dry run — no changes made. Re-run with --remove to delete the "
              f"COMPANION rows above" +
              (", and --remove-missing to also delete the MISSING rows." if missing_rows
               else "."))
        return

    final_rows = kept_rows
    removed_count = len(companion_rows)
    if args.remove_missing:
        removed_count += len(missing_rows)
    else:
        final_rows = final_rows + missing_rows

    tmp_path = csv_path + '.tmp'
    with open(tmp_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in final_rows:
            writer.writerow(row)
    os.replace(tmp_path, csv_path)

    print(f"{GREEN}Removed {removed_count} orphaned row(s) from {csv_path}{RESET}")
    if missing_rows and not args.remove_missing:
        print(f"  ({len(missing_rows)} MISSING row(s) left untouched — "
              f"re-run with --remove-missing to remove those too)")


if __name__ == '__main__':
    main()
