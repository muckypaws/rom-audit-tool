#!/usr/bin/env python3
"""
ROM Audit Tool — Duplicate ROM Finder

Scans ROM folders and the audit CSV to find ROMs that appear in
multiple system folders. Optionally computes MD5 checksums to
distinguish identical files (copy errors) from same-named files
with different content (legitimate multi-platform releases).

Two detection modes:
    --csv    Find ROMs that appear in multiple systems in the CSV
    --files  Scan ROM directories and compare filenames across systems

Add --checksum to compute MD5s for file-mode duplicates so you can
see whether the files are truly identical or just share a name.

Usage:
    python3 scripts/duplicates.py --csv
    python3 scripts/duplicates.py --files
    python3 scripts/duplicates.py --files --checksum
    python3 scripts/duplicates.py --files --checksum --paths
    python3 scripts/duplicates.py --csv --files
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import sys
import csv
import glob
import hashlib
import argparse
from collections import defaultdict


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

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

ROM_EXTENSIONS = [
    '*.7z', '*.zip', '*.chd', '*.iso', '*.cue', '*.rom',
    '*.n64', '*.z64', '*.v64', '*.sfc', '*.smc', '*.smd',
    '*.md', '*.gb', '*.gba', '*.gbc', '*.nes', '*.fds',
    '*.nds', '*.pce', '*.lnx', '*.ngp', '*.ngc', '*.ws',
    '*.wsc', '*.vb', '*.a26', '*.a52', '*.a78', '*.col',
    '*.int', '*.vec', '*.sg', '*.sgg', '*.gg', '*.adf',
    '*.ipf', '*.d64', '*.tap', '*.tzx', '*.dsk', '*.pbp',
    '*.cso', '*.bin', '*.img', '*.xex', '*.atr', '*.prg',
    '*.crt', '*.lha', '*.lzh', '*.mgw',
]

SKIP_SYSTEMS = {
    'ports', 'kodi', 'moonlight', 'prboom', 'scummvm',
    'odcommander', 'devilutionx', 'screenshots', 'tmp', 'daphne',
}

# Known system alias pairs — same ROM collection in two folder names
KNOWN_ALIASES = {
    frozenset(['genesis', 'megadrive']),
    frozenset(['famicom', 'nes']),
    frozenset(['mame-libretro', 'arcade']),
    frozenset(['fba', 'fbneo']),
}

# ANSI colours
GREEN  = '\033[32m'
RED    = '\033[31m'
YELLOW = '\033[33m'
CYAN   = '\033[36m'
RESET  = '\033[0m'
BOLD   = '\033[1m'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def compute_md5(path: str, chunk: int = 1024 * 1024) -> str:
    """
    Compute the MD5 checksum of a file.

    Reads in 1 MB chunks to handle large ROM files without loading
    them entirely into memory.

    Returns:
        Hex digest string, or 'ERROR' if the file cannot be read.
    """
    h = hashlib.md5()
    try:
        with open(path, 'rb') as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()
    except Exception:
        return 'ERROR'


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_duplicates_in_csv(csv_path: str) -> dict:
    """
    Find ROM filenames that appear in more than one system in the CSV.

    Returns:
        {romname: [(system, status), ...]} for entries with > 1 system.
    """
    by_name: dict = defaultdict(list)
    try:
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                romname = row.get('rom', '').strip()
                system  = row.get('system', '').strip()
                status  = row.get('status', '').strip()
                if romname and system:
                    by_name[romname].append((system, status))
    except Exception as e:
        print(f'ERROR: Could not read {csv_path}: {e}')
        sys.exit(1)

    return {
        name: entries
        for name, entries in by_name.items()
        if len(entries) > 1
    }


def find_duplicates_in_files(roms_path: str) -> dict:
    """
    Scan ROM directories for filenames present in multiple systems.

    Returns:
        {romname: [(system, full_path), ...]} for files with > 1 system.
    """
    by_name: dict = defaultdict(list)

    if not os.path.isdir(roms_path):
        print(f'ERROR: ROM path not found: {roms_path}')
        sys.exit(1)

    systems = sorted([
        d for d in os.listdir(roms_path)
        if os.path.isdir(os.path.join(roms_path, d))
        and not d.startswith('.')
        and d not in SKIP_SYSTEMS
    ])

    for system in systems:
        system_path = os.path.join(roms_path, system)
        for ext in ROM_EXTENSIONS:
            for rom_path in glob.glob(os.path.join(system_path, ext)):
                romname = os.path.basename(rom_path)
                by_name[romname].append((system, rom_path))

    return {
        name: entries
        for name, entries in by_name.items()
        if len(entries) > 1
    }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_duplicates(
    duplicates: dict,
    mode: str,
    show_paths: bool = False,
    show_checksum: bool = False
) -> None:
    """Print duplicate findings, optionally with MD5 comparison."""

    if not duplicates:
        print(f'\n  No duplicates found ({mode} mode).')
        return

    # Group by which systems are involved
    by_systems: dict = defaultdict(list)
    for romname, entries in sorted(duplicates.items()):
        systems_key = ' + '.join(sorted(set(e[0] for e in entries)))
        by_systems[systems_key].append((romname, entries))

    identical_count   = 0
    different_count   = 0
    uncheckable_count = 0

    print(f'\n{BOLD}Duplicates — {mode} mode '
          f'({len(duplicates)} ROM(s)){RESET}')
    print('─' * 60)

    for systems_key in sorted(by_systems.keys()):
        roms = by_systems[systems_key]
        print(f'\n  {YELLOW}[{systems_key}]{RESET}  {len(roms)} duplicate(s)')

        for romname, entries in roms:
            is_file_mode = (
                len(entries[0]) > 1 and
                isinstance(entries[0][1], str) and
                entries[0][1].startswith('/')
            )

            if show_checksum and is_file_mode:
                # Compute MD5 for each copy
                checksums = {}
                for system, path in entries:
                    print(f'      Computing MD5: {system}/{romname}...',
                          end='\r')
                    checksums[path] = compute_md5(path)

                all_same = (len(set(checksums.values())) == 1 and
                            'ERROR' not in checksums.values())
                has_error = 'ERROR' in checksums.values()

                if has_error:
                    marker = f'{YELLOW}[unreadable]{RESET}'
                    uncheckable_count += 1
                elif all_same:
                    marker = f'{RED}[IDENTICAL — same file, likely copy error]{RESET}'
                    identical_count += 1
                else:
                    marker = f'{GREEN}[different content — legitimate multi-platform]{RESET}'
                    different_count += 1

                print(f'    {romname}  {marker}')
                for system, path in entries:
                    md5 = checksums[path]
                    short = md5[:12] + '...' if md5 != 'ERROR' else 'ERROR'
                    size_mb = os.path.getsize(path) / 1024 / 1024
                    print(f'      [{system}]  {short}  '
                          f'({size_mb:.1f} MB)')
                    if show_paths:
                        print(f'        {path}')

            else:
                print(f'    {romname}')
                if show_paths and is_file_mode:
                    for system, path in entries:
                        print(f'      [{system}]  {path}')
                elif not is_file_mode:
                    for system, status in entries:
                        print(f'      [{system}]  status: {status}')

    # Checksum summary
    if show_checksum and (identical_count + different_count > 0):
        print(f'\n{"─" * 60}')
        print(f'  Checksum summary:')
        if identical_count:
            print(f'  {RED}{identical_count} identical{RESET}  '
                  f'— same file in multiple folders, safe to delete one copy')
        if different_count:
            print(f'  {GREEN}{different_count} different{RESET} '
                  f'— same name, different content (legitimate multi-platform)')
        if uncheckable_count:
            print(f'  {YELLOW}{uncheckable_count} unreadable{RESET}')


def suggest_resolution(duplicates: dict) -> None:
    """Flag known system alias pairs."""
    aliased = []
    for romname, entries in duplicates.items():
        systems = frozenset(e[0] for e in entries)
        for alias_pair in KNOWN_ALIASES:
            if alias_pair.issubset(systems):
                aliased.append((romname, alias_pair))
                break

    if aliased:
        pairs = sorted(set(
            ' ↔ '.join(sorted(p)) for _, p in aliased
        ))
        print(f'\n  {YELLOW}Note:{RESET} {len(aliased)} ROM(s) are in '
              f'known alias folder pairs ({", ".join(pairs)}).')
        print('  These folders contain the same ROM set under different names.')
        print('  Consider consolidating to one folder.')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='ROM Audit Tool — Find duplicate ROMs across system folders'
    )
    parser.add_argument(
        '--csv', nargs='?', const=True, metavar='PATH',
        help='Find duplicates in audit CSV (auto-detected if no path given)'
    )
    parser.add_argument(
        '--files', action='store_true',
        help='Scan ROM directories for duplicate filenames'
    )
    parser.add_argument(
        '--checksum', action='store_true',
        help='Compute MD5 for each duplicate file to distinguish '
             'identical copies from same-named multi-platform releases. '
             'Requires --files. Large ROM collections may take a few minutes.'
    )
    parser.add_argument(
        '--roms', metavar='PATH',
        help='ROM base directory (auto-detected if not specified)'
    )
    parser.add_argument(
        '--paths', action='store_true',
        help='Show full file paths for each duplicate'
    )
    args = parser.parse_args()

    if not args.csv and not args.files:
        parser.print_help()
        print('\nERROR: Specify --csv and/or --files')
        sys.exit(1)

    if args.checksum and not args.files:
        print('ERROR: --checksum requires --files')
        sys.exit(1)

    csv_dupes  = {}
    file_dupes = {}

    if args.csv:
        csv_path = args.csv if isinstance(args.csv, str) else find_csv()
        if not csv_path or not os.path.exists(csv_path):
            print('ERROR: Could not find rom_audit.csv')
            print('Specify with: --csv /path/to/rom_audit.csv')
            sys.exit(1)
        print(f'CSV: {csv_path}')
        csv_dupes = find_duplicates_in_csv(csv_path)
        print_duplicates(csv_dupes, 'CSV')

    if args.files:
        roms_path = args.roms or find_roms_path()
        if not roms_path:
            print('ERROR: Could not find ROMs directory')
            print('Specify with: --roms /path/to/roms')
            sys.exit(1)
        print(f'ROMs: {roms_path}')
        if args.checksum:
            print('Computing MD5 checksums — this may take a few minutes '
                  'for large files...')
        file_dupes = find_duplicates_in_files(roms_path)
        print_duplicates(
            file_dupes, 'files',
            show_paths=args.paths,
            show_checksum=args.checksum
        )

    all_dupes = {**csv_dupes, **file_dupes}
    if all_dupes:
        suggest_resolution(all_dupes)

    total = len(set(list(csv_dupes.keys()) + list(file_dupes.keys())))
    print(f'\n  Total: {total} duplicate ROM filename(s) found\n')


if __name__ == '__main__':
    main()
