"""
Batocera platform implementation for the ROM Audit Tool.

Supports Batocera v38 (direct script invocation with PYTHONPATH) and
v40+ (Python module invocation via -m flag). The version is detected
automatically at initialisation via the batocera-version command.

Future Batocera versions should require no changes here unless the
launcher mechanism or log format changes significantly.
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import re
import sys
import subprocess
from modules.platforms.base import Platform
from modules.common.logging import log
from modules.common import autofix as autofixer
from modules.common import configeditor


class BatoceraPlaftorm(Platform):
    """
    Platform implementation for Batocera Linux.

    Handles version detection to select the correct emulatorlauncher
    invocation, and provides Batocera-specific paths, environment
    variables, and log analysis markers.

    Supported versions:
        v38 and earlier: Direct script call with PYTHONPATH set
        v40 and later:   Python module invocation via -m flag
    """

    # ------------------------------------------------------------------
    # Log analysis criteria
    # These are passed to the common detection functions and define
    # what constitutes a successful launch or an error on Batocera.
    # ------------------------------------------------------------------

    # Strings in stdout that indicate RetroArch was successfully invoked.
    # Different Batocera versions produce different log output at launch.
    # Strings in stderr that indicate a specific libretro or MAME error
    ERROR_MARKERS = [
        "[libretro ERROR]",
        "Fatal error",
        "Failed to load",
    ]

    POST_LAUNCH_ERROR_MARKERS = [
        "NOT FOUND",                          # MAME missing ROM files
        "MAME returned an error!",            # MAME general failure
        "is required",                        # FBNeo missing ROM files
        "Failed to load content",             # RetroArch content load failure
        "Wrong data-format, corrupt or unsupported ROM",  # Gambatte corrupt ROM
        "Failed to extract content from compressed file", # Core requires uncompressed
    ]

    # These strings may appear in post-launch logs but are non-fatal warnings
    # that should NOT trigger an ERROR classification. A ROM is only flagged
    # as an error if a POST_LAUNCH_ERROR_MARKERS match is NOT in this list.
    # MAME driver status flags — game is playable but not arcade-perfect.
    # These appear as a warning screen at launch, not a crash.
    IMPERFECT_MARKERS = [
        'There are known problems with this game',
        'There are known problems emulating this',
        'The video emulation isn\'t 100% accurate',
        'The colors aren\'t 100% accurate',
        'The sound emulation isn\'t 100% accurate',
    ]

    # Dump-quality markers — the ROM has an unverified or known-bad
    # checksum but MAME will attempt to run it anyway. Unlike the
    # accuracy-warning IMPERFECT_MARKERS above (which confirm the game
    # loaded and is running, just not perfectly), these cannot confirm
    # the game actually loaded — two completely different runtime
    # outcomes produce the same text:
    #   (a) game loads and runs despite the bad dump   → genuinely IMPERFECT
    #   (b) game fails to display anything meaningful  → should be ERROR
    # A screenshot + pixel heuristic can distinguish them; without one,
    # the result lands as NEEDS REVIEW rather than a false IMPERFECT.
    # These are kept separate from IMPERFECT_MARKERS so run_test() can
    # flag which kind of IMPERFECT it found, and rom_audit.py's main
    # loop can route accordingly.
    DUMP_QUALITY_MARKERS = [
        # Follows "filename NOT FOUND (NO GOOD DUMP KNOWN)" — MAME found
        # the ROM chip but its checksum is unverified. The game may run
        # fine despite the warning — confirmed in practice with bigstrik.zip
        # displaying correctly on screen while being classified GENUINE ERROR.
        # Same ambiguity as ROM NEEDS REDUMP: only a screenshot heuristic
        # can distinguish "running fine" from "blank screen".
        'WARNING: the game might not run correctly',
        # MAME 2003+'s equivalent wording for the same situation.
        'ROM NEEDS REDUMP',
        # Standalone form — appears on its own in some MAME core versions
        # as well as in the "(NO GOOD DUMP KNOWN)" suffix form below.
        # Previously in POST_LAUNCH_ERROR_MARKERS on the incorrect assumption
        # that an unverified dump is always unrunnable — empirically false.
        'NO GOOD DUMP KNOWN',
    ]

    NON_FATAL_POST_LAUNCH_MARKERS = [
        # MAME outputs this when the samples directory is empty or absent.
        # The game still runs correctly — it just lacks audio samples.
        "parse path failed",
        # VICE (C64/C128) tries JiffyDOS as an optional kernal replacement.
        # When JiffyDOS files are absent it logs [libretro ERROR] and falls
        # back to the built-in kernal automatically. The game runs normally.
        "JiffyDOS",
        # VICE optional drive firmware — not required for normal operation.
        "DriveROM:",
        # VICE format auto-detection probes every file through multiple
        # format detectors in sequence. Failed probes are logged as
        # [libretro ERROR] but VICE continues to the correct format.
        # e.g. CRT files trigger GCR disk and tape probe failures before
        # VICE correctly identifies them as cartridge images.
        "Filesystem Image Probe:",
        "Tape: Cannot open file",
        # libretro cores log this when the frontend requests an input device
        # type the core doesn't support. The core falls back to a standard
        # joypad and the game runs normally. Affects Atari 2600 (stella)
        # and other cores that don't implement all controller device types.
        "RETRO_DEVICE_JOYPAD",
        # MAME 2010 (mame0139) logs "filename NOT FOUND (NO GOOD DUMP KNOWN)"
        # followed by "WARNING: the game might not run correctly." when a ROM
        # chip is present but has an unverified checksum. MAME continues to
        # run with the unverified dump — confirmed running in practice.
        # Without the suffix, bare "NOT FOUND" means the file is genuinely
        # absent and remains a fatal error.
        "(NO GOOD DUMP KNOWN)",
        # Standalone form — suppressed here so the dump-quality marker
        # can classify it via DUMP_QUALITY_MARKERS instead of ERROR.
        "NO GOOD DUMP KNOWN",
        # MAME 2003+ flags a ROM chip as needing a redump when its
        # checksum doesn't match the known-good set, but continues
        # running with the unverified dump anyway.
        "ROM NEEDS REDUMP",
    ]

    @property
    def non_fatal_post_launch_markers(self) -> list[str]:
        return self.NON_FATAL_POST_LAUNCH_MARKERS

    @property
    def imperfect_markers(self) -> list[str]:
        return self.IMPERFECT_MARKERS

    @property
    def dump_quality_markers(self) -> list[str]:
        """
        Markers that indicate a ROM has an unverified or known-bad dump
        but MAME will attempt to run it anyway.  Unlike imperfect_markers
        (which confirm the game loaded), these leave the actual load
        status ambiguous — a screenshot heuristic is needed to resolve
        whether the game genuinely displayed content (IMPERFECT) or
        showed nothing useful (ERROR).  Returning them separately lets
        the main loop route the result through verify_unverified_core()
        rather than trusting a bare IMPERFECT classification.
        """
        return self.DUMP_QUALITY_MARKERS

    BIOS_ERROR_MARKERS = [
        "BIOS not found",           # Flycast / generic
        "bios not found",           # Case variants
        "Required BIOS",            # Some cores
        "bios] NOT FOUND",          # MAME BIOS zip missing
    ]

    LAUNCH_INDICATORS = [
        # v43+: explicit controller monitor thread message
        "Starting background controller monitor",
        # v38: runCommand debug line showing the retroarch binary path
        "runCommand command: ['/usr/bin/retroarch'",
        # v43 with PosixPath objects in debug log output
        "runCommand command: [PosixPath('/usr/bin/retroarch')",
        # Standalone mupen64plus — N64
        "runCommand command: ['/usr/bin/mupen64plus'",
        # Standalone PPSSPP — PSP (appears at launch, not on exit)
        "runCommand command: ['/usr/bin/PPSSPP'",
        # Standalone VICE — VIC-20 (add correct binary once confirmed)
        "runCommand command: ['/usr/bin/x20'",
        "runCommand command: ['/usr/bin/xvic'",
    ]

    # String in stdout that indicates a non-zero exit from emulatorlauncher
    EXIT_MARKER = "Exiting configgen with status 1"

    def __init__(self) -> None:
        """
        Initialise the Batocera platform.

        Detects the installed Batocera version and builds the correct
        launcher command and any required extra environment variables.
        """
        self._version = self._detect_version()
        self._launcher_cmd, self._extra_env = self._build_launcher()

    # ------------------------------------------------------------------
    # Internal version detection and launcher construction
    # ------------------------------------------------------------------

    def _detect_version(self) -> int:
        """
        Detect the installed Batocera major version number.

        Runs batocera-version which outputs a string in the format
        "38 2023/10/14 03:07" or "36c 2023/03/04 09:23". Extracts
        and returns the leading integer as the major version.
        Returns 0 if detection fails for any reason.

        Handles version strings with trailing letters (e.g. "36c")
        by extracting only the leading numeric portion.

        Returns:
            Integer major version number, or 0 if undetectable.
        """
        import re
        try:
            result = subprocess.run(
                ['batocera-version'],
                capture_output=True,
                text=True
            )
            version_str = result.stdout.strip().split()[0]
            match = re.match(r'(\d+)', version_str)
            return int(match.group(1)) if match else 0
        except Exception:
            return 0

    def _build_launcher(self) -> tuple[list[str], dict[str, str]]:
        """
        Build the correct emulatorlauncher command for the detected version.

        Batocera v38 and earlier require emulatorlauncher.py to be called
        as a direct script with PYTHONPATH set so its relative imports
        resolve correctly. v40 and later expose it as a proper Python
        package invoked with the -m module flag.

        Returns:
            Tuple of (command_list, extra_env_dict) where extra_env_dict
            contains any additional environment variables needed beyond
            the standard display variables.
        """
        py_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        configgen_path = (
            f"/usr/lib/python{py_version}/site-packages/configgen"
        )
        launcher_path = f"{configgen_path}/emulatorlauncher.py"

        if self._version >= 40:
            # v40+: proper package, use module invocation
            cmd = ["/usr/bin/python3", "-m", "configgen.emulatorlauncher"]
            extra_env = {}
        else:
            # v38 and earlier: direct script with PYTHONPATH
            cmd = ["/usr/bin/python3", launcher_path]
            extra_env = {'PYTHONPATH': configgen_path}

        log(f"Batocera v{self._version} detected, Python {py_version}")
        log(f"Launcher: {' '.join(cmd)}")

        return cmd, extra_env

    # ------------------------------------------------------------------
    # Platform properties - paths and identification
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Human-readable platform name including detected version."""
        return f"Batocera v{self._version}"

    @property
    def roms_path(self) -> str:
        """Base path where all system ROM folders live."""
        return "/userdata/roms"

    @property
    def stdout_log(self) -> str:
        """Path to the emulator launcher stdout log file."""
        return "/userdata/system/logs/es_launch_stdout.log"

    @property
    def stderr_log(self) -> str:
        """Path to the emulator launcher stderr log file."""
        return "/userdata/system/logs/es_launch_stderr.log"

    @property
    def results_csv(self) -> str:
        """Path to the audit results CSV file."""
        return "/userdata/system/rom_audit/rom_audit.csv"

    @property
    def log_file(self) -> str:
        """Path to the persistent audit CLI log file."""
        return "/userdata/system/rom_audit/rom_audit.log"

    @property
    def pid_file(self) -> str:
        """Path to the PID file used to prevent duplicate runs."""
        return "/userdata/system/rom_audit/rom_audit.pid"

    @property
    def error_log_base(self) -> str:
        """Root path for archived error logs, grouped by system."""
        return "/userdata/system/rom_audit/audit_logs"

    @property
    def conf_path(self) -> str:
        """Full path to batocera.conf."""
        return "/userdata/system/batocera.conf"
    
    @property
    def libretro_core_path(self) -> str:
        return "/usr/lib/libretro"

    @property
    def faulty_roms_path(self) -> str:
        return "/userdata/faultyroms"

    # ------------------------------------------------------------------
    # Platform properties - log analysis criteria
    # ------------------------------------------------------------------

    @property
    def launch_indicators(self) -> list[str]:
        """Strings in stdout indicating successful RetroArch launch."""
        return self.LAUNCH_INDICATORS

    @property
    def error_markers(self) -> list[str]:
        """Strings in stderr indicating a specific error."""
        return self.ERROR_MARKERS

    @property
    def exit_marker(self) -> str:
        """String in stdout indicating non-zero launcher exit."""
        return self.EXIT_MARKER
    
    @property
    def post_launch_error_markers(self) -> list[str]:
        return self.POST_LAUNCH_ERROR_MARKERS

    BIOS_ERROR_MARKERS = [
        "BIOS not found",
        "bios not found",
        "Required BIOS",
        "bios] NOT FOUND",
        "cannot load BIOS",
        # GSplus (Apple II / IIgs) — missing ROM/BIOS produces a screen
        # full of @ signs and this log entry in stdout
        "Could not find required file",
    ]

    @property
    def bios_error_markers(self) -> list[str]:
        return self.BIOS_ERROR_MARKERS

    @property
    def subprocess_capture(self) -> bool:
        """Batocera captures subprocess output directly to log files."""
        return True

    # ------------------------------------------------------------------
    # Emulator management
    def capture_screenshot(self, dest_path: str) -> bool:
        """
        Capture the current display to a PNG file.

        Tries grim first (Wayland compositor capture) — works for all
        libretro/RetroArch cores which render through Wayland.

        Falls back to fbgrab (KMS/DRM framebuffer) for standalone
        emulators (Flycast/Naomi, mupen64plus/N64, Game and Watch etc.)
        that bypass the Wayland compositor and render directly to the
        framebuffer. grim cannot see these — fbgrab can.

        Args:
            dest_path: Full path for the output PNG file.

        Returns:
            True if capture succeeded and the file exists, False otherwise.
        """
        import os
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        # Build environment with Wayland display variables.
        # SSH sessions don't inherit these — grim silently returns 0
        # without writing anything if WAYLAND_DISPLAY is unset.
        env = os.environ.copy()
        env.setdefault('WAYLAND_DISPLAY', 'wayland-1')
        env.setdefault('XDG_RUNTIME_DIR', '/var/run')

        # Try grim — Wayland compositor capture
        # Works for RetroArch/libretro cores
        try:
            result = subprocess.run(
                ['grim', dest_path],
                capture_output=True,
                timeout=5,
                env=env
            )
            if result.returncode == 0 and os.path.exists(dest_path):
                return True
            if result.returncode != 0:
                log(f"  Screenshot: grim failed ({result.returncode}) "
                    f"— trying fbgrab")
        except FileNotFoundError:
            log("  Screenshot: grim not found — trying fbgrab")
        except Exception as e:
            log(f"  Screenshot: grim error ({e}) — trying fbgrab")

        # Fallback: fbgrab — KMS/DRM framebuffer capture
        # Works for standalone emulators that bypass Wayland:
        # Flycast (Naomi/Atomiswave), mupen64plus (N64),
        # Game and Watch, and other non-RetroArch emulators
        try:
            result = subprocess.run(
                ['fbgrab', dest_path],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0 and os.path.exists(dest_path):
                log("  Screenshot: captured via fbgrab (framebuffer)")
                return True
            if result.returncode != 0:
                log(f"  Screenshot: fbgrab also failed "
                    f"({result.stderr.decode(errors='replace').strip()})")
        except FileNotFoundError:
            log("  Screenshot: fbgrab not found — no capture method available")
        except Exception as e:
            log(f"  Screenshot: fbgrab error — {e}")

        return False

    # ------------------------------------------------------------------

    @property
    def emulator_processes(self) -> list[str]:
        """
        Batocera emulator process names to kill after each ROM test.

        Derived from the configgen generators directory and confirmed
        against installed binaries. Case must match exactly — pkill -f
        is case-sensitive on Linux.

        Covers ARM builds (RPi, Odroid etc) and x86 builds. Processes
        not installed on a given device are silently ignored by pkill/killall.
        """
        return [
            'retroarch',         # RetroArch / libretro — all systems
            'amiberry',          # Amiga
            'cannonball',        # Out Run engine
            'devilutionx',       # Diablo engine
            'dosbox',            # DOS (matches dosbox, dosbox-staging, dosbox-x)
            'drastic',           # Nintendo DS
            'duckstation-nogui', # PlayStation 1
            'easyrpg-player',    # RPG Maker
            'eduke32',           # Duke Nukem 3D / Build engine
            'flycast',           # Dreamcast / Naomi / Atomiswave
            'GSplus',            # Apple II / IIgs
            'gzdoom',            # Doom / Heretic / Hexen
            'hatari',            # Atari ST / STE / TT / Falcon
            'hypseus',           # Daphne laser disc
            'moonlight',         # Game streaming
            'mupen64plus',       # Nintendo 64
            'OpenBOR',           # Beat em ups (matches OpenBOR4432/6412/7142)
            'PPSSPP',            # PSP
            'scummvm',           # SCUMM / point-and-click adventures
            'SDLPoP',            # Prince of Persia
            'solarus-run',       # Solarus engine
            'sonic2013',         # Sonic the Hedgehog
            'soniccd',           # Sonic CD
            'supermodel',        # Sega Model 3
            'x64',               # VICE — Commodore 64
            'x128',              # VICE — Commodore 128
            'xvic',              # VICE — VIC-20
            # x86-only / newer Batocera versions
            'dolphin-emu',       # Nintendo GameCube / Wii
            'pcsx2',             # PlayStation 2
            'rpcs3',             # PlayStation 3
            'ryujinx',           # Nintendo Switch
            'yuzu',              # Nintendo Switch (alt)
            'xemu',              # Xbox
            'vita3k',            # PlayStation Vita
            'redream',           # Dreamcast (alt)
            'melonDS',           # Nintendo DS (alt)
        ]

    # ------------------------------------------------------------------
    # Autofix
    # ------------------------------------------------------------------

    # Arcade manufacturer subcategories that Batocera ES displays as
    # separate systems but emulatorlauncher only knows as their parent.
    # Confirmed from es-theme-carbon assets and Batocera launch behaviour.
    # When a ROM lives in one of these folders, we pass the parent system
    # name to emulatorlauncher while keeping the original ROM file path.
    SYSTEM_ALIAS_MAP = {
        'capcom':   'fbneo',
        'neogeo':   'fbneo',
        'konami':   'fbneo',
        'taito':    'fbneo',
        'cave':     'fbneo',
        'dataeast': 'fbneo',
        'snk':      'fbneo',
        'irem':     'fbneo',
        'sega':     'fbneo',     # Sega arcade hardware (not megadrive)
        'psikyo':   'fbneo',
        'toaplan':  'fbneo',
        'cps':      'fbneo',     # Capcom Play System (generic)
        'cps1':     'fbneo',     # CPS1 subcategory
        'cps2':     'fbneo',     # CPS2 subcategory
        'cps3':     'fbneo',     # CPS3 subcategory
        'igs':      'fbneo',     # PolyGame Master / IGS arcade hardware
    }

    def build_launch_cmd(self, system: str, rom: str) -> list[str]:
        """
        Build the emulatorlauncher command for a ROM.

        Maps arcade manufacturer subcategory folder names to their
        parent system before passing to emulatorlauncher. The ROM
        file path is passed unchanged — only the -system argument
        is remapped.

        Args:
            system: System folder name (e.g. 'capcom', 'fbneo', 'mame')
            rom:    Full path to the ROM file

        Returns:
            Complete command list for subprocess.Popen.
        """
        launch_system = self.SYSTEM_ALIAS_MAP.get(system, system)
        if launch_system != system:
            log(f"  System alias: [{system}] → [{launch_system}]")
        return self.get_launcher_cmd() + ["-system", launch_system, "-rom", rom]

    def get_configured_core(self, system: str, romname: str) -> str:
        """
        Resolve which core this ROM would actually launch with, given
        the CURRENT batocera.conf — without launching anything.

        Checks the per-game entry first (system["romname"].core=X,
        the same key format write_game_entries() produces), since
        that takes precedence over the global the same way
        emulatorlauncher itself resolves it. Falls back to the
        system-wide global (system.core=X) if no per-game entry
        exists. Returns '' if neither is present — true default
        applies, and for 'mame' specifically that's confirmed to be
        mame078plus (per es_systems.cfg), not fbneo, so '' correctly
        means "not a concern" rather than "unknown, assume the worst".

        Exists so callers can force a verification screenshot BEFORE
        a regular (non-autofix) test, not just within the autofix
        loop — see the base class docstring for why this matters.
        """
        launch_system = self.SYSTEM_ALIAS_MAP.get(system, system)
        try:
            with open(self.conf_path, 'r') as f:
                lines = f.readlines()
        except Exception:
            return ''

        pergame_pattern = re.compile(
            rf'^{re.escape(launch_system)}\["{re.escape(romname)}"\]\.core=(.+)$'
        )
        global_pattern = re.compile(
            rf'^{re.escape(launch_system)}\.core=(.+)$'
        )

        global_value = ''
        for line in lines:
            stripped = line.strip()
            m = pergame_pattern.match(stripped)
            if m:
                return m.group(1)
            m = global_pattern.match(stripped)
            if m:
                global_value = m.group(1)

        return global_value

    def on_successful_test(
        self,
        system: str,
        romname: str,
        combined: str
    ) -> None:
        """
        Write a per-game batocera.conf entry when a ROM passes its
        test with a global override suspended.

        When mame.core=fbneo is suspended for testing, a ROM that passes
        used a different core. Without a per-game entry, restoring the
        global override would make the game fail again from ES.

        Parses the emulatorlauncher log to find which core was actually
        loaded, then writes:
            system["romname"].core=detected_core
            system["romname"].emulator=libretro

        Args:
            combined: stdout AND stderr concatenated — was previously
                just stdout alone, which meant this regex could only
                ever match if emulatorlauncher's runCommand debug line
                happened to land in stdout specifically. Python's
                logging module defaults to stderr for exactly this kind
                of line, and every OTHER check in run_test() already
                uses the combined content — this one just hadn't been
                brought in line with that.

        If the core still can't be parsed even from the combined log
        (a genuine anomaly worth a second look, not just a stdout/
        stderr mixup), the logs are preserved via save_error_logs()
        rather than silently lost — without this, a normal OK result
        triggers clear_error_logs() moments later in record_result(),
        which would otherwise wipe the exact evidence needed to
        diagnose why the parse failed. A small marker file is written
        alongside so clear_error_logs() knows to leave the whole
        folder alone, the same way it already does for a screenshot.
        """
        suspended = getattr(self, '_suspended_overrides', {})
        if system not in suspended:
            return

        # Parse which libretro core was actually used from the log.
        # Matches the path pattern alone rather than anchoring on the
        # exact "-L " prefix syntax — emulatorlauncher logs the launch
        # command in two different formats on separate lines (plain,
        # space-separated, AND a debug-level Python list repr where
        # "-L" and the path are separated by "', '" not a space), and
        # anchoring on "-L " specifically only ever matched the first.
        # The path itself doesn't change between the two; only the
        # surrounding quote/comma syntax does.
        m = re.search(
            r'/usr/lib/libretro/(\w+)_libretro\.so',
            combined
        )
        if not m:
            log(f'  on_successful_test: could not parse core from log '
                f'for {romname} — no per-game entry written')
            try:
                from modules.common import filehandling
                filehandling.save_error_logs(
                    self.error_log_base, system, romname,
                    self.stdout_log, self.stderr_log
                )
                marker_dir = os.path.join(self.error_log_base, system, romname)
                os.makedirs(marker_dir, exist_ok=True)
                with open(os.path.join(marker_dir, 'PARSE_FAILED.txt'), 'w') as f:
                    f.write(
                        'on_successful_test() could not find a '
                        '"-L /usr/lib/libretro/X_libretro.so" line in '
                        'this ROM\'s log, with a global override '
                        'suspended for this system. The result was '
                        'still OK overall, so no per-game entry was '
                        'written — restoring the suspended global '
                        'override may cause this ROM to behave '
                        'differently than it did during this test. '
                        'Logs preserved here for investigation.\n'
                    )
                log(f'  Logs preserved for investigation: '
                    f'{self.error_log_base}/{system}/{romname}/')
            except Exception as e:
                log(f'  Warning: could not preserve logs for '
                    f'investigation: {e}')
            return

        core_used = m.group(1)
        suspended_core = suspended.get(system, {}).get('core', '')

        if core_used == suspended_core:
            # Same core as the suspended override — no entry needed
            return

        configeditor.write_game_entries(
            self.conf_path, system, romname,
            core_used, 'libretro'
        )
        log(
            f'  Per-game entry written: [{system}] {romname} '
            f'-> core={core_used} (global {suspended_core} suspended)'
        )

    def pre_test_run(self, systems: set) -> None:
        """
        Suspend global system overrides before testing begins.

        Global entries like mame.core=fbneo in batocera.conf cause
        silent false-OK results when a core exits cleanly without
        logging errors for ROMs it cannot run. Suspending them ensures
        each ROM is tested against the per-game or default core chain
        where genuine failures are logged and detected correctly.

        The suspended state is stored on the instance so post_test_run
        can restore it without needing any external coordination.
        """
        self._suspended_overrides = {}

        # Clean up any #ROMAUDIT# markers left by a previous run that
        # never reached post_test_run() — e.g. killed abruptly rather
        # than interrupted cleanly. Must happen before this session's
        # own suspend below, or a stale marker for a system THIS
        # session doesn't touch would sit there indefinitely.
        stale = configeditor.cleanup_stale_markers(self.conf_path)
        if stale:
            log(f'  Found and restored {stale} stale override marker'
                f'{"s" if stale != 1 else ""} from a previous '
                f'interrupted run.')

        log(f'pre_test_run: checking {len(systems)} system(s) for global overrides: {systems}')
        found = configeditor.get_system_overrides(self.conf_path, systems)
        if not found:
            log('pre_test_run: no global overrides found — nothing to suspend')
            return
        for system, vals in found.items():
            parts = ', '.join(f'{k}={v}' for k, v in vals.items())
            log(f'  Suspending global override: [{system}] {parts}')
        self._suspended_overrides = configeditor.suspend_system_overrides(
            self.conf_path, systems
        )
        log(
            f'  Global overrides suspended for testing '
            f'({len(self._suspended_overrides)} system(s)). '
            f'Will restore after audit.'
        )

    def post_test_run(self) -> None:
        """
        Restore any global overrides suspended by pre_test_run().

        Called in the finally block — guaranteed to run even if the
        audit is interrupted or raises an exception. Safe to call
        when nothing was suspended (no-op).
        """
        suspended = getattr(self, '_suspended_overrides', {})
        if not suspended:
            return
        configeditor.restore_system_overrides(self.conf_path, suspended)
        for system, vals in suspended.items():
            parts = ', '.join(f'{k}={v}' for k, v in vals.items())
            log(f'  Restored global override: [{system}] {parts}')
        self._suspended_overrides = {}

    def attempt_autofix(
        self,
        system: str,
        rom: str,
        romname: str,
        dashboard,
        state: dict,
        original_error: str = '',
        **kwargs
    ) -> tuple[str, str]:
        """
        Attempt to fix a failing ROM by trying known core/emulator
        combinations via batocera.conf per-game overrides.

        Maps system aliases before combination lookup so that arcade
        subcategory folders (capcom, cave etc.) correctly resolve to
        fbneo fix combinations rather than returning NO COMBINATIONS.

        Uses the autofixer module to iterate through defined combinations
        for the system, writing each to batocera.conf and testing.
        Backs up batocera.conf before the first fix attempt.

        Returns:
            ('FIXED', notes)           - Working combination found
            ('GENUINE ERROR', notes)   - All combinations failed
            ('NO COMBINATIONS', notes) - System not supported or no cores
        """
        # Map alias systems before combination lookup
        launch_system   = self.SYSTEM_ALIAS_MAP.get(system, system)
        installed_cores = kwargs.get('installed_cores')
        heuristic       = kwargs.get('heuristic', False)
        combinations    = autofixer.get_combinations(
            launch_system, installed_cores
        )

        if not combinations:
            return (
                'NO COMBINATIONS',
                f"No autofix combinations for [{system}]"
            )

        # Backup batocera.conf once per session (caller tracks this)
        if not kwargs.get('conf_backed_up'):
            configeditor.backup_conf(self.conf_path)

        # A NEEDS REVIEW from UNVERIFIED_CORES (fbneo grey-screen) should
        # proceed to the combination loop — the core masked the failure and
        # a different core might genuinely work.
        # Dump-quality NEEDS REVIEW also proceeds — the autofix loop's own
        # per-combination dump-quality handling (via verify_dump_quality)
        # will correctly accept confirmed-content results and reject blanks.

        fix_status, fix_notes, _ = autofixer.attempt_autofix(
            launch_system, rom, romname,
            self.conf_path,
            self,
            dashboard,
            state,
            installed_cores,
            heuristic=heuristic,
        )

        if fix_status in ('FIXED', 'NEEDS REVIEW'):
            return fix_status, fix_notes
        return 'GENUINE ERROR', fix_notes

    def get_installed_cores(self) -> set | None:
        """
        Return the set of installed libretro core names by scanning
        this platform's core directory. Used to filter autofix
        combinations to only those whose core is actually installed.
        """
        return autofixer.get_installed_cores(self.libretro_core_path)

    def log_autofix_availability(
        self,
        installed_cores: set = None
    ) -> None:
        """Log available autofix combinations for this platform."""
        if installed_cores is None:
            installed_cores = autofixer.get_installed_cores(
                self.libretro_core_path
            )
        log("=" * 60)
        log(f"Batocera autofix core availability: "
            f"{len(installed_cores)} installed libretro core(s).")
        autofixer.log_available_combinations(installed_cores)
        log("=" * 60)

    # ------------------------------------------------------------------
    # Launcher and environment
    # ------------------------------------------------------------------

    def get_launcher_cmd(self) -> list[str]:
        """
        Return the base emulatorlauncher command for this Batocera version.

        The system name and ROM path arguments are appended by the caller,
        so this returns only the interpreter and module/script components.

        Returns:
            Command list ready for subprocess.Popen, e.g.:
            ['/usr/bin/python3', '-m', 'configgen.emulatorlauncher']
        """
        return self._launcher_cmd

    def get_post_kill_delay(self, system: str) -> float:
        """
        Seconds to wait after the emulator is confirmed dead, before
        reading its logs for the final verdict.

        dreamcast and its arcade-board siblings get longer: Recalbox's
        own kill_emulators() override documents the identical exit-
        code-1 symptom on RPi Zero/GPi Case, caused by EGL/GLES
        graphics context release needing real time after a kill — not
        waiting produces exactly this. Confirmed in practice here:
        Carrier (USA).chd ran the full 12s display window successfully
        (genuinely playing, not still loading — ruling out the earlier
        "killed too early" theory) and still hit "Exiting configgen
        with status 1" right at the kill, going through the real
        emulatorlauncher.py wrapper exactly as the audit does. Recalbox's
        confirmed fix for the same symptom was a flat 2s wait.
        """
        FLYCAST_POST_KILL_DELAY = 2.0
        if system in ('dreamcast', 'naomi', 'naomi2', 'atomiswave'):
            return FLYCAST_POST_KILL_DELAY
        return super().get_post_kill_delay(system)

    def get_display_time(self, system: str) -> float:
        """
        Seconds to display a launched game before killing it.

        Default (the base DISPLAY_TIME constant, 3s) is fine for most
        Batocera systems. dreamcast and its arcade-board siblings
        (naomi, naomi2, atomiswave — same Flycast engine) get longer:
        confirmed in practice that killing Flycast at the 3s mark, while
        it's likely still mid-way through mounting/parsing a large CHD,
        produces a genuine "Non-zero exit from launcher" crash rather
        than a graceful shutdown — a manual run of the identical ROM
        that genuinely ran for 25s before being interrupted shut down
        cleanly, and a UI-launched example that ran ~10s before exiting
        also exited with status 0. Interrupting mid-load looks like the
        actual trigger, not anything our own timing/kill logic decides
        after the fact — no amount of waiting after the kill would fix
        this, since by then Flycast has already crashed; the fix has
        to be giving it enough time before the kill happens at all.

        naomi/naomi2/atomiswave already had a 6s *screenshot* delay
        hint elsewhere in this codebase for the same underlying engine
        — this extends the same reasoning to the actual display window,
        not just screenshot timing.
        """
        FLYCAST_DISPLAY_TIME = 12
        if system in ('dreamcast', 'naomi', 'naomi2', 'atomiswave'):
            return FLYCAST_DISPLAY_TIME
        return super().get_display_time(system)

    def get_working_dir(self) -> str:
        """
        Return /userdata — matching where EmulationStation runs from.

        MAME and other cores use the working directory as a fallback
        when resolving relative paths in rompath configuration. Using
        /userdata ensures emulator behaviour matches ES launches.
        """
        return '/userdata'

    def get_env(self) -> dict[str, str]:
        """
        Return the environment variables needed to launch a ROM.

        Builds on a copy of the current environment, adding the display
        server variables required for RetroArch to connect to the active
        Wayland session, and any extra variables needed for the detected
        Batocera version (e.g. PYTHONPATH for v38).

        Matches the EmulationStation launch environment as closely as
        possible so emulator behaviour (including MAME rompath resolution)
        is consistent between ES and tool launches.

        Returns:
            Complete environment dictionary for subprocess.Popen.
        """
        env = os.environ.copy()
        env.update({
            'DISPLAY':          ':0',
            'WAYLAND_DISPLAY':  'wayland-1',
            'XDG_RUNTIME_DIR':  '/var/run',
            'XDG_SESSION_TYPE': 'wayland',
            'HOME':             '/userdata/system',
            'SDL_NOMOUSE':      '1',
            'LANGUAGE':         '',
        })
        env.update(self._extra_env)
        return env
