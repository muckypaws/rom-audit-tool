#!/usr/bin/env python3
"""
ROM Audit Tool — System Summary

Reads the audit CSV and produces a per-system breakdown showing
OK vs not-OK counts for each system tested.

Usage:
    python3 scripts/summary.py
    python3 scripts/summary.py --csv /path/to/rom_audit.csv
    python3 scripts/summary.py --sort errors
    python3 scripts/summary.py --system arcade
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
    '/home/pi/RetroPie/rom_audit/rom_audit.csv',   # RetroPie
    '/userdata/system/rom_audit/rom_audit.csv',    # Batocera
    'rom_audit.csv',                                # Local fallback
]

# Statuses that count as successful
OK_STATUSES = {'OK', 'FIXED'}

# All known statuses for column ordering
ALL_STATUSES = [
    'OK', 'FIXED', 'IMPERFECT', 'NEEDS REVIEW', 'ERROR', 'GENUINE ERROR',
    'TIMEOUT', 'MISSING CORE', 'MISSING BIOS',
    'QUARANTINED', 'DELETED',
]


def find_csv() -> str:
    """Find the audit CSV from default locations."""
    for path in DEFAULT_CSV_PATHS:
        if os.path.exists(path):
            return path
    return None


def load_csv(path: str) -> list[dict]:
    """Load all rows from the audit CSV."""
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


def summarise(rows: list[dict], system_filter: str = None) -> dict:
    """
    Build per-system summary counts.

    Returns:
        Dict of system → {status: count, ...}
    """
    summary = defaultdict(lambda: defaultdict(int))

    for row in rows:
        system = row.get('system', '').strip()
        status = row.get('status', '').strip()

        if system_filter and system != system_filter:
            continue

        summary[system][status] += 1
        summary[system]['_total'] += 1

    return summary


def print_summary(
    summary: dict,
    sort_by: str = 'system'
) -> None:
    """Print the summary table to stdout."""

    if not summary:
        print("No data found.")
        return

    # Statuses that count as OK (dealt with and working)
    OK_STATUSES         = {'OK', 'FIXED'}
    # Statuses that are dealt with but not working
    RESOLVED_STATUSES   = {'QUARANTINED', 'DELETED'}

    # Determine which non-OK, non-resolved status columns have data
    active_statuses = [
        s for s in ALL_STATUSES
        if s not in OK_STATUSES and s not in RESOLVED_STATUSES
        and any(s in counts for counts in summary.values())
    ]

    # Determine which resolved columns have data
    resolved_statuses = [
        s for s in ('QUARANTINED', 'DELETED')
        if any(s in counts for counts in summary.values())
    ]

    # Column widths
    sys_w   = max(len('System'), max(len(s) for s in summary)) + 2
    count_w = 7
    ok_w    = 9
    act_w   = 13   # Active Errors

    # Header
    header = (
        f"{'System':<{sys_w}} "
        f"{'Total':>{count_w}} "
        f"{'OK+Fixed':>{ok_w}} "
        f"{'Active Errors':>{act_w}}"
    )
    for status in active_statuses:
        header += f"  {status[:12]:>{max(12, len(status))}}"
    for status in resolved_statuses:
        header += f"  {status[:12]:>{max(12, len(status))}}"

    sep = '-' * len(header)
    print()
    print(header)
    print(sep)

    # Sort
    if sort_by == 'errors':
        items = sorted(
            summary.items(),
            key=lambda x: (
                x[1].get('ERROR', 0) +
                x[1].get('GENUINE ERROR', 0)
            ),
            reverse=True
        )
    elif sort_by == 'total':
        items = sorted(
            summary.items(),
            key=lambda x: x[1].get('_total', 0),
            reverse=True
        )
    else:
        items = sorted(summary.items())

    # Totals accumulator
    grand = defaultdict(int)

    for system, counts in items:
        total      = counts.get('_total', 0)
        ok_count   = counts.get('OK', 0) + counts.get('FIXED', 0)
        resolved   = sum(counts.get(s, 0) for s in RESOLVED_STATUSES)
        # Active errors = genuinely outstanding problems
        # (failed and not yet quarantined or deleted)
        active_err = total - ok_count - resolved

        row = (
            f"{system:<{sys_w}} "
            f"{total:>{count_w}} "
            f"{ok_count:>{ok_w}} "
            f"{active_err:>{act_w}}"
        )
        for status in active_statuses:
            val   = counts.get(status, 0)
            col_w = max(12, len(status))
            row += f"  {val:>{col_w}}" if val else f"  {'-':>{col_w}}"
        for status in resolved_statuses:
            val   = counts.get(status, 0)
            col_w = max(12, len(status))
            row += f"  {val:>{col_w}}" if val else f"  {'-':>{col_w}}"

        print(row)
        grand['_total']  += total
        grand['_ok']     += ok_count
        grand['_active'] += active_err
        grand['_resolved'] += resolved
        for s in active_statuses + resolved_statuses:
            grand[s] += counts.get(s, 0)

    # Grand total
    print(sep)
    gtotal   = grand['_total']
    gok      = grand['_ok']
    gactive  = grand['_active']
    gresolved = grand['_resolved']
    gpct     = (gok / gtotal * 100) if gtotal else 0

    foot = (
        f"{'TOTAL':<{sys_w}} "
        f"{gtotal:>{count_w}} "
        f"{gok:>{ok_w}} "
        f"{gactive:>{act_w}}"
    )
    for status in active_statuses + resolved_statuses:
        val   = grand.get(status, 0)
        col_w = max(12, len(status))
        foot += f"  {val:>{col_w}}" if val else f"  {'-':>{col_w}}"
    print(foot)
    print()
    print(f"  Overall: {gok}/{gtotal} OK ({gpct:.1f}%)"
          + (f"  —  {gactive} outstanding"
             if gactive else "  —  all failures resolved")
          + (f"  —  {gresolved} quarantined/deleted"
             if gresolved else ""))
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description='ROM Audit Tool — Per-system summary'
    )
    parser.add_argument(
        '--csv',
        help='Path to rom_audit.csv (auto-detected if not specified)'
    )
    parser.add_argument(
        '--system',
        help='Show summary for a single system only'
    )
    parser.add_argument(
        '--sort',
        choices=['system', 'errors', 'total'],
        default='system',
        help='Sort order: system name (default), most errors, most ROMs'
    )
    args = parser.parse_args()

    csv_path = args.csv or find_csv()
    if not csv_path:
        print("ERROR: Could not find rom_audit.csv.")
        print("Specify with: --csv /path/to/rom_audit.csv")
        sys.exit(1)

    print(f"Reading: {csv_path}")
    rows    = load_csv(csv_path)
    summary = summarise(rows, system_filter=args.system)
    print_summary(summary, sort_by=args.sort)


if __name__ == '__main__':
    main()
