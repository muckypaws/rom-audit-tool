"""
Abstract base class for platform implementations.

Defines the interface that all platform modules must implement.
Each supported emulation system (Batocera, RetroPie, etc.) provides
a concrete subclass of Platform with system-specific paths, launcher
commands, environment variables, and log analysis markers.

Adding support for a new platform requires only:
    1. Creating a new file in modules/platforms/
    2. Subclassing Platform and implementing all abstract properties
       and methods
    3. Adding a detection condition in modules/common/detection.py

No other files need to change.
"""

from __future__ import annotations  # Python 3.7 compatibility

import os
import re
import signal
import subprocess
import time

from abc import ABC, abstractmethod

# Cached ffmpeg filter availability — queried once per process, not per
# screenshot. Avoids repeated subprocess calls on long screenshot runs.
_FFMPEG_HAS_DRAWTEXT: bool | None = None
_FFMPEG_HAS_ASS:      bool | None = None


def _check_ffmpeg_filters() -> tuple[bool, bool]:
    """Return (has_drawtext, has_ass), cached after the first call."""
    global _FFMPEG_HAS_DRAWTEXT, _FFMPEG_HAS_ASS
    if _FFMPEG_HAS_DRAWTEXT is None:
        try:
            r = subprocess.run(
                ['ffmpeg', '-filters'],
                capture_output=True, timeout=5
            )
            _FFMPEG_HAS_DRAWTEXT = b'drawtext' in r.stdout
            _FFMPEG_HAS_ASS      = b' ass '    in r.stdout
        except Exception:
            _FFMPEG_HAS_DRAWTEXT = False
            _FFMPEG_HAS_ASS      = False
    return _FFMPEG_HAS_DRAWTEXT, _FFMPEG_HAS_ASS
from modules.common import detection
from modules.common import filehandling
from modules.common.logging import log


# ---------------------------------------------------------------------------
# Constants used by Platform.run_test()
# ---------------------------------------------------------------------------

MAX_WAIT       = 20     # Default launch timeout (seconds)
CHECK_INTERVAL = 0.5    # Poll interval during Phase 1
DISPLAY_TIME   = 3      # Seconds to show game before kill
POST_KILL_FLUSH_DELAY = 0.3   # Grace period after proc.wait() confirms
                               # the process has exited, before reading
                               # its logs for the final verdict. proc.wait()
                               # only guarantees the PID we tracked has
                               # terminated — not that every line a core
                               # logs near shutdown (e.g. a threaded video
                               # stats line reported from a worker thread)
                               # has actually landed on disk yet. Confirmed
                               # in practice: a Game & Watch ROM's "Frames
                               # pushed: 31" line was genuinely missing at
                               # the moment of the read during the actual
                               # test, but present moments later when the
                               # log file was checked by hand — exactly the
                               # shape of a narrow flush race, not a logic
                               # bug in the frames-aware check itself.
OK_RECHECK_DELAY = 1.0        # Before finalising an OK result, re-read the
                               # logs once more after this delay and re-run
                               # parse_error() — confirmed case: fpoint1.zip
                               # (missing ROM files) was correctly caught as
                               # ERROR when --screenshot was active (which
                               # incidentally delays the kill signal, since
                               # take_screenshot() runs before
                               # kill_emulators() only in that path), but
                               # silently passed as OK on an otherwise
                               # identical run without it — the only
                               # structural difference between the two is
                               # that incidental delay. An OK shouldn't
                               # depend on an unrelated flag happening to
                               # buy enough time for error text to land.
                               # One extra read is deliberately cheap —
                               # this runs on every result that would
                               # otherwise be OK, the large majority of any
                               # audit, so the cost has to stay small; a
                               # genuinely broken ROM silently reported OK
                               # is a worse outcome than this delay.

# ---------------------------------------------------------------------------

