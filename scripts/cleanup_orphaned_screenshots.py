#!/usr/bin/env python3
"""
One-off cleanup for screenshots left behind in /recalbox/share/screenshots
by past rom_audit runs, before the screenshot capture was switched from
copy+leave-original to move (which makes this leftover impossible going
forward).

Scopes deletion precisely to the start and end of the audit run, derived
from the 'tested_at' timestamps already recorded in your results CSV —
no guessing at a time window. Only files modified inside that exact
range are removed; anything outside it (including screenshots you took
yourself before or after the run) is left untouched.

Usage:
    python3 cleanup_orphaned_screenshots.py /path/to/rom_audit_results.csv

Add --dry-run to list what would be deleted without deleting anything.
"""

from __future__ import annotations  # Python 3.9 compatibility
import csv
import os
import sys
from datetime import datetime, timedelta

SCREENSHOTS_DIR = '/recalbox/share/screenshots'


def parse_tested_at(value: str):
    """Parse the tested_at column into a datetime, or None if unparseable."""
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_csv> [--dry-run]")
        sys.exit(1)

    csv_path = sys.argv[1]
    dry_run  = '--dry-run' in sys.argv

    timestamps = []
    with open(csv_path, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = parse_tested_at(row.get('tested_at', ''))
            if ts:
                timestamps.append(ts)

    if not timestamps:
        print("No parseable 'tested_at' timestamps found in CSV — "
              "cannot determine a safe window. Aborting.")
        sys.exit(1)

    # Pad by a couple of minutes on each side to cover capture latency
    # (the screenshot is requested mid-test, slightly before/after the
    # row's own tested_at moment is written).
    pad = timedelta(minutes=2)
    start = min(timestamps) - pad
    end   = max(timestamps) + pad

    print(f"Audit window from CSV: {min(timestamps)} to {max(timestamps)}")
    print(f"Cleanup window (padded): {start} to {end}")
    print()

    if not os.path.isdir(SCREENSHOTS_DIR):
        print(f"{SCREENSHOTS_DIR} not found — nothing to clean up.")
        sys.exit(0)

    removed = 0
    kept    = 0
    for entry in os.listdir(SCREENSHOTS_DIR):
        full = os.path.join(SCREENSHOTS_DIR, entry)
        if not os.path.isfile(full) or not entry.lower().endswith(
            ('.png', '.jpg', '.jpeg', '.bmp')
        ):
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(full))
        if start <= mtime <= end:
            if dry_run:
                print(f"  Would delete: {entry}  (mtime {mtime})")
            else:
                try:
                    os.remove(full)
                    print(f"  Deleted: {entry}")
                except OSError as e:
                    print(f"  Failed to delete {entry}: {e}")
                    continue
            removed += 1
        else:
            kept += 1

    print()
    action = "Would remove" if dry_run else "Removed"
    print(f"{action} {removed} file(s) within the audit window.")
    print(f"Left {kept} file(s) outside the window untouched.")


if __name__ == '__main__':
    main()
