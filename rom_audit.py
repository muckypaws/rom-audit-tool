#!/usr/bin/env python3
"""
ROM Audit Tool
==============
A utility for testing ROM compatibility across emulation platforms.
Launches each ROM via the platform's emulator launcher, detects success
or failure, and records results to a CSV file for later review.
Error logs are automatically archived under a structured directory for
offline diagnosis.

Supported platforms (auto-detected):
    Batocera v38 / v43+
    RetroPie 4.x+ (Bullseye or later, direct console access required)
    Recalbox 9.x / 10.x

Adding a new platform:
    1. Create modules/platforms/<platform>.py subclassing Platform
    2. Add a detection condition in modules/common/detection.py
    3. No other files need to change

Basic usage:
    python3 rom_audit.py                         Audit all systems
    python3 rom_audit.py --system mame           Audit one system
    python3 rom_audit.py --system ports          Audit ports only
    python3 rom_audit.py --test pacman.7z        Test a single ROM
    python3 rom_audit.py --recheck               Retest errors/timeouts
    python3 rom_audit.py --recheck --system naomi Recheck one system
    python3 rom_audit.py --recheck-all            Retest everything including OKs
    python3 rom_audit.py --recheck-all --system naomi  Fresh baseline one system
    python3 rom_audit.py --new                   Test only untested ROMs
    python3 rom_audit.py --no-dashboard          Plain log output (SSH/tmux)

Screenshots:
    python3 rom_audit.py --screenshot            Capture screenshot per ROM
    python3 rom_audit.py --screenshot --annotate Burn system/ROM/timestamp in
    python3 rom_audit.py --screenshot --screenshot-flat  Flat dir, named per ROM
    python3 rom_audit.py --screenshot-delay 5    Extra wait before capture (s)

Checksums:
    python3 rom_audit.py --checksum md5          Record MD5 per ROM in CSV
    python3 rom_audit.py --checksum sha1         Record SHA1 per ROM in CSV

Filtering:
    python3 rom_audit.py --system mame --limit 10     Test 10 per system
    python3 rom_audit.py --exclude mame --exclude psx  Skip systems
    python3 rom_audit.py --since 2026-06-01            Only recently changed

Autofix (Recalbox / Batocera):
    python3 rom_audit.py --autofix               Fix errors using sidecar conf
    python3 rom_audit.py --recheck --autofix     Recheck then autofix

Cleanup:
    python3 rom_audit.py --cleanup --dry-run     Preview only
    python3 rom_audit.py --cleanup --action move Move errors to quarantine
    python3 rom_audit.py --cleanup --action delete

Resume / retest:
    python3 rom_audit.py pacman.7z               Resume from named ROM
    python3 rom_audit.py --recheck               Retest all non-OK results
    python3 rom_audit.py --no-es-restart         Skip ES restart after audit

Helper scripts (in scripts/):
    python3 scripts/summary.py                   Pass/fail totals per system
    python3 scripts/romlist.py --errors          List all errors
    python3 scripts/romlist.py --system mame     List one system
    python3 scripts/compare.py old.csv new.csv   Compare two audit runs
    python3 scripts/compare.py a.csv b.csv --checksum  Flag ROM replacements
    python3 scripts/duplicates.py                Find duplicate ROMs
    python3 scripts/filter.py --status ERROR     Filter CSV by status
    python3 scripts/prereqs.py                   Check platform prerequisites

Author:  Jason (muckypaws.com)
Created: May 2026
Licence: MIT
"""
from __future__ import annotations  # Python 3.7 compatibility

import os
import sys
import signal
import subprocess
import time
import argparse
from datetime import datetime

from modules.common.logging   import log, setup_log_file, close_log_file
from modules.common.detection import detect_platform
from modules.common.dashboard import Dashboard, calculate_eta
from modules.common import filehandling
from modules.common import pidfile as pidutil


# ---------------------------------------------------------------------------
# Audit configuration
# ---------------------------------------------------------------------------

# Maximum seconds to wait for a ROM to launch before marking as TIMEOUT
MAX_WAIT = 20

# How often to poll the log file for launch indicators (seconds)
CHECK_INTERVAL = 0.5

# Seconds to display a successfully launched game before killing it
DISPLAY_TIME = 3

# Number of recent ROM times to use for rolling ETA calculation
ROM_ETA_WINDOW = 10

# Tool version
VERSION = "1.4.4a"

# Systems requiring extended launch timeout due to tape, disk or slow
# emulator startup. Values are absolute seconds, overriding MAX_WAIT.
SLOW_SYSTEM_TIMEOUTS = {}  # type: dict[str, int]
# System-specific launch timeouts are now defined in each platform's
# get_launch_timeout() method. This dict is kept as a passthrough for
# any legacy callers; platform.get_launch_timeout(system) is the
# canonical source.