class Platform(ABC):
    """
    Abstract base class defining the interface all platform
    implementations must fulfil.

    The common detection algorithms (is_launched, parse_error) live in
    modules/common/detection.py. Platform subclasses supply the matching
    criteria via properties, keeping the algorithm in one place while
    the data remains platform-specific.

    Subclasses may override is_launched() and parse_error() directly
    if a platform requires fundamentally different matching logic rather
    than just different string criteria.
    """

    # ------------------------------------------------------------------
    # Abstract properties - paths and configuration
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable platform name (e.g. 'Batocera v43')."""
        ...

    @property
    def version(self) -> str:
        """
        Platform version string for display in logs and dashboard.
        Override in subclasses. Returns empty string by default.
        """
        return ''

    @property
    def display_name(self) -> str:
        """Name + version for display e.g. 'Recalbox 9.2.3-Pulstar'."""
        v = self.version
        return f"{self.name} {v}".strip() if v else self.name

    def get_rom_display_name(self, system: str, rom_path: str) -> str:
        """
        Return a human-readable name for a ROM path.

        The default is the file's basename, which is correct for every
        standard system. Override for systems where the filename is
        opaque (e.g. ports, where pak0.pak should display as 'Quake').

        Args:
            system:   System name (e.g. 'ports', 'mame').
            rom_path: Full path to the ROM file.

        Returns:
            Display name string.
        """
        return os.path.basename(rom_path)

    def get_launch_timeout(self, system: str) -> int | None:
        """
        Return launch detection timeout in seconds for a system.

        Return None to use the global MAX_WAIT default. Override in
        platform subclasses to add platform-specific slow systems.

        Args:
            system: System folder name e.g. 'zxspectrum', 'c64'.

        Returns:
            Timeout in seconds, or None for the global default.
        """
        # Cross-platform slow-loading systems (tape/disk based)
        CROSS_PLATFORM_TIMEOUTS: dict[str, int] = {
            'c64':          120,
            'zxspectrum':   180,
            'amstradcpc':   120,
            'msx':           90,
            'bbcmicro':      90,
            'pcenginecd':    60,
            'amiga':         30,
            'psp':           10,
            'n64':           10,
            'c20':           10,
        }
        return CROSS_PLATFORM_TIMEOUTS.get(system)

    def get_display_time(self, system: str) -> float:
        """
        Return how long to display a launched game before killing it,
        in seconds, for a given system.

        Default implementation ignores system and returns the module
        constant DISPLAY_TIME, preserved via getattr(self, 'display_time',
        DISPLAY_TIME) for backward compatibility with any platform that
        still defines display_time as a flat property rather than
        overriding this method. Override this method directly in a
        platform subclass to vary it per system — see RetroPiePlatform
        for the pattern, mirroring get_launch_timeout() above.

        This matters for more than just "how long does the test take" —
        it's also the only window during which whatever the core itself
        is going to log gets a chance to actually happen. Confirmed in
        practice: a Game & Watch ROM (lr-gw core) genuinely needs several
        seconds of running before RetroArch's own "Content ran for..."
        timer starts counting at all — killed before that point, the
        timer is permanently zero (not delayed, not unflushed — it
        simply never started), even though frames were already being
        pushed (a loading/splash phase, not "real" content yet by
        RetroArch's own internal tracking). No amount of waiting after
        the kill fixes this, since the value was already finalised the
        moment the process received the signal — the fix has to be
        giving the system enough time before that point, not after it.

        Args:
            system: System folder name e.g. 'gameandwatch', 'n64'.

        Returns:
            Display time in seconds.
        """
        return getattr(self, 'display_time', DISPLAY_TIME)

    def get_post_kill_delay(self, system: str) -> float:
        """
        Seconds to wait after the emulator process is confirmed dead,
        before reading its logs for the final verdict.

        Default is POST_KILL_FLUSH_DELAY (0.3s) — covers ordinary
        flush-timing slack, sufficient for most systems. Override per
        system where there's a confirmed, specific need for more —
        see Recalbox's own kill_emulators() override for a documented
        precedent: EGL/GLES graphics context release on RPi Zero/GPi
        Case needs real time, and not waiting produces exit code 1 —
        the identical symptom seen on Batocera with Flycast-family
        systems under investigation as of this writing.
        """
        return POST_KILL_FLUSH_DELAY

    def post_process_result(
        self,
        status: str,
        notes: str,
        system: str,
        romname: str,
        screenshot_path: str | None,
        configured_core: str,
        heuristic: bool,
        dashboard,
        state: dict,
    ) -> tuple[str, str, str | None]:
        """
        Post-process a result from run_test() before it is recorded.

        Called by rom_audit.py's orchestration layer immediately after
        run_test() returns, before autofix is considered. Centralises
        the two post-run checks that were previously duplicated at both
        call sites (--test path and main loop):

          1. UNVERIFIED_CORES check (OK results): if the configured
             core is in UNVERIFIED_CORES, the result cannot be trusted
             from log analysis alone — routes through
             verify_unverified_core() to force a screenshot and apply
             the appropriate NEEDS REVIEW / confirmed-clear verdict.

          2. Dump-quality sentinel (IMPERFECT results tagged
             [DUMP_QUALITY]): the sentinel means run_test() found a
             dump-quality marker whose load status is ambiguous — game
             may or may not have loaded. Routes through
             verify_dump_quality() which uses a screenshot heuristic
             to resolve to IMPERFECT (content confirmed), ERROR
             (blank/grey confirmed), or NEEDS REVIEW (no heuristic).
             The sentinel is stripped here before the CSV sees it.

        rom_audit.py has no business knowing about the sentinel format
        or which core names are in UNVERIFIED_CORES — both are
        implementation details of the detection/autofix layer. This
        method is the single point of contact between run_test()'s raw
        output and the recording layer.

        The base-class implementation handles both checks fully:
        verify_unverified_core() is a no-op when configured_core is
        not in UNVERIFIED_CORES, and the [DUMP_QUALITY] sentinel only
        ever appears when dump_quality_markers are defined (currently
        Batocera only) — so no platform override is needed.

        Returns:
            (status, notes, screenshot_path) — all three may differ
            from the inputs. screenshot_path may be updated if a forced
            screenshot path was created for the dump-quality check.
        """
        from modules.common import autofix as autofixer

        # Check 1 — UNVERIFIED_CORES
        if status == 'OK':
            verify_status, verify_suffix = autofixer.verify_unverified_core(
                configured_core, screenshot_path,
                heuristic, dashboard, state
            )
            notes += verify_suffix
            if verify_status:
                status = verify_status

        # Check 2 — dump-quality sentinel
        if status == 'IMPERFECT' and notes.startswith('[DUMP_QUALITY]'):
            if not screenshot_path:
                import os
                from modules.common.filehandling import _safe_path_component
                shot_dir = os.path.join(
                    self.error_log_base,
                    _safe_path_component(system),
                    _safe_path_component(romname)
                )
                try:
                    os.makedirs(shot_dir, exist_ok=True)
                    screenshot_path = os.path.join(
                        shot_dir,
                        f'{_safe_path_component(system)}_'
                        f'{_safe_path_component(romname)}_review.png'
                    )
                except Exception:
                    screenshot_path = None

            dq_status, dq_suffix = autofixer.verify_dump_quality(
                notes, screenshot_path, heuristic, dashboard, state
            )
            notes = notes.replace('[DUMP_QUALITY] ', '', 1)
            notes += dq_suffix
            if dq_status:
                status = dq_status

        return status, notes, screenshot_path

    def prepare_screenshot_path(
        self,
        system: str,
        romname: str,
        screenshot: bool,
        screenshot_flat: bool,
        configured_core: str,
        heuristic: bool = False,
    ) -> str | None:
        """
        Resolve the screenshot path to pass into run_test(), including
        forced-path overrides that must be decided before the test runs.

        Two cases force a screenshot regardless of --screenshot:

        1. UNVERIFIED_CORES: the core may silently mask failures, so a
           screenshot is always taken for post-test pixel analysis.

        2. --heuristic active: dump-quality markers (NO GOOD DUMP KNOWN,
           ROM NEEDS REDUMP etc.) can't be predicted before run_test()
           returns, but when one appears post_process_result() needs a
           real screenshot to analyse. Forcing one here for every ROM
           when --heuristic is active ensures it's available. Without
           this, verify_dump_quality() always gets a path that points to
           a file that was never written (the emulator is already dead
           by the time post_process_result() runs).

        Returns:
            Full path string, or None if no screenshot should be taken.
        """
        from modules.common import autofix as autofixer
        import os
        from modules.common.filehandling import _safe_path_component

        regular_path = self.screenshot_path(
            screenshot, screenshot_flat, system, romname
        )

        if regular_path:
            return regular_path

        safe_sys = _safe_path_component(system)
        safe_rom = _safe_path_component(romname)
        shot_dir = os.path.join(
            self.error_log_base, safe_sys, safe_rom
        )

        if configured_core in autofixer.UNVERIFIED_CORES:
            try:
                os.makedirs(shot_dir, exist_ok=True)
                return os.path.join(
                    shot_dir, f'{safe_sys}_{safe_rom}_review.png'
                )
            except Exception:
                return None

        if heuristic:
            # Force a screenshot so dump-quality post-processing has
            # something real to analyse — we can't know pre-test whether
            # a dump-quality marker will appear, but if it does we need
            # an actual image, not just a path to a non-existent file.
            try:
                os.makedirs(shot_dir, exist_ok=True)
                return os.path.join(
                    shot_dir, f'{safe_sys}_{safe_rom}_review.png'
                )
            except Exception:
                return None

        return None

    def interpret_fix_result(
        self,
        fix_status: str,
        fix_notes: str,
    ) -> tuple[str, str, bool]:
        """
        Translate an attempt_autofix() result into recording terms.

        attempt_autofix() returns 'FIXED' to mean "a working
        combination was found and written to config" — but the CSV
        status for a successfully fixed ROM is 'OK' (it now runs),
        while the fix description lives in the notes. rom_audit.py
        previously made this mapping decision inline at both call sites,
        which is platform/autofix-layer knowledge leaking into the
        orchestration layer.

        Returns:
            (csv_status, notes, count_as_fixed) where count_as_fixed
            signals whether to increment the dashboard's FIXED counter
            separately from the OK counter.
        """
        if fix_status == 'FIXED':
            return 'OK', fix_notes, True
        # NEEDS REVIEW, GENUINE ERROR, NO COMBINATIONS — pass through
        return fix_status, fix_notes, False

    def get_installed_cores(self) -> set | None:
        """
        Return the set of installed libretro core names for this
        platform, or None if the platform doesn't need core filtering.

        Called once before the audit loop begins when --autofix is
        active, so the result can be reused per-ROM without re-scanning
        the filesystem each time.

        Base implementation returns None (no filtering needed).
        Override on platforms that install cores to a known directory
        (e.g. Batocera's /usr/lib/libretro/).
        """
        return None

    def get_screenshot_delay_hint(self, system: str) -> int:
        """
        Return suggested screenshot delay in seconds for a system.

        Systems with long BIOS splash screens or engine init phases
        need extra delay after the launch indicator fires before a
        useful screenshot can be captured.

        Args:
            system: System folder name.

        Returns:
            Delay in seconds. 0 = no additional delay needed.
        """
        CROSS_PLATFORM_DELAYS: dict[str, int] = {
            'neogeo':      8,
            'naomi':       6,
            'naomi2':      6,
            'atomiswave':  6,
            'n64':         5,
            'psp':         8,
            'mame':        5,
            'gameandwatch': 4,
            'model2':      6,
            'model3':      6,
        }
        return CROSS_PLATFORM_DELAYS.get(system, 0)

    @property
    def screenshot_warmup(self) -> int:
        """
        Platform-level base delay added before every screenshot.

        Some platforms show a loading screen between the launch indicator
        firing and the game actually appearing on screen. This warmup
        is added automatically to any user-specified --screenshot-delay
        so screenshots are taken after the loading screen clears.

        Returns 0 by default — override in platforms that need it.
        """
        return 0

    def log_autofix_availability(self) -> None:
        """
        Log available autofix combinations at the start of a session.
        No-op by default — override in platforms that support autofix.
        """
        pass

    def validate_rom_launch(
        self,
        system: str,
        rom: str
    ) -> tuple[bool, str]:
        """
        Validate that a ROM can be launched before attempting it.

        Called by run_test before spawning the subprocess. Returning
        False causes the ROM to be recorded as MISSING CORE immediately
        rather than attempting a launch that will fail.

        Base implementation always returns True (proceed). Override in
        platform subclasses that have pre-launch validation.

        Args:
            system: System folder name.
            rom:    Full ROM path.

        Returns:
            (can_launch, reason) — False + reason string if invalid.
        """
        return True, ''

    def get_configured_core(self, system: str, romname: str) -> str:
        """
        Resolve which core/emulator THIS specific ROM would actually
        launch with, given current config — without launching it.

        Exists specifically so callers can check, BEFORE testing,
        whether a result is going to come from a core in
        UNVERIFIED_CORES (see autofix.py) and force a verification
        screenshot proactively. Without this, a ROM with an existing
        per-game config entry pointing at an unverified core (written
        by a previous autofix run, or set up by hand) sails through
        the regular non-autofix test path with a plain, untrusted OK
        and no flag at all — confirmed in practice: bandit.zip showed
        a grey error screen and reported plain OK on a regular
        --test/--recheck run, because the verification logic only
        ever lived inside the autofix loop, never the everyday path
        most ROMs actually go through.

        Base implementation returns '' (unknown/not applicable) —
        most platforms don't need this, since build_launch_cmd()
        either resolves the core itself (Recalbox, RetroPie) where
        the launch command's contents already tell you what's being
        used, or there's no equivalent masking risk identified yet.
        Override only where there's a genuine, demonstrated need.

        Args:
            system:  System folder name.
            romname: ROM filename.

        Returns:
            The resolved core name, or '' if unknown/not applicable.
        """
        return ''

    def prepare_autofix(self) -> None:
        """
        Called once before the autofix loop begins.

        Platforms should initialise any state needed across multiple
        autofix calls here (e.g. fetching installed cores, backing up
        config files). No-op by default.
        """
        pass

    @property
    @abstractmethod
    def roms_path(self) -> str:
        """Base path where all system ROM folders live."""
        ...

    @property
    @abstractmethod
    def stdout_log(self) -> str:
        """
        Path to the log file to monitor for launch indicators.

        For Batocera this is the file the subprocess stdout is redirected
        to. For RetroPie this is the fixed runcommand.sh log path at
        /dev/shm/runcommand.log. See also: subprocess_capture.
        """
        ...

    @property
    @abstractmethod
    def stderr_log(self) -> str:
        """
        Path to the log file to monitor for error indicators.

        For Batocera this is the file the subprocess stderr is redirected
        to. For RetroPie this is the same fixed runcommand.sh log path
        as stdout_log since all output goes to one file.
        """
        ...

    @property
    @abstractmethod
    def results_csv(self) -> str:
        """Path to the audit results CSV file."""
        ...

    @property
    @abstractmethod
    def log_file(self) -> str:
        """Path to the persistent audit CLI log file."""
        ...

    @property
    @abstractmethod
    def pid_file(self) -> str:
        """Path to the PID file used to prevent duplicate runs."""
        ...

    @property
    @abstractmethod
    def error_log_base(self) -> str:
        """Root path for archived error logs."""
        ...

    @property
    def conf_path(self) -> str:
        """
        Full path to the platform's emulator configuration file.

        Used by the autofix module to write per-game core/emulator
        overrides. Returns empty string by default for platforms that
        do not support config-based autofix.

        Override in platform subclasses that support autofix.
        """
        return ""

    # ------------------------------------------------------------------
    # Abstract properties - log analysis criteria
    # These are injected into the common detection functions, keeping
    # the algorithm in common/ and the data in the platform class.
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def launch_indicators(self) -> list[str]:
        """
        List of strings in the log that indicate a successful launch.
        Passed to common.detection.is_launched().
        """
        ...

    @property
    @abstractmethod
    def error_markers(self) -> list[str]:
        """
        List of strings in the log that indicate a specific error.
        Passed to common.detection.parse_error().
        """
        ...

    @property
    @abstractmethod
    def exit_marker(self) -> str:
        """
        String indicating a non-zero launcher exit.
        Passed to common.detection.parse_error().
        Set to empty string if the platform does not produce one.
        """
        ...

    # ------------------------------------------------------------------
    # Concrete properties with default implementations
    # Override in subclass if the platform requires different behaviour.
    # ------------------------------------------------------------------

    @property
    def additional_roms_paths(self) -> list[str]:
        """
        Additional ROM base paths to scan alongside roms_path.

        Some platforms (Recalbox) split ROMs across multiple locations —
        user content in one path and built-in/factory ROMs in another.
        Override in platform subclasses to add extra paths.

        Returns:
            List of additional base paths. Empty list by default.
        """
        return []

    @property
    def libretro_core_path(self) -> str:
        """Path to installed libretro core .so files."""
        return "/usr/lib/libretro"

    @property
    def faulty_roms_path(self) -> str:
        """Quarantine folder for faulty ROMs after cleanup."""
        return "/tmp/faultyroms"
    
    @property
    def subprocess_capture(self) -> bool:
        """
        Whether to redirect subprocess stdout/stderr to the log files.

        True (default) for platforms like Batocera where the launcher
        writes useful output to its stdout/stderr which we capture.

        False for platforms like RetroPie where the launcher writes to
        a fixed log file path regardless of stdout/stderr redirection.
        When False, run_test() redirects subprocess output to /dev/null
        and clears/monitors stdout_log directly instead.
        """
        return True

    @property
    def post_launch_error_markers(self) -> list[str]:
        """
        Markers indicating a fatal error even after a successful launch.

        Checked only when launched=True to catch games that display an
        error screen rather than actually playing. Intentionally stricter
        than error_markers — non-fatal operational warnings must not
        appear here.

        Returns empty list by default — subclasses override as needed.
        """
        return []
    
    @property
    def bios_error_markers(self) -> list[str]:
        """
        Strings in the log indicating a required BIOS file is missing.
        Checked in Phase 4 before all other analysis — a missing BIOS
        affects every ROM in the system and should not trigger autofix.
        Override in subclasses to add platform-specific patterns.
        """
        return [
            "cannot load BIOS",     # Flycast / Naomi
            "BIOS not found",       # Generic RetroArch
            "Required BIOS",        # Some cores
        ]

    # ------------------------------------------------------------------
    # Abstract methods - launcher and environment
    # ------------------------------------------------------------------

    @abstractmethod
    def get_launcher_cmd(self) -> list[str]:
        """
        Return the base command list for launching a ROM.

        For simple platforms, this is used by the default
        build_launch_cmd() implementation. For platforms with
        different argument ordering (e.g. RetroPie), override
        build_launch_cmd() directly instead.

        Returns:
            Command list for subprocess.Popen, e.g.:
            ['/usr/bin/python3', '-m', 'configgen.emulatorlauncher']
        """
        ...

    def get_working_dir(self) -> str | None:
        """
        Return the working directory for the emulator subprocess.

        Override in platform subclasses to match the working directory
        the frontend uses when launching emulators. MAME and some other
        cores use the working directory as a fallback when resolving
        relative paths in rompath configuration.

        Returns:
            Path string, or None to inherit the current directory.
        """
        return None

    def get_env(self) -> dict[str, str]:
        """
        Return the environment variables required to launch a ROM.

        Should include display server variables and any platform-
        specific paths needed by the launcher.

        Returns:
            Dictionary of environment variable name/value pairs,
            based on a copy of os.environ with additions applied.
        """
        ...

    # ------------------------------------------------------------------
    # Concrete methods with default implementations
    # Override in subclass only if fundamentally different logic needed.
    # ------------------------------------------------------------------

    def build_launch_cmd(self, system: str, rom: str) -> list[str]:
        """
        Build the complete launch command for a ROM.

        Default implementation appends Batocera-style named arguments
        to the base launcher command. Override for platforms that use
        different argument ordering or structure (e.g. RetroPie's
        runcommand.sh which uses positional arguments).

        Args:
            system: System name (e.g. 'mame', 'snes')
            rom:    Full path to the ROM file

        Returns:
            Complete command list for subprocess.Popen.
        """
        return self.get_launcher_cmd() + ["-system", system, "-rom", rom]

    def is_launched(self, content: str) -> bool:
        """
        Determine whether an emulator was successfully launched.

        Default implementation delegates to common.detection.is_launched()
        using this platform's launch_indicators. Override if the platform
        requires fundamentally different logic rather than different strings.

        Args:
            content: Contents of the monitored log file.

        Returns:
            True if a launch indicator is found in the content.
        """
        return detection.is_launched(content, self.launch_indicators)

    def on_successful_test(
        self,
        system: str,
        romname: str,
        stdout: str
    ) -> None:
        """
        Called immediately when a ROM passes its test.

        Override in platform subclasses to perform any post-success
        action e.g. writing a per-game config entry when a global
        core override was suspended for testing and needs to be
        preserved per-game after the global override is restored.

        The base implementation does nothing.
        """

    def discover_ports_roms(self) -> list:
        """
        Discover ports ROMs for this platform.

        Override in platform subclasses that support ports discovery
        (e.g. Recalbox and Batocera via gamelist.xml). The base
        implementation returns an empty list — no ports support.

        Returns:
            List of (system, rom_path) tuples, same format as
            filehandling.discover_roms() output.
        """
        return []

    def pre_test_run(self, systems: set) -> None:
        """
        Called after ROM discovery and just before the main test loop
        begins. Receives the set of system names that will be tested.

        Override in platform subclasses to perform any setup that must
        happen after the test list is known but before testing starts
        e.g. suspending global config overrides that would mask genuine
        failures during testing.

        The base implementation does nothing.
        """

    def post_test_run(self) -> None:
        """
        Called in the finally block after the main test loop ends,
        before post_audit(). Guaranteed to run even if the audit is
        interrupted or raises an exception.

        Override in platform subclasses to undo any changes made in
        pre_test_run() e.g. restoring suspended config overrides.

        The base implementation does nothing.
        """

    def pre_audit(self) -> None:
        """
        Prepare the platform for ROM testing.

        Called once before the first ROM is launched, but only when
        there is actual testing work to do. Override in platform
        subclasses that need display or emulator management before
        testing begins.

        Default implementation is a no-op — platforms that do not
        require preparation (e.g. Batocera) need not override this.
        """
        pass

    def post_audit(self) -> None:
        """
        Clean up after ROM testing completes or is interrupted.

        Called from a finally block to ensure cleanup always runs.
        Override in platform subclasses that need to restore display
        state or notify the user of required manual steps.

        Default implementation is a no-op.
        """
        pass

    @property
    def emulator_processes(self) -> list[str]:
        """
        List of emulator process names to kill after each ROM test.

        Override in platform subclasses to add platform-specific
        standalone emulators. The base implementation covers only
        RetroArch which is common to all supported platforms.
        """
        return ['retroarch']

    def take_screenshot(
        self,
        dest_path: str,
        system: str,
        romname: str,
        delay: float = 0,
        annotate: bool = False
    ) -> None:
        """
        Orchestrate the full screenshot sequence.

        Handles the delay, directory creation, capture and overlay in
        one call. rom_audit.py calls this single method — platform
        subclasses only need to override capture_screenshot() for the
        platform-specific capture mechanism. Overlay and logging are
        handled here and apply to all platforms automatically.

        Args:
            dest_path: Full path for the output PNG file.
            system:    System name e.g. 'atari2600'.
            romname:   ROM filename e.g. 'ADVNTURE.BIN'.
            delay:     Extra seconds to wait before capturing (default 0).
                       Allows long boot animations to finish rendering.
            annotate:  If True, burn system/ROM/timestamp into the image.
                       Controlled by the --annotate CLI flag.
        """
        import os
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        if delay > 0:
            log(f"  Screenshot delay: +{delay:.0f}s")
            time.sleep(delay)

        if self.capture_screenshot(dest_path):
            if annotate:
                self.overlay_screenshot(dest_path, system, romname)
            log(f"  Screenshot saved: {dest_path}")
        else:
            log("  Screenshot: capture failed")

    def capture_screenshot(self, dest_path: str) -> bool:
        """
        Capture the current screen to a file.

        Called just before kill_emulators() so the emulator has had
        the full display_time to render before the capture. Override
        in platform subclasses with the appropriate capture tool.

        Args:
            dest_path: Full path for the output PNG file.

        Returns:
            True if capture succeeded, False otherwise.
        """
        return False

    def overlay_screenshot(
        self,
        dest_path: str,
        system: str,
        romname: str
    ) -> None:
        """
        Burn metadata into a screenshot PNG.

        Tries three methods in order, stopping at the first success:
          1. ffmpeg drawtext — available on Batocera, zero Python deps
          2. PIL/Pillow      — available on RetroPie, pure Python
          3. Companion .txt  — zero dependencies, always works

        Args:
            dest_path: Full path to the PNG file to annotate in-place.
            system:    System name e.g. 'atari2600'.
            romname:   ROM filename e.g. 'ADVNTURE.BIN'.
        """
        import os
        from datetime import datetime

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        label     = f"{system}  |  {romname}  |  {timestamp}"

        # ------------------------------------------------------------------
        # Methods 1a/1b: ffmpeg overlay — check available filters once
        # ------------------------------------------------------------------
        import shutil as _shutil
        import tempfile as _tempfile

        _has_drawtext, _has_ass = _check_ffmpeg_filters()

        def _esc(s: str) -> str:
            """Escape characters special to ffmpeg drawtext."""
            return s.replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\'")

        font_candidates = [
            '/usr/share/fonts/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
            '/usr/share/fonts/TTF/DejaVuSans.ttf',
            '/usr/share/fonts/liberation/LiberationSans-Regular.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
            '/usr/share/fonts/freefont/FreeSans.ttf',
            '/usr/share/fonts/freefont/FreeMonoBold.ttf',
            '/usr/share/fonts/misc/DejaVuSans.ttf',
        ]
        font = next((f for f in font_candidates if os.path.exists(f)), None)
        annotation = f"{system}  |  {romname}  |  {timestamp}"

        tmp_path = dest_path + '.overlay.tmp'

        # Method 1a: ffmpeg drawtext
        if _has_drawtext:
            fontfile_arg = f'fontfile={font}:' if font else ''
            vf = (
                f"drawtext={fontfile_arg}"
                f"text='{_esc(annotation)}':"
                f"fontcolor=white:fontsize=22:"
                f"box=1:boxcolor=0x00000088:boxborderw=5:"
                f"x=10:y=h-th-10"
            )
            try:
                result = subprocess.run(
                    ['ffmpeg', '-i', dest_path, '-vf', vf, '-y', tmp_path],
                    capture_output=True, timeout=15
                )
                if result.returncode == 0 and os.path.exists(tmp_path) \
                        and os.path.getsize(tmp_path) > 0:
                    _shutil.move(tmp_path, dest_path)
                    return
            except Exception:
                pass
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        # Method 1b: ffmpeg ASS subtitles via libass (Recalbox Buildroot)
        # drawtext not compiled in but libass/ass filter is available.
        if _has_ass:
            # Strip ASS special characters from annotation text
            ass_text = (annotation
                        .replace('{', '').replace('}', '')
                        .replace('\\', '\\\\')
                        .replace('\n', '\\N'))
            fontname = 'DejaVu Sans' if font else 'sans-serif'

            # Read image dimensions from PNG IHDR so PlayRes matches the
            # actual screenshot — without this libass defaults to 640x480
            # and the font appears oversized relative to the content.
            img_w, img_h = 1920, 1080   # safe fallback
            try:
                with open(dest_path, 'rb') as _pf:
                    if _pf.read(8) == b'\x89PNG\r\n\x1a\n':
                        _pf.read(8)   # IHDR chunk length + type
                        img_w = int.from_bytes(_pf.read(4), 'big')
                        img_h = int.from_bytes(_pf.read(4), 'big')
            except Exception:
                pass
            # Scale font ~1/50th of image height, clamped 14-32pt
            font_size = max(14, min(32, img_h // 50))

            ass_content = (
                "[Script Info]\n"
                "ScriptType: v4.00+\n"
                f"PlayResX: {img_w}\n"
                f"PlayResY: {img_h}\n\n"
                "[V4+ Styles]\n"
                "Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,"
                "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,"
                "Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,"
                "MarginL,MarginR,MarginV,Encoding\n"
                f"Style: Default,{fontname},{font_size},&H00FFFFFF,&H00000000,"
                "&H80000000,0,0,0,0,100,100,0,0,1,1,1,1,15,15,20,1\n\n"
                "[Events]\n"
                "Format: Layer,Start,End,Style,Name,"
                "MarginL,MarginR,MarginV,Effect,Text\n"
                f"Dialogue: 0,0:00:00.00,0:00:00.10,Default,,0,0,0,,{ass_text}\n"
            )
            ass_fd, ass_path = _tempfile.mkstemp(suffix='.ass')
            try:
                with os.fdopen(ass_fd, 'w') as f:
                    f.write(ass_content)
                result = subprocess.run(
                    [
                        'ffmpeg',
                        '-loop', '1', '-t', '0.1',
                        '-i', dest_path,
                        '-vf', f'ass={ass_path}',
                        '-frames:v', '1',
                        '-vcodec', 'png',
                        '-f', 'image2',
                        '-y', tmp_path,
                    ],
                    capture_output=True, timeout=15
                )
                if result.returncode == 0 and os.path.exists(tmp_path) \
                        and os.path.getsize(tmp_path) > 0:
                    _shutil.move(tmp_path, dest_path)
                    return
                log(f"  Screenshot overlay (ass rc={result.returncode}): "
                    f"{result.stderr.decode(errors='replace').splitlines()[-1] if result.stderr else ''}")
            except Exception as e:
                log(f"  Screenshot overlay (ass error): {e}")
            finally:
                try:
                    os.unlink(ass_path)
                except Exception:
                    pass
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        # ------------------------------------------------------------------
        # Method 2: PIL/Pillow (RetroPie and any platform with Pillow)
        # ------------------------------------------------------------------
        try:
            from PIL import Image, ImageDraw, ImageFont

            # Verify file is readable and non-empty before attempting overlay
            if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
                raise ValueError(f"Screenshot file missing or empty: {dest_path}")

            img = Image.open(dest_path).convert('RGBA')
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            draw    = ImageDraw.Draw(overlay)

            pil_font = None
            if font:
                try:
                    pil_font = ImageFont.truetype(font, 22)
                except Exception:
                    pass
            if pil_font is None:
                pil_font = ImageFont.load_default()

            # textbbox added in Pillow 8.0 — fall back to textsize for
            # older versions (e.g. Bullseye ships Pillow 7.x)
            try:
                bbox   = draw.textbbox((0, 0), label, font=pil_font)
                text_h = bbox[3] - bbox[1]
            except AttributeError:
                _, text_h = draw.textsize(label, font=pil_font)
            bar_h  = text_h + 16

            draw.rectangle(
                [0, img.height - bar_h, img.width, img.height],
                fill=(0, 0, 0, 136)
            )
            draw.text(
                (10, img.height - bar_h + 8),
                label, fill='white', font=pil_font
            )

            Image.alpha_composite(img, overlay).convert('RGB').save(dest_path)
        except ImportError:
            pass   # Pillow not installed
        except Exception as e:
            log(f"  Screenshot overlay (PIL failed): {e}")

        # ------------------------------------------------------------------
        # Always write companion .txt — useful metadata record even when
        # the image overlay worked, and the only output when no annotation
        # tool is available. Zero dependencies, always succeeds.
        # ------------------------------------------------------------------
        txt_path = os.path.splitext(dest_path)[0] + '.txt'
        try:
            with open(txt_path, 'w') as f:
                f.write(
                    f"system:    {system}\n"
                    f"rom:       {romname}\n"
                    f"captured:  {timestamp}\n"
                )
        except Exception:
            pass   # Never fail the audit over an annotation

    def kill_emulators(self) -> None:
        """
        Kill all running emulator processes after a ROM test.

        Tries pkill first (standard Linux). Falls back to killall
        for BusyBox environments such as Batocera 36 and earlier
        where pkill is not available.
        """
        import subprocess
        for process in self.emulator_processes:
            try:
                subprocess.run(
                    ['pkill', '-f', process],
                    capture_output=True
                )
            except FileNotFoundError:
                # BusyBox (Batocera 36 and earlier) — use killall
                subprocess.run(
                    ['killall', '-9', process],
                    capture_output=True
                )

    # ------------------------------------------------------------------
    # Test execution
    # ------------------------------------------------------------------

    def screenshot_path(
        self,
        screenshot: bool,
        screenshot_flat: bool,
        system: str,
        romname: str
    ) -> str | None:
        """
        Build the screenshot destination path based on CLI flags.

        Flat mode  (--screenshot-flat):
            {error_log_base}/screenshots/{system}_{romname}.png
        Nested mode (default):
            {error_log_base}/{system}/{romname}/{system}_{romname}_screenshot.png

        Both modes sanitize system and romname via _safe_path_component()
        — the same helper used by save_error_logs() and clear_error_logs()
        in filehandling.py — so a crafted filename like '../../etc/passwd'
        can't traverse outside the intended audit_logs/ tree. Previously
        flat mode sanitized (via an inline re.sub) but nested mode did not;
        now both go through the single shared helper, so the whitelist is
        defined exactly once.

        Returns:
            Full path string, or None if screenshot not requested.
        """
        from modules.common.filehandling import _safe_path_component
        if not screenshot:
            return None
        safe_sys = _safe_path_component(system)
        safe_rom = _safe_path_component(romname)
        if screenshot_flat:
            flat_dir = os.path.join(self.error_log_base, 'screenshots')
            os.makedirs(flat_dir, exist_ok=True)
            return os.path.join(flat_dir, f'{safe_sys}_{safe_rom}.png')
        return os.path.join(
            self.error_log_base, safe_sys, safe_rom,
            f'{safe_sys}_{safe_rom}_screenshot.png'
        )

    def run_test(
        self,
        system: str,
        rom: str,
        dashboard,
        state: dict,
        timeout: float = MAX_WAIT,
        screenshot_path: str = None,
        screenshot_delay: float = 0,
        annotate: bool = False
    ) -> tuple:
        """
        Launch a single ROM and determine whether it loaded successfully.

        Phases:
          1 - Poll logs until launch indicator, error, or process exit.
          2 - Display the running game for DISPLAY_TIME seconds.
          3 - Optional screenshot capture with configurable delay.
          4 - Kill emulator and analyse complete log output.

        Returns:
            Tuple of (status, notes, elapsed_seconds).
            Status: OK, ERROR, MISSING CORE, TIMEOUT, or LAUNCHED.
        """
        stdout_log = self.stdout_log
        stderr_log = self.stderr_log

        can_launch, reason = self.validate_rom_launch(system, rom)
        if not can_launch:
            return 'MISSING CORE', reason, 0.0

        filehandling.clear_log(stdout_log)
        filehandling.clear_log(stderr_log)

        if self.subprocess_capture:
            stdout_f = open(stdout_log, 'w')
            stderr_f = open(stderr_log, 'w')
        else:
            stdout_f = open(os.devnull, 'w')
            stderr_f = open(os.devnull, 'w')

        wall_start = time.monotonic()
        status   = 'TIMEOUT'
        notes    = ''
        elapsed  = 0.0
        launched = False

        try:
            proc = subprocess.Popen(
                self.build_launch_cmd(system, rom),
                stdout=stdout_f,
                stderr=stderr_f,
                env=self.get_env(),
                cwd=self.get_working_dir(),
                preexec_fn=os.setsid
            )

            # Phase 1: wait for launch indicator
            while elapsed < timeout:
                time.sleep(CHECK_INTERVAL)
                elapsed += CHECK_INTERVAL

                if proc.poll() is not None:
                    content = (
                        filehandling.read_log(stdout_log) + '\n' +
                        filehandling.read_log(stderr_log)
                    )
                    if self.is_launched(content):
                        launched = True
                    else:
                        log('  Process exited without launch indicator')
                    break

                content = (
                    filehandling.read_log(stdout_log) + '\n' +
                    filehandling.read_log(stderr_log)
                )

                if 'MissingCore' in content:
                    status = 'MISSING CORE'
                    notes  = 'Core not installed'
                    break
                elif 'Traceback' in content and 'ERROR' in content:
                    status = 'ERROR'
                    notes  = 'Python exception in launcher'
                    break
                elif self.is_launched(content):
                    launched = True
                    break

                state['elapsed'] = time.time() - state['start_time']
                dashboard.update(state)
                if elapsed % 5 < CHECK_INTERVAL:
                    log(f'  ... still waiting ({elapsed:.0f}s)')
                    state['current_status'] = f'Waiting... ({elapsed:.0f}s)'

            else:
                # Loop exhausted timeout without break — if process still
                # alive, treat as launched (output-buffered systems like MAME).
                if proc.poll() is None:
                    launched = True

            # Phase 2: display game
            display_time = self.get_display_time(system)
            exited_early       = False
            early_exit_elapsed = 0.0
            early_exit_code    = None
            if launched:
                log(f'  ... launched, displaying for {display_time}s')
                state['current_status'] = f'Launched — displaying for {display_time}s'
                display_start = time.monotonic()
                display_end   = display_start + display_time
                while time.monotonic() < display_end:
                    if proc.poll() is not None:
                        # The process ended on its own during the display
                        # window — before we ever attempted to kill it.
                        # A genuinely running game should still be alive
                        # at this point; the emulator quitting by itself
                        # this early — even with a clean exit code — is
                        # a strong sign retro_load_game() failed internally
                        # and RetroArch exited gracefully rather than
                        # crashing or logging anything recognisable.
                        # Confirmed case: RetroArch/MAME content fails to
                        # load, exits cleanly (code 0) in well under the
                        # intended display window, with no error text in
                        # either its own log or EmulationStation's log —
                        # EmulationStation's "[Run] No error running" line
                        # is based purely on the same clean exit code and
                        # cannot tell the difference either.
                        exited_early       = True
                        early_exit_elapsed = time.monotonic() - display_start
                        early_exit_code    = proc.returncode
                        log(
                            f'  Process exited on its own after '
                            f'{early_exit_elapsed:.1f}s (expected to run '
                            f'{display_time}s) — exit code {early_exit_code}'
                        )
                        break
                    state['elapsed'] = time.time() - state['start_time']
                    dashboard.update(state)
                    time.sleep(min(CHECK_INTERVAL, display_end - time.monotonic()))

                # Catches the exact edge the loop above can miss: the
                # process dies right as display_end passes, between the
                # loop's last poll and its time condition expiring — so
                # the loop exits normally (treated as "ran the full
                # window") without ever re-checking. Confirmed in
                # practice: a genuine SIGSEGV (exit code -11) on
                # exctleag.zip was caught as ERROR on some runs and
                # silently passed as OK on others, identical ROM and
                # command — the crash itself wasn't intermittent, only
                # whether this exact race landed before or after
                # display_end was. One extra poll here, costing nothing
                # when the process is genuinely still running (the
                # overwhelmingly common case), closes it.
                if not exited_early and proc.poll() is not None:
                    exited_early       = True
                    early_exit_elapsed = time.monotonic() - display_start
                    early_exit_code    = proc.returncode
                    log(
                        f'  Process exited on its own right at the '
                        f'display window boundary ({early_exit_elapsed:.1f}s) '
                        f'— exit code {early_exit_code}'
                    )

                # Phase 3: screenshot — skip if the process already exited;
                # there is nothing genuine left on screen to capture and a
                # "screenshot" at this point would just show whatever ES
                # or the desktop happened to render after the fact.
                if screenshot_path and not exited_early:
                    total_delay = screenshot_delay + getattr(
                        self, 'screenshot_warmup', 0
                    )
                    if total_delay > 0:
                        delay_end = time.monotonic() + total_delay
                        while time.monotonic() < delay_end:
                            remaining = delay_end - time.monotonic()
                            state['current_status'] = (
                                f'Waiting for game to load... ({remaining:.0f}s)'
                            )
                            state['elapsed'] = time.time() - state['start_time']
                            dashboard.update(state)
                            time.sleep(
                                min(CHECK_INTERVAL, max(0.0, delay_end - time.monotonic()))
                            )
                    state['current_status'] = 'Capturing screenshot...'
                    dashboard.update(state)
                    self.take_screenshot(
                        screenshot_path, system,
                        self.get_rom_display_name(system, rom),
                        0, annotate=annotate
                    )

                if not exited_early:
                    self.kill_emulators()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()

            else:
                # Never launched — force kill immediately
                try:
                    log(f'  Killing — no launch indicator after {elapsed:.0f}s')
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()

        except KeyboardInterrupt:
            log('  Interrupted — killing any running game...')
            self.kill_emulators()
            try:
                if 'proc' in dir() and proc is not None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait()
            except (ProcessLookupError, OSError):
                pass
            raise
        except Exception as e:
            status = 'ERROR'
            notes  = f'Launch failed: {e}'
            log(f'  ERROR: {e}')
        finally:
            stdout_f.close()
            stderr_f.close()

        # Grace period before reading the final log content — covers
        # both the original reason (proc.wait() returning only confirms
        # the tracked PID is gone, not that everything it ever wrote has
        # been flushed to disk yet) and, per-system, time for a graphics
        # context to release cleanly after being killed — see
        # get_post_kill_delay() and Recalbox's own kill_emulators()
        # override for a confirmed precedent of the same exit-code-1
        # symptom from exactly this cause.
        time.sleep(self.get_post_kill_delay(system))

        # Phase 4: analyse complete log output
        stdout_content = filehandling.read_log(stdout_log)
        stderr_content = filehandling.read_log(stderr_log)
        combined       = stdout_content + '\n' + stderr_content

        if launched:
            post_error = None
            non_fatal  = getattr(self, 'non_fatal_post_launch_markers', [])
            for line in combined.splitlines():
                if any(m in line for m in self.post_launch_error_markers):
                    if any(nf in line for nf in non_fatal):
                        continue
                    post_error = line.strip()[:80]
                    break

            if post_error:
                status = 'ERROR'
                notes  = post_error
            else:
                # Delegate to the platform's own parse_error() rather than
                # calling detection.parse_error() directly with a blank
                # exit_marker. This was previously suppressed under the
                # assumption that the process is always killed by our own
                # SIGKILL (making any exit-code marker meaningless, always
                # -9). That assumption is false: launch_indicators can
                # match (e.g. Recalbox's "Running command:" / "/usr/bin/
                # retroarch" appears the instant the launcher attempts to
                # invoke retroarch) BEFORE the emulator itself crashes with
                # a genuine non-zero exit code — so `launched` can become
                # True for a ROM that actually failed within the same
                # poll interval, well before we ever send a kill signal.
                # self.parse_error() lets platforms catch this:
                #   - Recalbox parses "Process exitcode: N" directly from
                #     the log and checks it against TOLERATED_EXIT_CODES.
                #   - RetroPie checks for zero-runtime-with-zero-frames.
                #   - Batocera's EXIT_MARKER ("Exiting configgen with
                #     status 1") is logged by the Python launcher wrapper
                #     only AFTER it observes the emulator's real exit code
                #     — if we SIGKILL a genuinely healthy running game, the
                #     wrapper is killed before it can log that line, so
                #     this check carries no risk of false positives for
                #     legitimate successful launches we deliberately end.
                error_status, error_notes = self.parse_error(
                    stdout_content, stderr_content
                )
                if error_status:
                    status = error_status
                    notes  = error_notes
                elif exited_early:
                    # No recognisable error text and no non-zero/crash exit
                    # code — but the process quit on its own well before
                    # the display window ended, which we never asked it to
                    # do. A clean, fast, self-initiated exit like this is
                    # the same signature a content-load failure produces
                    # (e.g. RetroArch's retro_load_game() returning false
                    # and exiting gracefully) and is indistinguishable from
                    # a real success using exit codes or log text alone.
                    status = 'ERROR'
                    notes  = (
                        f'Process exited on its own after '
                        f'{early_exit_elapsed:.1f}s (expected to run '
                        f'{display_time}s), exit code {early_exit_code} — '
                        f'likely failed to load content silently'
                    )
                else:
                    # Don't finalise OK off a single read — give any
                    # output still being flushed one short, deliberately
                    # cheap chance to land and be re-checked. See
                    # OK_RECHECK_DELAY above for why this exists.
                    time.sleep(OK_RECHECK_DELAY)
                    fresh_stdout = filehandling.read_log(stdout_log)
                    fresh_stderr = filehandling.read_log(stderr_log)
                    recheck_status, recheck_notes = self.parse_error(
                        fresh_stdout, fresh_stderr
                    )
                    if recheck_status:
                        status  = recheck_status
                        notes   = recheck_notes
                        combined = fresh_stdout + '\n' + fresh_stderr
                        log(f'  Re-check after {OK_RECHECK_DELAY}s found '
                            f'an error not present on the first read: '
                            f'{recheck_notes}')
                    else:
                        status = 'OK'
                        self.on_successful_test(
                            system,
                            self.get_rom_display_name(system, rom),
                            combined
                        )

            # Check for imperfect emulation — game runs but MAME/core
            # flags known accuracy issues (colour, sound, timing etc.)
            # Only applies when status would otherwise be OK.
            if status == 'OK':
                imperfect = getattr(self, 'imperfect_markers', [])
                dump_quality = getattr(self, 'dump_quality_markers', [])
                for line in combined.splitlines():
                    if any(m in line for m in dump_quality):
                        # Dump-quality marker — load status is ambiguous.
                        # Tag notes with a sentinel so rom_audit.py's main
                        # loop can route this through verify_unverified_core()
                        # rather than trusting a bare IMPERFECT result.
                        # The sentinel is stripped before the CSV is written;
                        # it is never user-visible in its raw form.
                        status = 'IMPERFECT'
                        notes  = f'[DUMP_QUALITY] {line.strip()[:80]}'
                        break
                    if any(m in line for m in imperfect):
                        status = 'IMPERFECT'
                        notes  = line.strip()[:80]
                        break

        elif status not in ('MISSING CORE', 'ERROR'):
            error_status, error_notes = self.parse_error(
                stdout_content, stderr_content
            )
            if error_status:
                status = error_status
                notes  = error_notes
            elif self.is_launched(stdout_content):
                status = 'OK'
            elif status == 'TIMEOUT':
                pass
            else:
                status = 'LAUNCHED'
                notes  = 'RetroArch started, game status unconfirmed'

        return status, notes, time.monotonic() - wall_start

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
        Attempt to fix a failing ROM using platform-specific methods.

        Override in platform subclasses to provide autofix support.
        The default implementation returns NO COMBINATIONS indicating
        no autofix support for the platform.

        Args:
            system:         System folder name e.g. 'mame', 'arcade'
            rom:            Full path to the ROM file
            romname:        ROM filename e.g. 'pacman.7z'
            dashboard:      Dashboard instance for live updates
            state:          Shared state dict for dashboard updates
            original_error: Notes from the initial failed test. Used by
                            platform implementations to adjust autofix
                            behaviour based on the specific error type
                            e.g. Batocera detects NO GOOD DUMP KNOWN
                            and returns LAUNCHED rather than FIXED to
                            flag the result for manual verification.
            **kwargs:       Platform-specific additional arguments

        Returns:
            Tuple of (status, notes) where status is one of:
                'FIXED'           - A working combination was found
                'LAUNCHED'        - A core ran without errors but the
                                   result needs manual verification
                'GENUINE ERROR'   - All combinations failed
                'NO COMBINATIONS' - No autofix support available
        """
        return 'NO COMBINATIONS', f"No autofix support for [{system}]"

    @property
    def imperfect_markers(self) -> list[str]:
        """
        Strings in the log indicating imperfect but functional emulation.
        e.g. MAME driver warnings about known accuracy issues.
        Override in platform subclasses to enable IMPERFECT detection.
        """
        return []

    @property
    def dump_quality_markers(self) -> list[str]:
        """
        Strings in the log indicating a ROM has an unverified or known-bad
        dump that MAME will attempt to run anyway.

        Unlike imperfect_markers (which confirm the game loaded), these
        leave the actual load status ambiguous — a screenshot heuristic
        is needed to resolve whether the game genuinely displayed content
        (IMPERFECT) or showed nothing useful (ERROR).

        Override in platform subclasses where MAME dump-quality warnings
        are present. Returns empty list by default.
        """
        return []

    def parse_error(
        self,
        stdout: str,
        stderr: str
    ) -> tuple[str, str]:
        """
        Attempt to extract an error status and description from logs.

        Default implementation delegates to common.detection.parse_error()
        using this platform's error_markers and exit_marker. Override if
        the platform requires fundamentally different logic.

        Args:
            stdout: Contents of the stdout log file.
            stderr: Contents of the stderr log file.

        Returns:
            Tuple of (status, notes) where status is "ERROR" if an error
            was found, or (None, "") if no error was detected.
        """
        return detection.parse_error(
            stdout, stderr,
            self.error_markers,
            self.exit_marker,
            non_fatal=getattr(self, 'non_fatal_post_launch_markers', [])
        )
