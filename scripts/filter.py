#!/usr/bin/env python3
"""
ROM Audit Tool — CSV Filter

Filters the audit CSV by system and/or status and outputs
matching rows. Useful for building targeted fix lists or
exporting specific subsets for further processing.

Usage:
    python3 scripts/filter.py --status ERROR
    python3 scripts/filter.py --system arcade
    python3 scripts/filter.py --system arcade --status "GENUINE ERROR"
    python3 scripts/filter.py --status ERROR --status "GENUINE ERROR"
    python3 scripts/filter.py --system arcade --status ERROR --csv-out errors.csv
    python3 scripts/filter.py --status "GENUINE ERROR" --roms-only
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import csv
import sys
import argparse
from collections import defaultdict


# ---------------------------------------------------------------------------
# Default CSV locations
# ---------------------------------------------------------------------------

DEFAULT_CSV_PATHS = [
    '/home/pi/RetroPie/rom_audit/rom_audit.csv',
    '/userdata/system/rom_audit/rom_audit.csv',
    'rom_audit.csv',
]

FIELD_WIDTHS = {
    'system':          14,
    'rom':             50,
    'status':          14,
    'elapsed_seconds':  7,
    'tested_at':       20,
    'notes':           60,
}


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


def filter_rows(
    rows: list[dict],
    systems: list[str] = None,
    statuses: list[str] = None,
    notes_contains: str = None,
) -> list[dict]:
    """
    Filter rows by system, status, and/or notes substring.

    All filters are case-insensitive. Multiple statuses are OR'd together.
    Multiple systems are OR'd together. All active filters are AND'd.
    """
    result = []
    systems_lower  = [s.lower() for s in systems]  if systems  else None
    statuses_lower = [s.lower() for s in statuses] if statuses else None

    for row in rows:
        system = row.get('system', '').strip().lower()
        status = row.get('status', '').strip().lower()
        notes  = row.get('notes',  '').strip().lower()

        if systems_lower and system not in systems_lower:
            continue
        if statuses_lower and status not in statuses_lower:
            continue
        if notes_contains and notes_contains.lower() not in notes:
            continue

        result.append(row)

    return result


def print_table(rows: list[dict], roms_only: bool = False) -> None:
    """Print filtered rows as a formatted table or plain ROM list."""
    if not rows:
        print("No matching rows.")
        return

    if roms_only:
        for row in rows:
            print(row.get('rom', ''))
        return

    # Determine column widths from data
    widths = {k: len(k) for k in FIELD_WIDTHS}
    for row in rows:
        for col in widths:
            val = str(row.get(col, ''))
            widths[col] = max(widths[col], min(len(val), FIELD_WIDTHS[col]))

    # Header
    header = '  '.join(f"{col:<{widths[col]}}" for col in widths)
    sep    = '  '.join('-' * widths[col] for col in widths)
    print()
    print(header)
    print(sep)

    # Rows
    for row in rows:
        line_parts = []
        for col in widths:
            val = str(row.get(col, ''))
            # Truncate long values
            if len(val) > widths[col]:
                val = val[:widths[col] - 1] + '…'
            line_parts.append(f"{val:<{widths[col]}}")
        print('  '.join(line_parts))

    print()
    print(f"  {len(rows)} row(s) matched.")
    print()


def write_csv(rows: list[dict], path: str) -> None:
    """Write filtered rows to a new CSV file."""
    if not rows:
        print("No rows to write.")
        return

    fieldnames = [
        'system', 'rom', 'status',
        'elapsed_seconds', 'tested_at', 'notes'
    ]
    try:
        with open(path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Written {len(rows)} row(s) to: {path}")
    except Exception as e:
        print(f"ERROR: Could not write {path}: {e}")


def print_status_counts(rows: list[dict]) -> None:
    """Print a brief count of statuses in the filtered results."""
    counts = defaultdict(int)
    for row in rows:
        counts[row.get('status', '').strip()] += 1
    parts = [f"{status}: {count}" for status, count in sorted(counts.items())]
    print(f"  Counts — {' | '.join(parts)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='ROM Audit Tool — Filter CSV results'
    )
    parser.add_argument(
        '--csv',
        help='Path to rom_audit.csv (auto-detected if not specified)'
    )
    parser.add_argument(
        '--system',
        action='append',
        dest='systems',
        metavar='SYSTEM',
        help='Filter by system name (repeatable: --system arcade --system mame)'
    )
    parser.add_argument(
        '--status',
        action='append',
        dest='statuses',
        metavar='STATUS',
        help=(
            'Filter by status (repeatable: --status ERROR --status "GENUINE ERROR")\n'
            'Valid values: OK, FIXED, IMPERFECT, NEEDS REVIEW, ERROR,\n'
            '              GENUINE ERROR, NO COMBINATIONS, TIMEOUT,\n'
            '              LAUNCHED, MISSING CORE, MISSING BIOS,\n'
            '              QUARANTINED, DELETED'
        )
    )
    parser.add_argument(
        '--notes',
        metavar='TEXT',
        help='Filter rows where notes contains TEXT (case-insensitive)'
    )
    parser.add_argument(
        '--csv-out',
        metavar='FILE',
        help='Write matching rows to a new CSV file'
    )
    parser.add_argument(
        '--roms-only',
        action='store_true',
        help='Output ROM filenames only, one per line (useful for scripting)'
    )
    parser.add_argument(
        '--counts',
        action='store_true',
        help='Show status counts for the filtered results'
    )
    args = parser.parse_args()

    if not args.systems and not args.statuses and not args.notes:
        parser.print_help()
        print("\nERROR: Specify at least one of --system, --status or --notes")
        sys.exit(1)

    csv_path = args.csv or find_csv()
    if not csv_path:
        print("ERROR: Could not find rom_audit.csv.")
        print("Specify with: --csv /path/to/rom_audit.csv")
        sys.exit(1)

    if not args.roms_only:
        print(f"Reading: {csv_path}")

    rows    = load_csv(csv_path)
    matched = filter_rows(
        rows,
        systems        = args.systems,
        statuses       = args.statuses,
        notes_contains = args.notes,
    )

    if args.csv_out:
        write_csv(matched, args.csv_out)
    elif args.roms_only:
        print_table(matched, roms_only=True)
    else:
        print_table(matched)
        if args.counts:
            print_status_counts(matched)


if __name__ == '__main__':
    main()