# ---------------------------------------------------------------------------
# Core test logic
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Main entry point. Detects the platform, parses arguments, initialises
    the dashboard and logging, then runs the requested operation.
    """
    parser = argparse.ArgumentParser(
        description='ROM Audit Tool — Multi-platform emulation tester',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                             Audit all systems
  %(prog)s --system mame               Audit MAME only
  %(prog)s --test pacman.7z            Test a single ROM
  %(prog)s --test pacman.7z --system mame
  %(prog)s --recheck                   Recheck errors and timeouts
  %(prog)s --autofix                   Autofix ERROR and GENUINE ERROR roms
  %(prog)s --recheck --autofix         Recheck everything failing then autofix
  %(prog)s --recheck --system mame
  %(prog)s --cleanup --dry-run         Preview cleanup
  %(prog)s --cleanup --action move     Move faulty ROMs to quarantine
  %(prog)s --cleanup --action delete   Delete faulty ROMs (with confirmation)
  %(prog)s --cleanup --action move --system mame aerofgts.7z
  %(prog)s bombjack.7z                 Resume from a specific ROM

Recommended for SSH use (prevents session drop from killing the audit):
  tmux new-session -s romaudit
  python3 rom_audit.py
  # Detach: Ctrl+B then D
  # Reattach: tmux attach -s romaudit
        """
    )
    parser.add_argument(
        'restart_from', nargs='?',
        help='ROM filename to restart from, or specific ROM for --cleanup'
    )
    parser.add_argument(
        '--recheck', action='store_true',
        help='Retest all non-OK results (ERROR, TIMEOUT, LAUNCHED, GENUINE ERROR, MISSING BIOS, MISSING CORE)'
    )
    parser.add_argument(
        '--recheck-all', action='store_true',
        help='Retest every ROM regardless of current status, including OK '
             'and FIXED. Use after major core updates or ROM set changes '
             'to establish a fresh baseline.'
    )
    parser.add_argument(
        '--new', action='store_true',
        help='Test only ROMs not yet in the CSV — useful after adding '
             'new ROMs to an existing collection without retesting everything'
    )
    parser.add_argument(
        '--system', type=str, nargs='+',
        help='Audit one or more systems (e.g. --system mame snes n64)'
    )
    parser.add_argument(
        '--exclude', type=str, nargs='+',
        help='Exclude one or more systems (e.g. --exclude mame fbneo)'
    )
    parser.add_argument(
        '--test', type=str,
        help='Test a single ROM by filename (e.g. --test pacman.7z)'
    )
    parser.add_argument(
        '--cleanup', action='store_true',
        help='Run cleanup pass on ERROR ROMs after remediation'
    )
    parser.add_argument(
        '--action', choices=['move', 'delete'], default='move',
        help='Cleanup action: move to quarantine (default) or delete permanently'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Preview cleanup changes without applying them'
    )
    parser.add_argument(
        '--include-imperfect', action='store_true',
        help='Include IMPERFECT ROMs in --cleanup. By default only '
             'ERROR and GENUINE ERROR ROMs are processed — IMPERFECT '
             'ROMs are playable and excluded unless this flag is set.'
    )
    parser.add_argument(
        '--include-needs-review', action='store_true',
        help='Include NEEDS REVIEW ROMs in --cleanup. By default these '
             'are excluded — a result the pixel heuristic flagged (or '
             'could not check) is not confirmed broken, just uncertain. '
             'Set this once you have actually looked at the screenshot '
             'and confirmed it really is bad.'
    )
    parser.add_argument(
        '--autofix', action='store_true',
        help='Attempt to fix failing ROMs by trying known core combinations'
    )
    parser.add_argument(
        '--no-dashboard', action='store_true',
        help='Disable the curses dashboard and use scrolling log output'
    )
    parser.add_argument(
        '--migrate', action='store_true',
        help='Migrate a ROM to correct system folder after autofix (RetroPie)'
    )
    parser.add_argument(
        '--core', type=str,
        help='Core name to migrate to (use with --migrate, e.g. lr-mame2010)'
    )
    parser.add_argument(
        '--screenshot', action='store_true',
        help='Capture a screenshot just before killing each emulator '
             '(saved to audit_logs/<system>/<rom>/<system>_<rom>_'
             'screenshot.png)'
    )
    parser.add_argument(
        '--screenshot-flat', action='store_true',
        help='Save screenshots as {system}_{rom}.png in a flat '
             'screenshots/ directory alongside the CSV, rather than '
             'nested inside audit_logs/<system>/<rom>/.'
    )
    parser.add_argument(
        '--screenshot-delay', type=float, metavar='SECONDS', default=0,
        help='Extra seconds to wait after display_time before capturing '
             'screenshot (default: 0). Useful for systems with long boot '
             'animations — PSP, NeoGeo, N64 etc.'
    )
    parser.add_argument(
        '--heuristic', action='store_true',
        help='Run pixel analysis on the forced verification screenshot '
             'whenever a result comes from a core in UNVERIFIED_CORES '
             '(FBNeo currently — known to silently grey-screen a bad '
             'dump with no error text, indistinguishable from genuine '
             'success in logs alone). Applies both to autofix and to a '
             'regular test/recheck where an existing config entry '
             'already points at that core. Without --heuristic, the '
             'screenshot is still captured and flagged UNVERIFIED for '
             'manual review; with it, the screenshot is also analysed '
             'and the result — "likely blank/error" or "likely genuine '
             'content" — is added to the notes automatically. Named '
             'heuristic rather than thorough deliberately: this is a '
             'probabilistic flag based on pixel sampling, not a '
             'definitive verdict. Adds real time per occurrence (a '
             'pure-Python PNG decode, currently several seconds at 4K '
             'capture resolution, well under a second at 1080p) — but '
             'only when that specific situation comes up, not on every '
             'ROM, so the total cost across a full audit is normally '
             'small regardless of resolution.'
    )
    parser.add_argument(
        '--annotate', action='store_true',
        help='Burn system, ROM name and timestamp into each captured '
             'screenshot. Requires --screenshot. Uses ffmpeg ASS subtitles '
             'or drawtext depending on platform.'
    )
    parser.add_argument(
        '--checksum', type=str, metavar='ALGORITHM', default=None,
        choices=['md5', 'sha1'],
        help='Compute a checksum for each ROM and store it in the CSV. '
             'Choose md5 or sha1. Blank if omitted. Note: large CD images '
             'may add several seconds per ROM on slow storage.'
    )
    parser.add_argument(
        '--no-es-restart', action='store_true',
        help='Do not restart EmulationStation after audit (RetroPie only)'
    )
    parser.add_argument(
        '--limit', type=int, metavar='N',
        help='Test at most N ROMs per system — useful for quick sanity checks'
    )
    parser.add_argument(
        '--since', type=str, metavar='DATE',
        help='Recheck ROMs tested before DATE (format: YYYY-MM-DD)'
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # SSH session warning — alert before anything else starts
    # ------------------------------------------------------------------
    if (os.environ.get('SSH_CONNECTION')
            and not os.environ.get('TMUX')
            and not os.environ.get('STY')
            and not args.no_dashboard):
        print()
        print("  WARNING: Running in an SSH session without tmux or screen.")
        print("  If your connection drops the audit will be killed.")
        print()
        print("  Recommended:")
        print("    tmux new-session -s romaudit 'python3 rom_audit.py'")
        print("    # Detach: Ctrl+B then D  |  Reattach: tmux attach -t romaudit")
        print()
        print("  Or use nohup:")
        print("    nohup python3 rom_audit.py --no-dashboard "
              "> rom_audit_console.log 2>&1 &")
        print("    # NOTE: do not redirect to rom_audit.log specifically —")
        print("    # that's also this tool's own internal log file path,")
        print("    # and two independent writers to the same file at the")
        print("    # OS level WILL corrupt each other's output.")
        print()
        print("  Continuing in 5 seconds — Ctrl+C to abort...")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            sys.exit(0)

    # ------------------------------------------------------------------
    # Platform detection
    # ------------------------------------------------------------------
    try:
        platform = detect_platform()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Ensure project directory exists before any file operations
    # ------------------------------------------------------------------
    os.makedirs(os.path.dirname(platform.log_file), exist_ok=True)

    # ------------------------------------------------------------------
    # Logging setup
    # ------------------------------------------------------------------
    setup_log_file(platform.log_file)
    log(f"ROM Audit Tool v{VERSION} starting")
    log(f"Platform: {platform.display_name}")

    # ------------------------------------------------------------------
    # Dashboard initialisation
    # ------------------------------------------------------------------
    dashboard = Dashboard()
    if not args.no_dashboard and not args.cleanup and not args.test:
        if not dashboard.start():
            log("Dashboard disabled — stdout is not an interactive "
                "terminal (redirected or piped). Using plain "
                "scrolling output instead.")
    else:
        log("Dashboard disabled.")

    # ------------------------------------------------------------------
    # PID file check and write
    # ------------------------------------------------------------------
    pidutil.check_running(platform.pid_file)
    pidutil.write_pid(platform.pid_file)

    # ------------------------------------------------------------------
    # tmux tip for SSH users
    # ------------------------------------------------------------------
    log("TIP: Running over SSH? Use tmux to survive session drops:")
    log("     tmux new-session -s romaudit")
    log("     Reattach with: tmux attach -s romaudit")

    try:
        # ------------------------------------------------------------------
        # Cleanup mode
        # ------------------------------------------------------------------
        if args.cleanup:
            if not args.dry_run and not args.action:
                log("ERROR: --cleanup requires --action move or "
                    "--action delete")
                log("       Use --dry-run to preview without making changes.")
                sys.exit(1)
            filehandling.run_cleanup(platform, args, specific_rom=args.restart_from)
            return

        # ------------------------------------------------------------------
        # Migrate mode (RetroPie only)
        # ------------------------------------------------------------------
        if getattr(args, 'migrate', False):
            if not args.restart_from:
                log("ERROR: --migrate requires a ROM filename.")
                log("  python3 rom_audit.py --migrate --system arcade "
                    "--core lr-mame2010 s1945a.zip")
                sys.exit(1)
            if not getattr(args, 'core', None):
                log("ERROR: --migrate requires --core <core_name>.")
                sys.exit(1)
            if not args.system:
                log("ERROR: --migrate requires --system <system>.")
                sys.exit(1)
            from modules.common import migrate as migrator
            already_tested = filehandling.load_results(platform.results_csv)
            migrator.migrate_rom(
                source_system  = args.system[0],
                romname        = args.restart_from,
                target_core    = args.core,
                roms_path      = platform.roms_path,
                results_csv    = platform.results_csv,
                already_tested = already_tested,
                dry_run        = args.dry_run
            )
            if not args.dry_run:
                filehandling.save_results(
                    platform.results_csv, already_tested
                )
            return

        # ------------------------------------------------------------------
        # Prepare autofix if active
        # ------------------------------------------------------------------
        installed_cores = None
        conf_backed_up  = False
        if args.autofix:
            platform.log_autofix_availability()
            platform.prepare_autofix()
            installed_cores = platform.get_installed_cores()

        # ------------------------------------------------------------------
        # Load previous results
        # ------------------------------------------------------------------
        already_tested = filehandling.load_results(platform.results_csv)
        log(f"Previously tested: {len(already_tested)} roms")

        # ------------------------------------------------------------------
        # Single ROM test mode
        # ------------------------------------------------------------------
        if args.test:
            log("Scanning for rom...")
            all_roms = filehandling.discover_roms(
                platform.roms_path, args.system,
                getattr(args, 'exclude', None)
            )
            for extra_path in getattr(platform, 'additional_roms_paths', []):
                if os.path.isdir(extra_path):
                    extra = filehandling.discover_roms(
                        extra_path, args.system,
                        getattr(args, 'exclude', None)
                    )
                    existing = {
                        f"{s}:{os.path.basename(r)}" for s, r in all_roms
                    }
                    all_roms.extend(
                        (s, r) for s, r in extra
                        if f"{s}:{os.path.basename(r)}" not in existing
                    )
            matches = [
                (s, r) for s, r in all_roms
                if os.path.basename(r) == args.test
            ]

            if not matches:
                log(f"ERROR: ROM '{args.test}' not found.")
                sys.exit(1)

            system, rom = matches[0]
            romname = platform.get_rom_display_name(system, rom)
            log(f"Testing [{system}]: {args.test}")

            # ROM confirmed — safe to prepare the platform
            platform.pre_audit()
            platform.pre_test_run({system})

            state = {
                'platform':            platform.display_name,
                'version':             VERSION,
                'current_system':      system,
                'current_rom':         args.test,
                'current_status':      'Testing...',
                'total':               1,
                'tested':              0,
                'counts':              {},
                'start_time':          time.time(),
                'elapsed':             0,
                'eta':                 MAX_WAIT,
                'checksum_algorithm':  getattr(args, 'checksum', None) or '',
            }

            # Resolve which core THIS ROM would actually use given the
            # current config, BEFORE testing — so a forced verification
            # screenshot can be requested up front if needed, the same
            # way attempt_autofix() already does for its own attempts.
            # Without this, a ROM with an existing config entry pointing
            # at an unverified core sails through this regular test path
            # with a plain, untrusted OK and no flag at all — confirmed
            # in practice with bandit.zip showing a grey error screen
            # and reporting plain OK on exactly this path.
            configured_core = getattr(
                platform, 'get_configured_core', lambda s, r: ''
            )(system, romname)
            screenshot_path = platform.prepare_screenshot_path(
                system, romname,
                getattr(args, 'screenshot', False),
                getattr(args, 'screenshot_flat', False),
                configured_core,
                heuristic=getattr(args, 'heuristic', False),
            )

            status, notes, elapsed = platform.run_test(
                system, rom, dashboard, state,
                timeout=platform.get_launch_timeout(system) or MAX_WAIT,
                screenshot_path=screenshot_path,
                screenshot_delay=getattr(args, 'screenshot_delay', 0),
                annotate=getattr(args, 'annotate', False)
            )

            status, notes, screenshot_path = platform.post_process_result(
                status, notes, system, romname, screenshot_path,
                configured_core, getattr(args, 'heuristic', False),
                dashboard, state
            )

            if status in ('ERROR', 'MISSING CORE', 'NEEDS REVIEW') and args.autofix:
                fix_status, fix_notes = platform.attempt_autofix(
                    system, rom, romname,
                    dashboard,
                    state,
                    original_error   = notes,
                    installed_cores  = installed_cores,
                    heuristic        = getattr(args, 'heuristic', False),
                    conf_backed_up   = conf_backed_up,
                    slow_timeouts    = {},
                )
                status, notes, was_fixed = platform.interpret_fix_result(
                    fix_status, fix_notes
                )
                if was_fixed:
                    conf_backed_up = True

            log(f"Result: {status} ({elapsed:.1f}s) {notes}")

            checksum_result = filehandling.record_result(
                already_tested, platform,
                system, args.test, status, notes, elapsed,
                rom=rom,
                checksum_algorithm=getattr(args, 'checksum', None) or '',
            )
            if checksum_result:
                state['checksum_result'] = checksum_result
                dashboard.update(state)
            log("CSV updated.")
            return



        # ------------------------------------------------------------------
        # ROM discovery
        # ------------------------------------------------------------------
        log("Scanning rom folders...")

        # Standard system discovery (ports excluded by SKIP_SYSTEMS;
        # real port ROMs come from discover_ports_roms() below).
        #
        # BUG FIXED: this used to blank system_filter to None entirely
        # whenever 'ports' appeared anywhere in it, which meant
        # "--system ports" (specifying ONLY ports) silently fell back
        # to scanning every system instead of none — discover_roms()
        # treats an empty/None filter as "no restriction". That's how
        # "--system ports --recheck" ended up rechecking mame: ports
        # produces nothing from the standard scan by design, but the
        # blanked filter let every other system's ROMs into all_roms,
        # and recheck_keys (built from the whole CSV, not scoped to
        # the requested system) matched plenty of them.
        #
        # Fix: only strip 'ports' itself out of the filter, preserving
        # any other systems requested alongside it. If nothing is left
        # after stripping it, skip the standard scan outright rather
        # than passing a filter value that discover_roms() would
        # interpret as "no filter at all".
        system_filter = args.system
        skip_standard_scan = False
        if system_filter:
            systems_list = (
                [system_filter] if isinstance(system_filter, str)
                else system_filter
            )
            non_ports = [s for s in systems_list if s != 'ports']
            if non_ports:
                system_filter = non_ports
            else:
                skip_standard_scan = True

        if skip_standard_scan:
            all_roms = []
        else:
            all_roms = filehandling.discover_roms(
                platform.roms_path, system_filter,
                getattr(args, 'exclude', None)
            )

        # Scan any additional ROM paths (e.g. Recalbox share_init).
        # Skipped along with the standard scan above when only 'ports'
        # was requested — same reasoning, nothing for this to find.
        if not skip_standard_scan:
            for extra_path in getattr(platform, 'additional_roms_paths', []):
                if os.path.isdir(extra_path):
                    extra_roms = filehandling.discover_roms(
                        extra_path, system_filter,
                        getattr(args, 'exclude', None)
                    )
                    # Deduplicate by system:romname — user path wins
                    existing_keys = {
                        f"{s}:{platform.get_rom_display_name(s, r)}"
                        for s, r in all_roms
                    }
                    added = [
                        (s, r) for s, r in extra_roms
                        if f"{s}:{platform.get_rom_display_name(s, r)}"
                        not in existing_keys
                    ]
                    if added:
                        log(f"  +{len(added)} ROM(s) from {extra_path}")
                    all_roms.extend(added)

        # Ports discovery — gamelist.xml based, separate from file scan
        wants_ports = (
            args.system is None
            or args.system == 'ports'
            or (isinstance(args.system, list) and 'ports' in args.system)
        )
        if wants_ports:
            port_roms = platform.discover_ports_roms()
            if port_roms:
                existing_keys = {
                    f"{s}:{platform.get_rom_display_name(s, r)}"
                    for s, r in all_roms
                }
                port_roms = [
                    (s, r) for s, r in port_roms
                    if f"{s}:{platform.get_rom_display_name(s, r)}"
                    not in existing_keys
                ]
                log(f"  +{len(port_roms)} port(s) discovered via gamelist.xml")
                all_roms.extend(port_roms)

        log(f"Total roms found: {len(all_roms)}")

        # ------------------------------------------------------------------
        # Build the list of ROMs to test
        # ------------------------------------------------------------------
        if args.recheck or args.autofix or getattr(args, 'recheck_all', False):
            # --recheck-all: retest everything including OK and FIXED
            if getattr(args, 'recheck_all', False):
                recheck_statuses = {
                    'OK', 'FIXED', 'ERROR', 'LAUNCHED', 'TIMEOUT',
                    'GENUINE ERROR', 'MISSING BIOS', 'MISSING CORE',
                    'NEEDS REVIEW','NO COMBINATIONS'
                }
            # --recheck tests all failing statuses
            # --autofix alone only needs ERROR and GENUINE ERROR
            elif args.recheck:
                recheck_statuses = {
                    'ERROR', 'LAUNCHED', 'TIMEOUT', 'MISSING CORE',
                    'GENUINE ERROR', 'MISSING BIOS', 'NEEDS REVIEW'
                }
            else:
                recheck_statuses = {'ERROR', 'GENUINE ERROR', 'NEEDS REVIEW'}

            # --since: also recheck ROMs tested before a given date
            since_date = None
            if getattr(args, 'since', None):
                try:
                    from datetime import datetime
                    since_date = datetime.strptime(
                        args.since, '%Y-%m-%d'
                    )
                    log(f"  Since filter: retesting ROMs "
                        f"tested before {args.since}")
                    # Expand recheck set to include stale OK results
                    recheck_statuses |= {'OK', 'FIXED'}
                except ValueError:
                    log(f"  WARNING: --since date '{args.since}' "
                        f"is not in YYYY-MM-DD format — ignoring")

            recheck_keys = set()
            for key, row in already_tested.items():
                status = row.get('status', '')
                if status not in recheck_statuses:
                    continue
                if since_date:
                    tested_at_str = row.get('tested_at', '')[:10]
                    try:
                        from datetime import datetime
                        tested_at = datetime.strptime(
                            tested_at_str, '%Y-%m-%d'
                        )
                        if tested_at >= since_date:
                            continue   # Tested recently — skip
                    except ValueError:
                        pass
                recheck_keys.add(key)

            roms_to_test = [
                (system, rom) for system, rom in all_roms
                if f"{system}:{platform.get_rom_display_name(system, rom)}" in recheck_keys
            ]
            mode = "Recheck + Autofix" if args.autofix else "Recheck"
            log(f"{mode} mode: {len(roms_to_test)} roms to retest")

        elif getattr(args, 'new', False):
            # --new: test only ROMs with no CSV entry at all.
            # Useful when new ROMs are added to an existing collection
            # without wanting to retest everything that already passed.
            # Does NOT retest previously failing ROMs — use --recheck for that.
            roms_to_test = [
                (system, rom) for system, rom in all_roms
                if f"{system}:{platform.get_rom_display_name(system, rom)}" not in already_tested
            ]
            log(f"New ROMs mode: {len(roms_to_test)} untested ROM(s) found")
            if not roms_to_test:
                log("  No new ROMs found — all discovered ROMs are "
                    "already in the CSV.")
                log("  Use --recheck to retest failures, or --since DATE "
                    "to retest stale results.")

        else:
            roms_to_test  = []
            restart_found = args.restart_from is None

            for system, rom in all_roms:
                romname = platform.get_rom_display_name(system, rom)
                if not restart_found:
                    if romname == args.restart_from:
                        restart_found = True
                    else:
                        continue
                key = f"{system}:{romname}"
                if key not in already_tested:
                    roms_to_test.append((system, rom))

            if args.restart_from and not restart_found:
                log(f"ERROR: ROM '{args.restart_from}' not found.")
                sys.exit(1)

        # Apply --limit: cap ROM count per system
        if getattr(args, 'limit', None):
            from itertools import groupby
            limited = []
            for sys_name, grp in groupby(roms_to_test, key=lambda x: x[0]):
                limited.extend(list(grp)[:args.limit])
            if len(limited) < len(roms_to_test):
                log(f"  Limit: {len(limited)} ROMs selected "
                    f"(max {args.limit} per system)")
            roms_to_test = limited

        log(f"Roms to test: {len(roms_to_test)}")

        # ------------------------------------------------------------------
        # Screenshot disk space check
        # Each screenshot is roughly 100-300KB depending on the system.
        # With thousands of ROMs this can add up fast. Warn if headroom
        # looks tight; disable the feature automatically if critically low.
        # ------------------------------------------------------------------
        if getattr(args, 'screenshot', False) and roms_to_test:
            try:
                import shutil as _shutil
                stat  = _shutil.disk_usage(platform.error_log_base)
                free_mb      = stat.free / (1024 * 1024)
                estimated_mb = len(roms_to_test) * 0.2   # ~200KB average
                headroom_mb  = 500

                if free_mb < headroom_mb:
                    log(f"WARNING: Only {free_mb:.0f}MB free on "
                        f"{platform.error_log_base} — disabling "
                        f"--screenshot to protect disk space.")
                    args.screenshot = False
                elif free_mb < estimated_mb + headroom_mb:
                    log(f"WARNING: Screenshot mode may use "
                        f"~{estimated_mb:.0f}MB but only "
                        f"{free_mb:.0f}MB free. Screenshots enabled "
                        f"but monitor disk usage.")
                else:
                    log(f"Screenshot mode: ~{estimated_mb:.0f}MB "
                        f"estimated, {free_mb:.0f}MB free — OK")
            except Exception:
                pass   # Non-fatal — continue without the check

        if getattr(args, 'heuristic', False):
            log("Heuristic mode: pixel analysis will run on the forced "
                "verification screenshot whenever a result comes from "
                "an UNVERIFIED_CORES core (FBNeo currently) — both "
                "during autofix and on a regular test/recheck where an "
                "existing config entry already points at it. This adds "
                "real time per occurrence — a pure-Python PNG decode, "
                "several seconds at 4K, under a second at 1080p — but "
                "only when that specific situation comes up, not on "
                "every ROM.")

        # Log screenshot delay hints for systems with long boot sequences.
        # If --screenshot is active but no explicit --screenshot-delay was
        # given, suggest the recommended delay for the systems being tested.
        if getattr(args, 'screenshot', False) and roms_to_test:
            delay = getattr(args, 'screenshot_delay', 0)
            if delay == 0:
                systems_tested = {s for s, _ in roms_to_test}
                hints = {
                    s: platform.get_screenshot_delay_hint(s)
                    for s in systems_tested
                    if platform.get_screenshot_delay_hint(s) > 0
                }
                if hints:
                    log("Screenshot delay hints (use --screenshot-delay N):")
                    for sys_name, secs in sorted(hints.items()):
                        log(f"  {sys_name:<16} suggested: {secs}s")

        if not roms_to_test:
            log("Nothing to test — no pre-audit steps needed.")
        else:
            # Only prepare the platform (stop ES on RetroPie etc.)
            # when there is actual ROM testing work to do.
            platform.pre_audit()

        # Notify the platform which systems will be tested so it can
        # perform any necessary pre-test setup (e.g. Batocera suspends
        # global core overrides that would mask genuine failures).
        if roms_to_test:
            tested_systems = {s for s, _ in roms_to_test}
            platform.pre_test_run(tested_systems)

        log(
            f"Estimated time: "
            f"{len(roms_to_test) * (MAX_WAIT / 2) / 60:.0f}-"
            f"{len(roms_to_test) * MAX_WAIT / 60:.0f} minutes"
        )
        log("Starting in 3 seconds...")
        time.sleep(3)

        # ------------------------------------------------------------------
        # Initialise counters from any previous run
        # ------------------------------------------------------------------
        counts = {
            "OK": 0, "ERROR": 0, "MISSING CORE": 0,
            "MISSING BIOS": 0, "TIMEOUT": 0, "LAUNCHED": 0,
            "GENUINE ERROR": 0, "FIXED": 0, "IMPERFECT": 0
        }
        for row in already_tested.values():
            s = row.get('status', 'UNKNOWN')
            counts[s] = counts.get(s, 0) + 1

        # Per-system rom counts and total distinct system count, for the
        # dashboard's "Systems Tested: X/Y" and per-system "Rom: X/Y"
        # display. roms_to_test is confirmed system-contiguous (built from
        # discover_roms()'s sorted output, never reordered afterward), so
        # tracking transitions while iterating the flat list below is
        # sufficient — no need for a nested per-system structure.
        system_rom_counts = {}  # type: dict[str, int]
        for sys_name, _ in roms_to_test:
            system_rom_counts[sys_name] = system_rom_counts.get(sys_name, 0) + 1
        total_systems = len(system_rom_counts)

        # Shared state dict updated throughout the audit loop
        state = {
            'platform':           platform.display_name,
            'version':            VERSION,
            'current_system':     '',
            'current_rom':        '',
            'current_status':     'Starting...',
            'total':              len(roms_to_test),
            'tested':             0,
            'counts':             counts,
            'start_time':         time.time(),
            'elapsed':            0,
            'eta':                MAX_WAIT * len(roms_to_test),
            'checksum_algorithm': getattr(args, 'checksum', None) or '',
            'checksum_info':      '',
            'checksum_result':    '',
            'total_systems':      total_systems,
            'systems_completed':  0,
            'system_rom_index':   0,
            'system_rom_total':   0,
        }

        # Rolling window of recent ROM durations for ETA calculation
        recent_times = []  # type: list[float]

        # Track whether batocera.conf has been backed up this session
        conf_backed_up = False
        last_rom_tested = None

        # System-transition tracking for the dashboard counters above —
        # loop-local, not part of state itself until assigned each iteration
        systems_completed = 0
        previous_system    = None
        system_rom_index   = 0

        # ------------------------------------------------------------------
        # Main audit loop
        # ------------------------------------------------------------------
        try:
            interrupted = False
            for i, (system, rom) in enumerate(roms_to_test, 1):
                romname = platform.get_rom_display_name(system, rom)
                last_rom_tested = romname

                # A new system started — the previous one (if any) just
                # completed. roms_to_test's confirmed contiguity is what
                # makes this single comparison sufficient; no lookahead
                # or per-system pass needed.
                if system != previous_system:
                    if previous_system is not None:
                        systems_completed += 1
                    system_rom_index = 0
                    previous_system  = system
                system_rom_index += 1

                # Update state before test starts using current rolling avg
                state['current_system']     = system
                state['current_rom']        = romname
                state['current_status']     = 'Testing...'
                # Show the checksum from CSV (if any) on the dashboard from
                # the start of this ROM's test, so user can see what's on
                # record before verification runs.
                existing_entry = already_tested.get(f'{system}:{romname}', {})
                state['checksum_info']   = existing_entry.get('checksum', '')
                state['checksum_result'] = ''   # cleared; set after verify
                state['tested']             = i - 1
                state['systems_completed']  = systems_completed
                state['system_rom_index']   = system_rom_index
                state['system_rom_total']   = system_rom_counts[system]
                state['elapsed']            = time.time() - state['start_time']
                state['eta']                = calculate_eta(
                    recent_times, len(roms_to_test) - i + 1
                )
                dashboard.update(state)

                log(f"[{i}/{len(roms_to_test)}] "
                    f"[{system}] Testing: {romname} ...")

                rom_wall_start = time.time()

                # See the --test path above for the full reasoning —
                # same pre-check, same reason: a ROM with an existing
                # config entry pointing at an unverified core must not
                # sail through this path with a plain, untrusted OK.
                configured_core = getattr(
                    platform, 'get_configured_core', lambda s, r: ''
                )(system, romname)
                screenshot_path = platform.prepare_screenshot_path(
                    system, romname,
                    getattr(args, 'screenshot', False),
                    getattr(args, 'screenshot_flat', False),
                    configured_core,
                    heuristic=getattr(args, 'heuristic', False),
                )

                status, notes, elapsed = platform.run_test(
                    system, rom, dashboard, state,
                    timeout=platform.get_launch_timeout(system) or MAX_WAIT,
                    screenshot_path=screenshot_path,
                    screenshot_delay=getattr(args, 'screenshot_delay', 0),
                    annotate=getattr(args, 'annotate', False)
                )

                status, notes, screenshot_path = platform.post_process_result(
                    status, notes, system, romname, screenshot_path,
                    configured_core, getattr(args, 'heuristic', False),
                    dashboard, state
                )

                # Autofix — also triggered on MISSING CORE since
                # validate_rom_launch() can reject a ROM pre-launch (e.g.
                # Recalbox extension mismatch against the default core)
                # when other installed cores would actually work.
                # Also triggered on NEEDS REVIEW from UNVERIFIED_CORES
                # (heuristic confirmed grey screen) — unfixable-by-core-swap
                # cases (NO GOOD DUMP KNOWN, dump-quality warnings) are
                # handled inside platform.attempt_autofix() itself.
                if status in ('ERROR', 'MISSING CORE', 'NEEDS REVIEW') and args.autofix:
                    fix_status, fix_notes = platform.attempt_autofix(
                        system, rom, romname,
                        dashboard,
                        state,
                        original_error   = notes,
                        installed_cores  = installed_cores,
                        heuristic        = getattr(args, 'heuristic', False),
                        conf_backed_up   = conf_backed_up,
                        slow_timeouts    = {},
                    )
                    status, notes, was_fixed = platform.interpret_fix_result(
                        fix_status, fix_notes
                    )
                    if was_fixed:
                        counts['FIXED'] = counts.get('FIXED', 0) + 1
                        conf_backed_up  = True
                    elif fix_status == 'NEEDS REVIEW':
                        counts['NEEDS REVIEW'] = (
                            counts.get('NEEDS REVIEW', 0) + 1
                        )
                        conf_backed_up = True

                # Update rolling ETA window with actual wall clock time
                # including kill/wait time for standalone emulators.
                rom_wall_time = time.time() - rom_wall_start
                recent_times.append(rom_wall_time)
                if len(recent_times) > ROM_ETA_WINDOW:
                    recent_times.pop(0)

                # Decrement previous status count if ROM was already in CSV
                key = f"{system}:{romname}"
                if key in already_tested:
                    prev_status = already_tested[key].get('status', '')
                    if prev_status in counts:
                        counts[prev_status] = max(
                            0, counts.get(prev_status, 0) - 1
                        )

                counts[status] = counts.get(status, 0) + 1
                state['counts']         = counts
                state['tested']         = i
                state['current_status'] = f'{status} ({elapsed:.1f}s)'
                state['elapsed']        = time.time() - state['start_time']
                state['eta']            = calculate_eta(
                    recent_times, len(roms_to_test) - i
                )
                dashboard.update(state)

                log(f"  -> {status} ({elapsed:.1f}s) {notes}")

                checksum_result = filehandling.record_result(
                    already_tested, platform,
                    system, romname, status, notes, elapsed,
                    rom=rom,
                    checksum_algorithm=state.get('checksum_algorithm', ''),
                )
                if checksum_result:
                    state['checksum_result'] = checksum_result

                # Refresh dashboard so the completed ROM's result is
                # visible — the pre-record update above had stale counts
                state['current_status'] = (
                    f'{status} \u2014 {romname}'
                    if status != 'OK' else f'OK \u2014 {romname}'
                )
                state['elapsed'] = time.time() - state['start_time']
                dashboard.update(state)

                # Progress summary every 10 ROMs in the log
                if i % 10 == 0:
                    elapsed_str = Dashboard._fmt_time(
                        time.time() - state['start_time']
                    )
                    eta_str = Dashboard._fmt_time(
                        calculate_eta(recent_times, len(roms_to_test) - i)
                    )
                    log(
                        f"--- Progress: {i}/{len(roms_to_test)} | "
                        f"OK:{counts['OK']} | "
                        f"Fixed:{counts.get('FIXED', 0)} | "
                        f"Error:{counts['ERROR']} | "
                        f"Genuine:{counts.get('GENUINE ERROR', 0)} | "
                        f"Missing Core:{counts['MISSING CORE']} | "
                        f"Missing BIOS:{counts.get('MISSING BIOS', 0)} | "
                        f"Imperfect:{counts.get('IMPERFECT', 0)} | "
                        f"Needs Review:{counts.get('NEEDS REVIEW', 0)} | "
                        f"Timeout:{counts['TIMEOUT']} | "
                        f"Elapsed:{elapsed_str} | "
                        f"ETA:{eta_str} ---"
                    )

        except KeyboardInterrupt:
            interrupted = True
            log("\nInterrupted. Progress saved.")
            if last_rom_tested:
                log(f"To resume: python3 rom_audit.py {last_rom_tested}")

        # Force one final dashboard render. On normal completion this
        # shows "Complete" with the full count; on interrupt it shows
        # the genuine progress reached, not a misleading 100%/N-of-N —
        # state['tested'] already holds the real completed count from
        # inside the loop (set after each ROM finishes), so the fix
        # here is simply to stop overwriting it on the interrupted path.
        state['current_status']    = 'Interrupted' if interrupted else 'Complete'
        state['elapsed']           = time.time() - state['start_time']
        state['eta']               = 0
        state['counts']            = counts
        if not interrupted:
            state['tested'] = len(roms_to_test)
            # The last system's own completion never triggers the in-loop
            # transition increment above — there's no next system to
            # detect it. Bump explicitly here so the final display reads
            # N/N, not one short. Only valid on genuine completion — on
            # interrupt, systems_completed already holds the real count
            # of systems actually finished, which is what should show.
            state['systems_completed'] = total_systems
        dashboard.force_update(state)

    finally:
        # Platform cleans up after itself — restores any config changes
        # made in pre_test_run (e.g. Batocera restores suspended overrides).
        platform.post_test_run()

        # Stop the dashboard first — summary is then logged as plain text
        # so it appears cleanly below the dashboard without duplication.
        platform.post_audit()
        dashboard.stop()

        # Summary printed after stop() so it goes directly to stdout
        # rather than into the dashboard buffer.
        try:
            log("\n=== AUDIT COMPLETE ===")
            log(
                f"OK:{counts['OK']} | "
                f"Fixed:{counts.get('FIXED', 0)} | "
                f"Error:{counts['ERROR']} | "
                f"Genuine Error:{counts.get('GENUINE ERROR', 0)} | "
                f"Missing Core:{counts['MISSING CORE']} | "
                f"Missing BIOS:{counts.get('MISSING BIOS', 0)} | "
                f"Imperfect:{counts.get('IMPERFECT', 0)} | "
                f"Needs Review:{counts.get('NEEDS REVIEW', 0)} | "
                f"Timeout:{counts['TIMEOUT']}"
            )
            log(f"Elapsed  : {Dashboard._fmt_time(state['elapsed'])}")
            log(f"Results  : {platform.results_csv}")
            log(f"Log file : {platform.log_file}")
            if counts.get('GENUINE ERROR', 0) > 0:
                log("Genuine errors cannot be fixed by core changes alone.")
                log("Consider replacing the ROM dump or removing with --cleanup.")
            if counts.get('NEEDS REVIEW', 0) > 0:
                log("NEEDS REVIEW results are not confirmed broken — an "
                    "UNVERIFIED_CORES result the pixel check flagged or "
                    "could not check at all. Look at the screenshot before "
                    "deciding: python3 scripts/romlist.py --needs-review")
            if counts['ERROR'] > 0:
                log(f"Error logs: {platform.error_log_base}")
                log("Run with --autofix to attempt automatic fixes.")
                log("Run with --cleanup --action move when done to quarantine "
                    "remaining failures.")
        except Exception:
            pass  # Never let summary printing crash the cleanup

        pidutil.remove_pid(platform.pid_file)
        close_log_file()


if __name__ == "__main__":
    main()
