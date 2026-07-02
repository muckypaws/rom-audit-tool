#!/usr/bin/env python3
"""
ROM Audit Tool — Compute Checksums

Walks the existing CSV and computes a checksum for each ROM's file on
disk, writing it into the checksum column — without launching, testing,
or otherwise touching the ROM itself. Pure file-hashing pass, much
faster than a real audit since nothing gets executed.

By default, only rows with an empty checksum are processed (so a
re-run after adding new ROMs only does new work). Use --force to
recompute every row regardless — useful for periodically re-verifying
an entire collection's integrity, since a changed hash on a ROM you
didn't touch is a real, concrete signal something happened to it
outside this tool (see the SpyHunter case: a ROM that worked, then
didn't, traced to its file content genuinely changing between audits
— not anything this tool did).

Usage:
    python3 scripts/compute_checksums.py                       # all systems, missing only
    python3 scripts/compute_checksums.py --system mame         # one system, missing only
    python3 scripts/compute_checksums.py --system mame snes     # multiple systems
    python3 scripts/compute_checksums.py --force                # recompute everything
    python3 scripts/compute_checksums.py --algorithm sha1        # use sha1 instead of md5
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import csv
import sys
import time
import hashlib
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

GREEN  = '\033[32m'
YELLOW = '\033[33m'
RED    = '\033[31m'
CYAN   = '\033[36m'
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


def compute_checksum(rom_path: str, algorithm: str) -> str:
    """
    Same approach as the main tool's own compute_checksum(): chunked
    reads so large CD/CHD images don't need loading fully into memory.
    Returns '' on any failure (missing file, permission error, etc.)
    rather than raising — a checksum pass should never crash on one
    bad file partway through a large collection.
    """
    try:
        h = hashlib.new(algorithm)
        with open(rom_path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return f"{algorithm}:{h.hexdigest()}"
    except Exception:
        return ''


def format_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{minutes / 60:.1f}h"


def main():
    parser = argparse.ArgumentParser(
        description='ROM Audit Tool — Compute Checksums (no testing, file-hash only)'
    )
    parser.add_argument(
        '--csv', help='Path to rom_audit.csv (auto-detected if not specified)'
    )
    parser.add_argument(
        '--roms', help='ROM base directory (auto-detected if not specified)'
    )
    parser.add_argument(
        '--system', nargs='+', metavar='SYSTEM',
        help='Limit to one or more systems (default: every system in the CSV)'
    )
    parser.add_argument(
        '--algorithm', default='md5', choices=['md5', 'sha1'],
        help='Hash algorithm to use (default: md5)'
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Recompute every row, including ones that already have a '
             'checksum (default: only rows with an empty checksum)'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help="Report what would be computed without writing the CSV"
    )
    args = parser.parse_args()

    csv_path = args.csv or find_csv()
    if not csv_path or not os.path.exists(csv_path):
        print(f"{RED}ERROR: Could not find rom_audit.csv. Specify with --csv{RESET}")
        sys.exit(1)

    roms_path = args.roms or find_roms_path()
    if not roms_path or not os.path.isdir(roms_path):
        print(f"{RED}ERROR: Could not find ROM directory. Specify with --roms{RESET}")
        sys.exit(1)

    print(f"CSV:       {csv_path}")
    print(f"ROMs:      {roms_path}")
    print(f"Algorithm: {args.algorithm}")
    if args.system:
        print(f"Systems:   {', '.join(args.system)}")
    print()

    with open(csv_path, 'r', newline='') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if 'checksum' not in (fieldnames or []):
        print(f"{RED}ERROR: CSV has no 'checksum' column.{RESET}")
        sys.exit(1)

    system_filter = set(args.system) if args.system else None

    # Build the work list first so progress/ETA is meaningful
    to_process = []
    for idx, row in enumerate(rows):
        system = row.get('system', '')
        if system_filter and system not in system_filter:
            continue
        if system == 'ports':
            # Ports use display names from gamelist.xml, not direct
            # file paths the way standard systems do — skip, same
            # reasoning as prune_orphaned_entries.py.
            continue
        existing = (row.get('checksum') or '').strip()
        if existing and not args.force:
            continue
        to_process.append(idx)

    total = len(to_process)
    if total == 0:
        print(f"{GREEN}Nothing to do — no matching rows need a checksum. "
              f"Use --force to recompute existing ones too.{RESET}")
        return

    print(f"{total} ROM(s) to hash...\n")

    computed = 0
    missing = 0
    start_time = time.time()

    for n, idx in enumerate(to_process, 1):
        row = rows[idx]
        system = row.get('system', '')
        romname = row.get('rom', '')
        rom_path = os.path.join(roms_path, system, romname)

        if not os.path.exists(rom_path):
            print(f"  [{n}/{total}] {YELLOW}MISSING{RESET} [{system}] {romname}")
            missing += 1
            continue

        checksum = compute_checksum(rom_path, args.algorithm)
        if checksum:
            rows[idx]['checksum'] = checksum
            computed += 1
        else:
            print(f"  [{n}/{total}] {RED}FAILED{RESET}  [{system}] {romname}")
            continue

        if n % 25 == 0 or n == total:
            elapsed = time.time() - start_time
            rate = n / elapsed if elapsed > 0 else 0
            remaining = (total - n) / rate if rate > 0 else 0
            print(f"  [{n}/{total}] {CYAN}{system}{RESET}/{romname} "
                  f"— ETA {format_eta(remaining)}")

    print()
    print(f"Computed: {computed}   Missing: {missing}   "
          f"Total elapsed: {format_eta(time.time() - start_time)}")

    if args.dry_run:
        print(f"\n{YELLOW}Dry run — CSV not written.{RESET}")
        return

    tmp_path = csv_path + '.tmp'
    with open(tmp_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp_path, csv_path)

    print(f"{GREEN}CSV updated: {csv_path}{RESET}")


if __name__ == '__main__':
    main()
