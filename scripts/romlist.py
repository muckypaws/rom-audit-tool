#!/usr/bin/env python3
"""
ROM Audit Tool — ROM List

Extracts ROM filenames with their relevant detail (error note or test
date) from the audit CSV. Designed for quick targeted lists without
the full table format of filter.py.

Output format varies by status:
    ERROR / GENUINE ERROR  →  "romname", "error note"
    MISSING BIOS           →  "romname", "MISSING BIOS"
    OK / FIXED             →  "romname", tested_at date
    TIMEOUT / IMPERFECT    →  "romname", status

Usage:
    python3 scripts/romlist.py --system gba --errors
    python3 scripts/romlist.py --system gbc --ok
    python3 scripts/romlist.py --system arcade --genuine
    python3 scripts/romlist.py --system naomi --bios
    python3 scripts/romlist.py --system mame --imperfect
    python3 scripts/romlist.py --system snes --all
    python3 scripts/romlist.py --errors                     # all systems
    python3 scripts/romlist.py --system arcade --errors --out errors.txt
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import csv
import sys
import argparse


# ---------------------------------------------------------------------------
# Default CSV locations
# ---------------------------------------------------------------------------

DEFAULT_CSV_PATHS = [
    '/home/pi/RetroPie/rom_audit/rom_audit.csv',
    '/userdata/system/rom_audit/rom_audit.csv',
    'rom_audit.csv',
]

# Status groups
ERROR_STATUSES   = {'ERROR'}
GENUINE_STATUSES   = {'GENUINE ERROR'}
BIOS_STATUSES      = {'MISSING BIOS'}
MISSING_CORE_STATUSES = {'MISSING CORE'}
IMPERFECT_STATUSES = {'IMPERFECT'}
NEEDS_REVIEW_STATUSES = {'NEEDS REVIEW'}
OK_STATUSES      = {'OK', 'FIXED'}
ALL_STATUSES     = (
    ERROR_STATUSES | GENUINE_STATUSES | BIOS_STATUSES |
    MISSING_CORE_STATUSES | IMPERFECT_STATUSES | NEEDS_REVIEW_STATUSES |
    OK_STATUSES | {'TIMEOUT', 'MISSING CORE'}
)


def find_csv() -> str:
    for path in DEFAULT_CSV_PATHS:
        if os.path.exists(path):
            return path
    return None


def load_csv(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        print(f"ERROR: Could not read {path}: {e}")
        sys.exit(1)
    return rows


def format_row(row: dict) -> str:
    """
    Format a single CSV row as a quoted filename + relevant detail.

    ERROR / GENUINE ERROR  →  "romname", "error note"
    MISSING BIOS           →  "romname", "MISSING BIOS"
    OK / FIXED             →  "romname", tested_at
    TIMEOUT / IMPERFECT    →  "romname", status
    MISSING CORE           →  "romname", "MISSING CORE"
    """
    romname  = row.get('rom', '').strip()
    status   = row.get('status', '').strip()
    notes    = row.get('notes', '').strip()
    date     = row.get('tested_at', '').strip()
    checksum = row.get('checksum', '').strip()
    # prefix: "filename", [md5:hash],   — or just  "filename",  if no checksum
    prefix = f'"{romname}", [{checksum}],' if checksum else f'"{romname}",'

    if status in ERROR_STATUSES | GENUINE_STATUSES:
        detail = f'"{notes}"' if notes else f'"{status}"'
        return f'{prefix} {detail}'

    if status in BIOS_STATUSES:
        return f'{prefix} "MISSING BIOS"'

    if status in OK_STATUSES:
        return f'{prefix} {date}'

    # TIMEOUT, LAUNCHED, MISSING CORE etc.
    return f'{prefix} "{status}"'


def filter_and_print(
    rows: list[dict],
    statuses: set[str],
    system: str = None,
    out_path: str = None
) -> None:
    """
    Filter rows and output formatted lines.

    When a system filter is provided, outputs a flat list.
    When no system filter is provided, groups output by system
    with a heading for each, so results from different systems
    are clearly separated.
    """
    # Group rows by system, preserving encounter order
    from collections import defaultdict
    grouped = defaultdict(list)
    total   = 0

    for row in rows:
        row_system = row.get('system', '').strip()
        row_status = row.get('status', '').strip()

        if system and row_system.lower() != system.lower():
            continue
        if row_status not in statuses:
            continue

        grouped[row_system].append(format_row(row))
        total += 1

    if not grouped:
        print("No matching ROMs found.")
        return

    lines = []

    if system:
        # Single system — flat list, no heading needed
        lines.extend(grouped.get(system, []) or next(iter(grouped.values())))
    else:
        # Multiple systems — group with headings
        for sys_name in sorted(grouped.keys()):
            entries = grouped[sys_name]
            lines.append(f"[{sys_name}]  ({len(entries)} ROM(s))")
            lines.append('-' * (len(sys_name) + 16))
            lines.extend(entries)
            lines.append('')   # blank line between systems

    output = '\n'.join(lines)

    if out_path:
        try:
            with open(out_path, 'w') as f:
                f.write(output + '\n')
            print(f"Written {total} entries to: {out_path}")
        except Exception as e:
            print(f"ERROR: Could not write {out_path}: {e}")
    else:
        print(output)
        print(f"  {total} ROM(s) listed.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='ROM Audit Tool — ROM filename + detail list'
    )
    parser.add_argument(
        '--csv',
        help='Path to rom_audit.csv (auto-detected if not specified)'
    )
    parser.add_argument(
        '--system',
        metavar='SYSTEM',
        help='Filter by system name e.g. gba, arcade, snes'
    )
    parser.add_argument(
        '--out',
        metavar='FILE',
        help='Write output to a text file instead of stdout'
    )

    # Status selection — mutually exclusive group
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        '--errors',
        action='store_true',
        help='List ERROR ROMs with their error message'
    )
    group.add_argument(
        '--genuine',
        action='store_true',
        help='List GENUINE ERROR ROMs (failed all autofix attempts)'
    )
    group.add_argument(
        '--bios',
        action='store_true',
        help='List MISSING BIOS ROMs'
    )
    group.add_argument(
        '--missing-core',
        action='store_true',
        help='List MISSING CORE ROMs'
    )
    group.add_argument(
        '--imperfect',
        action='store_true',
        help='List IMPERFECT ROMs (load but have known accuracy issues)'
    )
    group.add_argument(
        '--needs-review',
        action='store_true',
        help='List NEEDS REVIEW ROMs (an UNVERIFIED_CORES result the '
             'pixel heuristic flagged, or could not check at all — '
             'go look at the screenshot)'
    )
    group.add_argument(
        '--ok',
        action='store_true',
        help='List OK and FIXED ROMs with their test date'
    )
    group.add_argument(
        '--fixed',
        action='store_true',
        help='List FIXED ROMs (autofix succeeded) with their test date'
    )
    group.add_argument(
        '--timeout',
        action='store_true',
        help='List TIMEOUT ROMs'
    )
    group.add_argument(
        '--all-errors',
        action='store_true',
        help='List all failing ROMs (ERROR + GENUINE ERROR + MISSING BIOS + TIMEOUT)'
    )
    group.add_argument(
        '--all',
        action='store_true',
        help='List all ROMs regardless of status'
    )

    args = parser.parse_args()

    csv_path = args.csv or find_csv()
    if not csv_path:
        print("ERROR: Could not find rom_audit.csv.")
        print("Specify with: --csv /path/to/rom_audit.csv")
        sys.exit(1)

    rows = load_csv(csv_path)

    # Determine which statuses to include
    if args.errors:
        statuses = ERROR_STATUSES
    elif args.genuine:
        statuses = GENUINE_STATUSES
    elif args.bios:
        statuses = BIOS_STATUSES
    elif args.missing_core:
        statuses = MISSING_CORE_STATUSES
    elif args.imperfect:
        statuses = IMPERFECT_STATUSES
    elif args.needs_review:
        statuses = NEEDS_REVIEW_STATUSES
    elif args.ok:
        statuses = OK_STATUSES
    elif args.fixed:
        statuses = {'FIXED'}
    elif args.timeout:
        statuses = {'TIMEOUT', 'LAUNCHED'}
    elif args.all_errors:
        statuses = (
            ERROR_STATUSES | GENUINE_STATUSES |
            BIOS_STATUSES | {'TIMEOUT', 'LAUNCHED'}
        )
    elif args.all:
        statuses = ALL_STATUSES
    else:
        statuses = ALL_STATUSES

    system_label = f"[{args.system}] " if args.system else ""
    status_label = (
        'errors' if args.errors else
        'genuine errors' if args.genuine else
        'BIOS errors' if args.bios else
        'missing core' if args.missing_core else
        'imperfect' if args.imperfect else
        'needs review' if args.needs_review else
        'OK' if args.ok else
        'fixed' if args.fixed else
        'timeouts' if args.timeout else
        'all errors' if args.all_errors else
        'all'
    )
    print(f"# {system_label}{status_label} — {csv_path}")
    print()

    filter_and_print(
        rows,
        statuses,
        system  = args.system,
        out_path= args.out
    )


if __name__ == '__main__':
    main()
