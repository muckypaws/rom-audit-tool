#!/usr/bin/env python3
"""
ROM Audit Tool — Compare Two Audit Runs

Compares two rom_audit.csv files and reports what changed between them.
Useful for tracking progress after fixes, core updates or ROM set changes.

Categories reported:
    Improved         — was ERROR/GENUINE ERROR/TIMEOUT, now OK/FIXED
    Regressed        — was OK/FIXED, now ERROR/GENUINE ERROR/TIMEOUT
    Fixed            — status changed to FIXED (autofix succeeded)
    New              — ROM present in new CSV but not in old
    Removed          — ROM present in old CSV but not in new
    Unchanged        — status identical in both
    ROM replaced     — same status, different checksum (file swapped)
    Suspicious fix   — status improved AND checksum changed

Usage:
    python3 scripts/compare.py old.csv new.csv
    python3 scripts/compare.py old.csv new.csv --system arcade
    python3 scripts/compare.py old.csv new.csv --improved
    python3 scripts/compare.py old.csv new.csv --regressed
    python3 scripts/compare.py old.csv new.csv --checksum
    python3 scripts/compare.py old.csv new.csv --summary
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import csv
import sys
import argparse
from collections import defaultdict


OK_STATUSES   = {'OK', 'FIXED'}
PART_STATUSES = {'IMPERFECT'}   # Playable but not arcade-perfect
BAD_STATUSES  = {'ERROR', 'GENUINE ERROR', 'TIMEOUT', 'LAUNCHED',
                 'MISSING BIOS', 'MISSING CORE', 'NO COMBINATIONS'}


def load_csv(path: str) -> dict[str, dict]:
    """Load CSV into dict keyed by system:romname."""
    rows = {}
    try:
        with open(path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = f"{row.get('system','').strip()}:{row.get('rom','').strip()}"
                rows[key] = row
    except Exception as e:
        print(f"ERROR: Could not read {path}: {e}")
        sys.exit(1)
    return rows


def _checksum(row: dict | None) -> str:
    """Return normalised checksum string, or '' if absent/None."""
    if row is None:
        return ''
    return row.get('checksum', '').strip()


def compare(
    old: dict,
    new: dict,
    system_filter: str = None
) -> dict:
    """
    Compare two CSV dicts and categorise every ROM.

    Returns a dict with keys:
        improved, regressed, fixed, new, removed, unchanged,
        status_changed, rom_replaced, suspicious_fix
    Each value is a list of (key, old_row_or_None, new_row_or_None).
    """
    results = defaultdict(list)

    all_keys = set(old.keys()) | set(new.keys())

    for key in sorted(all_keys):
        system = key.split(':')[0]
        if system_filter and system.lower() != system_filter.lower():
            continue

        old_row = old.get(key)
        new_row = new.get(key)

        if old_row is None:
            results['new'].append((key, None, new_row))
            continue

        if new_row is None:
            results['removed'].append((key, old_row, None))
            continue

        old_status = old_row.get('status', '').strip()
        new_status = new_row.get('status', '').strip()
        old_chk    = _checksum(old_row)
        new_chk    = _checksum(new_row)

        # Checksum comparison — only meaningful when both runs recorded one
        chk_changed = (
            bool(old_chk) and bool(new_chk) and old_chk != new_chk
        )

        was_bad  = old_status in BAD_STATUSES
        was_part = old_status in PART_STATUSES
        now_ok   = new_status in OK_STATUSES
        was_ok   = old_status in OK_STATUSES
        now_bad  = new_status in BAD_STATUSES

        # IMPERFECT→OK counts as improved; OK→IMPERFECT as regressed
        if old_status == new_status:
            if chk_changed:
                # Same result, different file — ROM was silently swapped
                results['rom_replaced'].append((key, old_row, new_row))
            else:
                results['unchanged'].append((key, old_row, new_row))
            continue

        # Status changed
        if was_bad and now_ok:
            if chk_changed:
                # Improvement coincides with file change — might be a
                # ROM replacement fix rather than a config/core fix
                results['suspicious_fix'].append((key, old_row, new_row))
            else:
                results['improved'].append((key, old_row, new_row))
        elif was_ok and now_bad:
            results['regressed'].append((key, old_row, new_row))
        elif new_status == 'FIXED':
            results['fixed'].append((key, old_row, new_row))
        else:
            results['status_changed'].append((key, old_row, new_row))

    return dict(results)


def fmt_key(key: str) -> str:
    """Format 'system:romname' as '[system] romname'."""
    parts = key.split(':', 1)
    if len(parts) == 2:
        return f"[{parts[0]}] {parts[1]}"
    return key


def _chk_detail(old_row: dict, new_row: dict) -> str:
    """Return a short checksum-change string for display."""
    old_chk = _checksum(old_row)
    new_chk = _checksum(new_row)
    if old_chk and new_chk and old_chk != new_chk:
        # Show just the hash portion after the algorithm prefix
        def _short(c: str) -> str:
            return c.split(':', 1)[-1][:12] if ':' in c else c[:12]
        return f"  [{_short(old_chk)}… → {_short(new_chk)}…]"
    return ''


def print_section(
    title: str,
    entries: list,
    colour: str,
    show_detail: bool = True,
    show_checksum: bool = False
) -> None:
    reset = '\033[0m'
    bold  = '\033[1m'

    print(f"\n{bold}{colour}{title} ({len(entries)}){reset}")
    print(f"{colour}{'─' * (len(title) + 8)}{reset}")

    if not entries:
        print("  (none)")
        return

    by_system = defaultdict(list)
    for key, old_row, new_row in entries:
        system = key.split(':')[0]
        by_system[system].append((key, old_row, new_row))

    for system in sorted(by_system.keys()):
        sys_entries = by_system[system]
        print(f"\n  [{system}]  {len(sys_entries)} ROM(s)")
        for key, old_row, new_row in sys_entries:
            romname = key.split(':', 1)[1] if ':' in key else key
            chk_str = _chk_detail(old_row, new_row) if show_checksum else ''
            if show_detail and old_row and new_row:
                old_s = old_row.get('status', '?')
                new_s = new_row.get('status', '?')
                notes = new_row.get('notes', '').strip()
                detail = f"  {old_s} → {new_s}"
                if notes:
                    detail += f'  "{notes}"'
                print(f"    {romname}{detail}{chk_str}")
            elif show_detail and new_row is None:
                old_s = old_row.get('status', '?')
                print(f"    {romname}  (was {old_s}){chk_str}")
            elif show_detail and old_row is None:
                new_s = new_row.get('status', '?')
                print(f"    {romname}  (now {new_s}){chk_str}")
            else:
                print(f"    {romname}{chk_str}")


def print_summary(
    results: dict,
    old: dict,
    new: dict,
    show_checksum: bool = False
) -> None:
    """Print a compact summary table."""
    improved     = len(results.get('improved', []))
    regressed    = len(results.get('regressed', []))
    fixed        = len(results.get('fixed', []))
    new_roms     = len(results.get('new', []))
    removed      = len(results.get('removed', []))
    changed      = len(results.get('status_changed', []))
    unchanged    = len(results.get('unchanged', []))
    replaced     = len(results.get('rom_replaced', []))
    suspicious   = len(results.get('suspicious_fix', []))

    green  = '\033[32m'
    red    = '\033[31m'
    yellow = '\033[33m'
    cyan   = '\033[36m'
    reset  = '\033[0m'
    bold   = '\033[1m'

    total_old = len(old)
    total_new = len(new)
    old_ok    = sum(1 for r in old.values() if r.get('status') in OK_STATUSES)
    new_ok    = sum(1 for r in new.values() if r.get('status') in OK_STATUSES)
    old_pct   = (old_ok / total_old * 100) if total_old else 0
    new_pct   = (new_ok / total_new * 100) if total_new else 0

    print(f"\n{bold}Summary{reset}")
    print("─" * 45)
    print(f"  {'Total ROMs:':<30} {total_old:>6} → {total_new:>6}")
    print(f"  {'OK + Fixed:':<30} "
          f"{green}{old_ok:>6}{reset} → "
          f"{green}{new_ok:>6}{reset}  "
          f"({old_pct:.1f}% → {new_pct:.1f}%)")
    print()
    print(f"  {green}{'Improved (bad→OK):':<30} {improved:>6}{reset}")
    print(f"  {green}{'Fixed (autofix):':<30} {fixed:>6}{reset}")
    print(f"  {red}{'Regressed (OK→bad):':<30} {regressed:>6}{reset}")
    print(f"  {yellow}{'New ROMs:':<30} {new_roms:>6}{reset}")
    print(f"  {yellow}{'Removed ROMs:':<30} {removed:>6}{reset}")
    print(f"  {'Other status change:':<30} {changed:>6}")
    print(f"  {'Unchanged:':<30} {unchanged:>6}")

    if show_checksum:
        print()
        print(f"  {cyan}{'ROM replaced (same status):':<30} {replaced:>6}{reset}")
        print(f"  {yellow}{'Suspicious fix (ROM+status):':<30} {suspicious:>6}{reset}")

    # Note if checksums were not recorded
    has_old_chk = any(_checksum(r) for r in old.values())
    has_new_chk = any(_checksum(r) for r in new.values())
    if show_checksum and not (has_old_chk and has_new_chk):
        missing = []
        if not has_old_chk:
            missing.append('old CSV')
        if not has_new_chk:
            missing.append('new CSV')
        print(f"\n  Note: no checksums recorded in {' or '.join(missing)} "
              f"— use --checksum md5 during audit for ROM tracking.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='ROM Audit Tool — Compare two audit runs'
    )
    parser.add_argument('old_csv', help='Earlier audit CSV')
    parser.add_argument('new_csv', help='More recent audit CSV')
    parser.add_argument(
        '--system', metavar='SYSTEM',
        help='Filter comparison to a single system'
    )
    parser.add_argument(
        '--improved', action='store_true',
        help='Show only improved ROMs (bad→OK)'
    )
    parser.add_argument(
        '--regressed', action='store_true',
        help='Show only regressed ROMs (OK→bad)'
    )
    parser.add_argument(
        '--new', action='store_true', dest='show_new',
        help='Show only newly added ROMs'
    )
    parser.add_argument(
        '--removed', action='store_true',
        help='Show only removed ROMs'
    )
    parser.add_argument(
        '--checksum', action='store_true',
        help='Show ROMs where checksum changed between runs — '
             'indicates a ROM file was replaced. Highlights suspicious '
             'fixes where status improved AND file changed.'
    )
    parser.add_argument(
        '--summary', action='store_true',
        help='Show summary table only'
    )
    args = parser.parse_args()

    for path in (args.old_csv, args.new_csv):
        if not os.path.exists(path):
            print(f"ERROR: File not found: {path}")
            sys.exit(1)

    print(f"\nOld: {args.old_csv}")
    print(f"New: {args.new_csv}")
    if args.system:
        print(f"System filter: [{args.system}]")

    old = load_csv(args.old_csv)
    new = load_csv(args.new_csv)

    results = compare(old, new, system_filter=args.system)

    show_all = not any([
        args.improved, args.regressed, args.show_new,
        args.removed, args.checksum, args.summary
    ])

    if args.summary or show_all:
        print_summary(results, old, new, show_checksum=args.checksum or show_all)

    if not args.summary:
        if args.improved or show_all:
            print_section(
                'Improved (bad → OK)', results.get('improved', []),
                '\033[32m', show_checksum=args.checksum
            )
        if args.regressed or show_all:
            print_section(
                'Regressed (OK → bad)', results.get('regressed', []),
                '\033[31m', show_checksum=args.checksum
            )
        if show_all:
            print_section(
                'Fixed by autofix', results.get('fixed', []),
                '\033[32m', show_checksum=args.checksum
            )
            print_section(
                'Other status change', results.get('status_changed', []),
                '\033[33m', show_checksum=args.checksum
            )
        if args.show_new or show_all:
            print_section(
                'New ROMs', results.get('new', []),
                '\033[36m', show_checksum=args.checksum
            )
        if args.removed or show_all:
            print_section(
                'Removed ROMs', results.get('removed', []),
                '\033[35m', show_checksum=args.checksum
            )
        if args.checksum or show_all:
            print_section(
                'ROM replaced (same status, different checksum)',
                results.get('rom_replaced', []),
                '\033[36m', show_checksum=True
            )
            print_section(
                'Suspicious fix (status improved AND checksum changed)',
                results.get('suspicious_fix', []),
                '\033[33m', show_checksum=True
            )

    print()


if __name__ == '__main__':
    main()
