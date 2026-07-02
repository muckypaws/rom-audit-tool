#!/usr/bin/env python3
"""
ROM Audit Tool — Clear System Overrides (Batocera)

Removes every override entry for one or more specified systems from
batocera.conf — both the global default (system.core=X,
system.emulator=X) and every per-game entry (system["romname"].*=X),
regardless of key. Also picks up any stale #ROMAUDIT# suspended marker
left behind by an abruptly killed run, since that's still technically
an entry for the system even while temporarily disabled.

This is Batocera-specific. Recalbox uses per-ROM sidecar files rather
than a single shared conf, and RetroPie uses emulators.cfg with a
different key format — neither fits this script's "one shared file,
many systems" model. Adapting this approach to those platforms would
need a different implementation, not a flag on this one.

Why this exists: autofix can accept FBNeo as a "fix" using exactly the
same shallow launch-and-check-for-errors criteria everything else is
judged by — and FBNeo's known behaviour on a bad dump is to silently
grey-screen rather than log anything, making a masked failure
indistinguishable from a genuine pass. Existing per-game entries
written before this was understood (or before autofix was made to
flag it) carry no marker distinguishing a validated fix from a masked
failure — the only way to know is to clear them and retest under
current, more careful logic. See CHANGELOG / CLAUDE.md for the full
account (UNVERIFIED_CORES in modules/common/autofix.py).

Usage:
    python3 scripts/clear_system_overrides.py --system mame
    python3 scripts/clear_system_overrides.py --system mame fbneo
    python3 scripts/clear_system_overrides.py --system mame --remove
    python3 scripts/clear_system_overrides.py --system mame --remove --conf /path/to/batocera.conf
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import re
import sys
import shutil
import argparse
from datetime import datetime


DEFAULT_CONF_PATHS = [
    '/userdata/system/batocera.conf',
    'batocera.conf',
]

RED    = '\033[31m'
YELLOW = '\033[33m'
GREEN  = '\033[32m'
CYAN   = '\033[36m'
BOLD   = '\033[1m'
RESET  = '\033[0m'


def find_conf() -> str:
    for path in DEFAULT_CONF_PATHS:
        if os.path.exists(path):
            return path
    return None


def build_patterns(system: str) -> tuple:
    """
    Returns (global_pattern, stale_global_pattern, pergame_pattern)
    for one system name, all anchored to avoid partial-name collisions
    (e.g. 'mame' must not match a hypothetical 'mame2' system).
    """
    escaped = re.escape(system)
    global_pattern = re.compile(rf'^{escaped}\.(core|emulator)=.*$')
    stale_pattern  = re.compile(rf'^#ROMAUDIT#{escaped}\.(core|emulator)=.*$')
    pergame_pattern = re.compile(rf'^{escaped}\["[^"]*"\]\.[a-zA-Z_.]+=.*$')
    return global_pattern, stale_pattern, pergame_pattern


def extract_romname(line: str, system: str) -> str:
    """Pull the romname out of a system["romname"].key=value line."""
    m = re.match(rf'^{re.escape(system)}\["([^"]*)"\]', line)
    return m.group(1) if m else '?'


def main():
    parser = argparse.ArgumentParser(
        description='ROM Audit Tool — Clear System Overrides (Batocera)'
    )
    parser.add_argument(
        '--system', nargs='+', required=True, metavar='SYSTEM',
        help='One or more system names to clear, e.g. --system mame fbneo'
    )
    parser.add_argument(
        '--conf', help='Path to batocera.conf (auto-detected if not specified)'
    )
    parser.add_argument(
        '--remove', action='store_true',
        help='Actually remove the matched entries (default: report only)'
    )
    parser.add_argument(
        '--show-all', action='store_true',
        help='List every matched romname rather than truncating to 20'
    )
    args = parser.parse_args()

    conf_path = args.conf or find_conf()
    if not conf_path or not os.path.exists(conf_path):
        print(f"{RED}ERROR: Could not find batocera.conf. Specify with --conf{RESET}")
        sys.exit(1)

    print(f"Config: {conf_path}")
    print(f"Systems: {', '.join(args.system)}\n")

    with open(conf_path, 'r') as f:
        lines = f.readlines()

    kept_lines = []
    summary = {}   # system -> {'global': [...], 'stale': [...], 'pergame': [...]}
    for system in args.system:
        summary[system] = {'global': [], 'stale': [], 'pergame': []}

    patterns = {s: build_patterns(s) for s in args.system}

    for line in lines:
        stripped = line.strip()
        matched = False
        for system, (global_p, stale_p, pergame_p) in patterns.items():
            if global_p.match(stripped):
                summary[system]['global'].append(stripped)
                matched = True
                break
            if stale_p.match(stripped):
                summary[system]['stale'].append(stripped)
                matched = True
                break
            if pergame_p.match(stripped):
                romname = extract_romname(stripped, system)
                summary[system]['pergame'].append(romname)
                matched = True
                break
        if not matched:
            kept_lines.append(line)

    total_matched = sum(
        len(v['global']) + len(v['stale']) + len(v['pergame'])
        for v in summary.values()
    )

    if total_matched == 0:
        print(f"{GREEN}No matching entries found for the specified system(s) — "
              f"nothing to do.{RESET}")
        return

    for system, found in summary.items():
        print(f"{BOLD}[{system}]{RESET}")
        if found['global']:
            print(f"  {YELLOW}Global override(s):{RESET}")
            for line in found['global']:
                print(f"    {line}")
        if found['stale']:
            print(f"  {CYAN}Stale suspended marker(s) (#ROMAUDIT#):{RESET}")
            for line in found['stale']:
                print(f"    {line}")
        if found['pergame']:
            unique_roms = list(dict.fromkeys(found['pergame']))  # dedupe, preserve order
            line_count = len(found['pergame'])
            rom_count = len(unique_roms)
            print(f"  {YELLOW}Per-game entries: {line_count} line(s) "
                  f"across {rom_count} ROM(s){RESET}")
            shown = unique_roms if args.show_all else unique_roms[:20]
            for romname in shown:
                print(f"    {romname}")
            if not args.show_all and rom_count > 20:
                print(f"    ... and {rom_count - 20} more ROM(s) (use --show-all to list them)")
        print()

    if not args.remove:
        print(f"Dry run — no changes made. {total_matched} entr"
              f"{'y' if total_matched == 1 else 'ies'} would be removed. "
              f"Re-run with --remove to apply.")
        return

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = f"{conf_path}.bak.{timestamp}"
    try:
        shutil.copy2(conf_path, backup_path)
        print(f"Backed up to: {backup_path}")
    except Exception as e:
        print(f"{RED}ERROR: Could not create backup, aborting: {e}{RESET}")
        sys.exit(1)

    try:
        with open(conf_path, 'w') as f:
            f.writelines(kept_lines)
    except Exception as e:
        print(f"{RED}ERROR: Could not write {conf_path}: {e}{RESET}")
        print(f"Original is safe at: {backup_path}")
        sys.exit(1)

    print(f"{GREEN}Removed {total_matched} entr"
          f"{'y' if total_matched == 1 else 'ies'} from {conf_path}{RESET}")
    print(f"Backup retained at: {backup_path}")
    print()
    print("Note: any per-game entries removed here have CSV rows that")
    print("are now stale relative to the config. Re-run with --recheck")
    print("--autofix on the affected system(s) to get fresh, current results.")


if __name__ == '__main__':
    main()
