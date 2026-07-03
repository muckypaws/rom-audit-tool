"""
RetroPie platform implementation for the ROM Audit Tool.

RetroPie differs from Batocera in several important ways:

    - Games are launched via RetroArch directly (not runcommand.sh)
      to avoid EmulationStation display ownership conflicts
    - EmulationStation must be stopped before the audit runs to
      release the KMS/DRM display
    - RetroArch output is captured directly from subprocess stderr
    - Autofix works by trying different core .so files rather than
      writing to a config file (no batocera.conf equivalent)
    - Remediation of core mismatches requires moving ROMs to the
      correct system folder, handled by the --migrate command

These differences are encapsulated here so the rest of the tool
requires minimal changes to support RetroPie.
"""

from __future__ import annotations   # Python 3.9 compatibility

import os
import time
import subprocess
import shlex

from modules.platforms.base import Platform
from modules.common.logging import log
from modules.common import filehandling


# How long to poll for "Frames pushed: N" to actually appear when the
# zero-runtime text is already present but no frame count has shown up
# yet — see _check_zero_runtime()'s docstring for why a fixed delay
# beforehand isn't reliable here; the exact time the core's last
# shutdown line takes to land on disk varies run to run.
ZERO_RUNTIME_RETRY_TIMEOUT  = 2.0
ZERO_RUNTIME_RETRY_INTERVAL = 0.2


class RetroPiePlatform(Platform):
    """
    Platform implementation for RetroPie 4.x and later.

    Calls RetroArch directly rather than via runcommand.sh to avoid
    display ownership issues. Requires EmulationStation to be stopped
    before launching ROMs. Autofix tries alternative MAME core versions
    rather than writing per-game config entries.
    """

    # ------------------------------------------------------------------
    # Log analysis criteria
    # ------------------------------------------------------------------

    LAUNCH_INDICATORS = [
        # Core loaded from disk — appears early in RetroArch verbose output
        "[INFO] Loading dynamic libretro core from:",
        # DRM display found — confirms video initialised
        "[INFO] [DRM]: Found",
        # GL context confirmed
        "[INFO] [GL]: Found GL context:",
        # Video display server
        "[INFO] [Video]: Found display server:",
    ]

    ERROR_MARKERS = [
        "[libretro ERROR]",
        "[ERROR] Failed to load content",
        "Fatal error",
        "Could not find core",
    ]

    EXIT_MARKER = ""

    POST_LAUNCH_ERROR_MARKERS = [
        "NOT FOUND",
        "Game driver not found",
        "[ERROR] Failed to load content",
        "Failed to load content",
        "is required",
        "Wrong data-format, corrupt or unsupported ROM",
        #"Failed to extract content from compressed file",
        "game does not work",                              # MAME driver warning
        "There are known problems emulating this game",    # MAME known issues
        # NOTE: "Content ran for a total of: 00 hours, 00 minutes, 00
        # seconds" deliberately NOT listed here. It used to be, which
        # meant it matched unconditionally as an error right here and
        # _check_zero_runtime()'s frames-aware logic below was never
        # reached — completely defeating the reason that check exists.
        # Confirmed in practice: 1944.zip (a known-good CPS1 ROM) was
        # flagged ERROR despite running for the tool's full measured
        # display window (5.8s, matching a clean run) on one pass and
        # OK on another, with RetroArch's own zero-runtime line
        # appearing non-deterministically either way — exactly the
        # same logging quirk already known for snes9x/Stella2014, just
        # not exempted for MAME-family cores until now. Handle this
        # text ONLY via _check_zero_runtime(), which additionally
        # requires zero frames pushed before treating it as a genuine
        # failure.
    ]

    BIOS_ERROR_MARKERS = [
        "cannot load BIOS",
        "BIOS not found",
        "Required BIOS",
    ]

    # ------------------------------------------------------------------
    # MAME core combinations for autofix
    # Ordered from oldest (most compatible with older ROMs) to newest.
    # Each entry: (display_name, core_dir, core_so)
    # ------------------------------------------------------------------
    MAME_CORE_COMBINATIONS = [
        ('lr-mame2000',    'lr-mame2000',    'mame2000_libretro.so'),
        ('lr-mame2003',    'lr-mame2003',    'mame2003_libretro.so'),
        ('lr-mame2003-plus','lr-mame2003-plus','mame2003_plus_libretro.so'),
        ('lr-mame2010',    'lr-mame2010',    'mame2010_libretro.so'),
        ('lr-mame2014',    'lr-mame2014',    'mame2014_libretro.so'),
        ('lr-mame2016',    'lr-mame2016',    'mame2016_libretro.so'),
        ('lr-mame',        'lr-mame',        'mame_libretro.so'),
    ]

    FBA_CORE_COMBINATIONS = [
        ('lr-fba',         'lr-fba',         'fba_libretro.so'),
        ('lr-fbalpha2012', 'lr-fbalpha2012', 'fbalpha2012_libretro.so'),
        ('lr-fbneo',       'lr-fbneo',       'fbneo_libretro.so'),
    ]

    SYSTEM_CORE_COMBINATIONS = {
        'arcade':       MAME_CORE_COMBINATIONS,
        'mame-libretro':MAME_CORE_COMBINATIONS,
        'fba':          FBA_CORE_COMBINATIONS,
    }

    # Lines that look like errors but are non-fatal MAME 2003 warnings
    NON_FATAL_MARKERS = [
        "cpunum_get_localtime() called for invalid cpu num",
    ]

    LIBRETRO_CORES_PATH = "/opt/retropie/libretrocores"

    # Some RetroPie ROM folder names differ from their config directory names
    SYSTEM_CONFIG_MAP = {
        'genesis':  'megadrive',
        'sg-1000':  'sg1000',
    }

    def __init__(self) -> None:
        self._version       = self._detect_version()
        self._retropie_home = self._detect_retropie_home()
        self._screen_width, self._screen_height = (
            self._detect_screen_resolution()
        )
        log(f"RetroPie v{self._version} detected")
        log(f"RetroPie home: {self._retropie_home}")

    def _detect_version(self) -> str:
        """
        Detect the installed RetroPie version.

        Reads /opt/retropie/VERSION which contains a version string
        such as "4.8.8". This file is only written after the RetroPie
        setup script (retropie_setup.sh) has been run at least once —
        on a stock/imaged system that's never had the setup script
        launched, it won't exist yet.

        Falls back to the RetroPie-Setup git commit hash if VERSION
        is missing, since that's present on any standard install.

        Returns:
            Version string e.g. "4.8.8", a git hash e.g. "git-6e83a7d5",
            or "unknown" if neither is available.
        """
        version_file = "/opt/retropie/VERSION"
        try:
            with open(version_file, 'r') as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass

        try:
            import subprocess
            result = subprocess.run(
                ['git', '-C', '/home/pi/RetroPie-Setup', 'log', '-1', '--pretty=format:%h'],
                capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0 and result.stdout.strip():
                return f"git-{result.stdout.strip()}"
        except Exception:
            pass

        return "unknown"

    def _detect_retropie_home(self) -> str:
        """
        Detect the home directory containing the RetroPie folder.

        On Raspberry Pi the user is always 'pi' and the path is
        /home/pi/RetroPie. On x86 installations the user could be
        anything. Searches candidates in priority order:

        1. Current user (USER env var)
        2. Sudo originating user (SUDO_USER) — for sudo invocations
        3. LOGNAME
        4. Traditional 'pi' user as Pi fallback
        5. Any user with a RetroPie directory in their home
        6. Current process home as last resort

        Returns:
            Full path to the home directory containing RetroPie/,
            e.g. '/home/pi' or '/home/jason'.
        """
        import pwd

        candidates = []
        for env_var in ('USER', 'SUDO_USER', 'LOGNAME'):
            user = os.environ.get(env_var, '').strip()
            if user and user != 'root':
                candidates.append(user)
        candidates.append('pi')

        # Check each candidate for a RetroPie directory
        for username in candidates:
            try:
                home = pwd.getpwnam(username).pw_dir
                if os.path.isdir(os.path.join(home, 'RetroPie')):
                    return home
            except KeyError:
                continue

        # Scan all users as fallback
        try:
            for entry in pwd.getpwall():
                home = entry.pw_dir
                if os.path.isdir(os.path.join(home, 'RetroPie')):
                    log(f"  Found RetroPie under: {home}")
                    return home
        except Exception:
            pass

        # Absolute last resort — current process home
        home = os.path.expanduser('~')
        log(f"  Warning: could not locate RetroPie directory, "
            f"defaulting to {home}")
        return home

    def _detect_screen_resolution(self) -> tuple[int, int]:
        """
        Detect the current screen resolution from the KMS/DRM subsystem.

        Tries fbset first for an accurate current mode reading, then
        falls back to the sysfs virtual_size interface. Returns
        1920x1080 if both methods fail.

        Returns:
            Tuple of (width, height) as integers.
        """
        # Try fbset — reads current display mode accurately
        try:
            result = subprocess.run(
                ['fbset', '-s'],
                capture_output=True, text=True, timeout=3
            )
            for line in result.stdout.splitlines():
                if 'geometry' in line:
                    parts = line.split()
                    return int(parts[1]), int(parts[2])
        except Exception:
            pass

        # Fallback via sysfs — available without fbset
        try:
            with open('/sys/class/graphics/fb0/virtual_size') as f:
                w, h = f.read().strip().split(',')
                return int(w), int(h)
        except Exception:
            pass

        log("  Warning: could not detect screen resolution, "
            "defaulting to 1920x1080")
        return 1920, 1080

    # ------------------------------------------------------------------
    # Platform properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return f"RetroPie v{self._version}"

    @property
    def roms_path(self) -> str:
        return os.path.join(self._retropie_home, "RetroPie", "roms")

    @property
    def stdout_log(self) -> str:
        """RetroArch stdout — typically empty, verbose output goes to stderr."""
        return os.path.join(self._retropie_home, "RetroPie", "rom_audit", "retroarch_stdout.log")

    @property
    def stderr_log(self) -> str:
        """RetroArch stderr — all verbose output including launch indicators."""
        return os.path.join(self._retropie_home, "RetroPie", "rom_audit", "retroarch_stderr.log")

    @property
    def subprocess_capture(self) -> bool:
        """
        Capture RetroArch stdout/stderr directly to our log files.
        We call RetroArch directly so we own the output streams.
        """
        return True

    @property
    def results_csv(self) -> str:
        return os.path.join(self._retropie_home, "RetroPie", "rom_audit", "rom_audit.csv")

    @property
    def log_file(self) -> str:
        return os.path.join(self._retropie_home, "RetroPie", "rom_audit", "rom_audit.log")

    @property
    def pid_file(self) -> str:
        return os.path.join(self._retropie_home, "RetroPie", "rom_audit", "rom_audit.pid")

    @property
    def error_log_base(self) -> str:
        return os.path.join(self._retropie_home, "RetroPie", "rom_audit", "audit_logs")

    @property
    def faulty_roms_path(self) -> str:
        return os.path.join(self._retropie_home, "RetroPie", "faultyroms")

    @property
    def libretro_core_path(self) -> str:
        return self.LIBRETRO_CORES_PATH

    @property
    def conf_path(self) -> str:
        """
        RetroPie has no batocera.conf equivalent.
        Returns empty string — standard autofix is skipped.
        RetroPie uses core-swapping autofix instead.
        """
        return ""

    def get_display_time(self, system: str) -> float:
        """
        Seconds to display a launched game before killing it.

        Base default is longer than Batocera's to allow RetroArch to
        fully initialise after the launch indicator fires. gameandwatch
        specifically needs longer still — confirmed in practice: lr-gw
        genuinely needs several seconds running before RetroArch's own
        "Content ran for..." timer starts counting at all. Killed
        before that point (the previous flat 5s default), the timer
        was permanently zero — not delayed, never started — even with
        frames already being pushed during what's apparently a loading/
        splash phase by this core's own timeline. Two UI-confirmed-
        working examples showed the timer only reaching 2-4 seconds
        of real progress; 10s gives comfortable margin beyond that
        without being excessive for a system this lightweight.
        """
        GAMEANDWATCH_DISPLAY_TIME = 10
        if system == 'gameandwatch':
            return GAMEANDWATCH_DISPLAY_TIME
        return 5

    @property
    def non_fatal_post_launch_markers(self) -> list[str]:
        """
        Strings that look like errors but are non-fatal MAME 2003 warnings.
        Games continue running correctly despite these appearing in the log.
        """
        return [
            "cpunum_get_localtime() called for invalid cpu num",
        ]

    # ------------------------------------------------------------------
    # Log analysis properties
    # ------------------------------------------------------------------

    @property
    def launch_indicators(self) -> list[str]:
        return self.LAUNCH_INDICATORS

    @property
    def error_markers(self) -> list[str]:
        return self.ERROR_MARKERS

    @property
    def exit_marker(self) -> str:
        return self.EXIT_MARKER

    @property
    def post_launch_error_markers(self) -> list[str]:
        return self.POST_LAUNCH_ERROR_MARKERS

    @property
    def bios_error_markers(self) -> list[str]:
        return self.BIOS_ERROR_MARKERS

    # ------------------------------------------------------------------
    # Launcher
    # ------------------------------------------------------------------
    def parse_error(
        self,
        stdout_content: str,
        stderr_content: str
    ) -> tuple[str, str]:
        """
        Override base parse_error to filter non-fatal MAME 2003 warnings.
        cpunum_get_localtime appears as [libretro ERROR] but is non-fatal
        and does not prevent games from running.

        Also checks for zero-runtime with zero frames — a reliable
        indicator of genuine failure. Zero runtime alone is not enough
        since snes9x and Stella 2014 report zero runtime for zip-loaded
        ROMs that actually ran fine (frames pushed confirms the truth).
        """
        non_fatal = self.non_fatal_post_launch_markers
        combined  = stdout_content + "\n" + stderr_content

        for line in combined.splitlines():
            # Check error markers
            if any(m in line for m in self.error_markers):
                # Skip known non-fatal warnings
                if any(nf in line for nf in non_fatal):
                    continue
                return "ERROR", line.strip()[:80]

            # Check exit marker
            if self.exit_marker and self.exit_marker in line:
                return "ERROR", line.strip()[:80]

        # Zero-runtime check — only flag as error if no frames were pushed.
        # Zero runtime alone is unreliable for zip-loaded ROMs.
        if self._check_zero_runtime(combined):
            return (
                "ERROR",
                "Content ran for 0 seconds with 0 frames — genuine failure"
            )

        return "", ""

    def _check_zero_runtime(self, log_content: str) -> bool:
        """
        Return True only if content ran for zero seconds AND zero frames
        were pushed — the reliable indicator of a genuine load failure.

        Zero runtime alone is not sufficient: snes9x and Stella 2014
        report zero runtime for zip-loaded ROMs that actually ran fine.
        Frames pushed is the ground truth — 771 frames means the game ran
        regardless of what the content timer reports.

        If the zero-runtime text is present but no frames-pushed count
        has appeared yet, polls by re-reading the actual log files from
        disk for up to ZERO_RUNTIME_RETRY_TIMEOUT seconds rather than
        concluding genuine failure off one read. Confirmed in practice
        against a real ROM that genuinely works (Donkey Kong Jr. (Coleco)
        on lr-gw): "Threaded video stats: Frames pushed: N" is the LAST
        line a core writes during its own shutdown sequence — after
        "Content ran for...", after "Unloading game/core/symbols" — and
        a fixed grace period before checking wasn't enough; the actual
        delay before that final line lands on disk varies between runs
        (confirmed with frame counts of both 31 and 40 on back-to-back
        tests of the identical ROM, with the line genuinely present each
        time, just not always there yet at the moment of the read).
        Polling for the line to actually show up, with a sensible upper
        bound, adapts to however long it really takes instead of
        gambling on one fixed number.
        """
        import re

        if ('Content ran for a total of: '
                '00 hours, 00 minutes, 00 seconds.' not in log_content):
            return False

        match = re.search(r'Frames pushed: (\d+)', log_content)
        if match and int(match.group(1)) > 0:
            return False

        # Zero-runtime text is present but no non-zero frame count yet
        # — the genuinely ambiguous case. Poll for it rather than judge
        # off a single, possibly-premature read.
        deadline = time.monotonic() + ZERO_RUNTIME_RETRY_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(ZERO_RUNTIME_RETRY_INTERVAL)
            fresh = (
                filehandling.read_log(self.stdout_log) + '\n' +
                filehandling.read_log(self.stderr_log)
            )
            match = re.search(r'Frames pushed: (\d+)', fresh)
            if match and int(match.group(1)) > 0:
                log(f'  Frames pushed: {match.group(1)} appeared on '
                    f'retry — not a genuine failure, just a late-'
                    f'flushing shutdown line.')
                return False

        return True

    def _get_pergame_override(
        self,
        system: str,
        romname: str
    ) -> str | None:
        """
        Look up a per-game emulator override in the global emulators.cfg.

        Checks /opt/retropie/configs/all/emulators.cfg for an entry
        matching the pattern:  system_romname = "core_name"

        This is exactly what runcommand.sh reads when launching from ES.
        Consulting it here ensures the audit tool uses the same core ES
        would use, so a ROM that was fixed by autofix tests correctly on
        recheck rather than falling back to the system default.

        Args:
            system:  System name e.g. 'arcade'.
            romname: ROM filename e.g. 'wizwarz.zip'.

        Returns:
            Core name string e.g. 'lr-mame2010', or None if no override.
        """
        stem = os.path.splitext(romname)[0]
        key  = f"{system}_{stem}"
        cfg_path = '/opt/retropie/configs/all/emulators.cfg'
        try:
            with open(cfg_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' not in line:
                        continue
                    lhs, rhs = line.split('=', 1)
                    if lhs.strip() == key:
                        return rhs.strip().strip('"')
        except Exception:
            pass
        return None

    def _parse_emulators_cfg(
        self,
        system: str,
        preferred_core: str = None
    ) -> tuple[str, str]:
        """
        Parse emulators.cfg for a system and return the emulator name
        and its command template.

        If preferred_core is given (from a per-game override), return
        that core's command template instead of the system default.

        Args:
            system:         System name e.g. 'arcade', 'n64'
            preferred_core: Optional core name to prefer over the default
                            e.g. 'lr-mame2010'

        Returns:
            Tuple of (core_name, command_template) or ('', '') if
            the file cannot be read.
        """
        cfg_system = self.SYSTEM_CONFIG_MAP.get(system, system)
        cfg_path = f"/opt/retropie/configs/{cfg_system}/emulators.cfg"
        try:
            default  = None
            commands = {}
            with open(cfg_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('default'):
                        default = line.split('=', 1)[1].strip().strip('"')
                    elif '=' in line:
                        name, cmd = line.split('=', 1)
                        commands[name.strip()] = cmd.strip().strip('"')

            # Prefer the per-game override core if it has a command entry
            if preferred_core and preferred_core in commands:
                return preferred_core, commands[preferred_core]
            if default and default in commands:
                return default, commands[default]
        except Exception as e:
            log(f"  Warning: could not read emulators.cfg for {system}: {e}")
        return '', ''

    def _write_appendconfig(self) -> None:
        """
        Write /dev/shm/retroarch.cfg as runcommand.sh normally would.

        Includes cache_directory which enables RetroArch to extract ZIP
        archives before loading them — required for cores such as
        lr-nestopia, lr-gambatte and lr-mgba that do not support loading
        directly from ZIP files. Without this setting, those cores fail
        with [ERROR] Failed to load content when given a .zip path.

        Screen resolution is detected at startup via _detect_screen_resolution
        so this works correctly across all display configurations.
        """
        os.makedirs('/tmp/retroarch', exist_ok=True)

        lines = [
            'video_refresh_rate = "60"\n',
            'video_fullscreen = "true"\n',
            f'video_fullscreen_x = "{self._screen_width}"\n',
            f'video_fullscreen_y = "{self._screen_height}"\n',
            'cache_directory = "/tmp/retroarch"\n',
        ]
        try:
            with open('/dev/shm/retroarch.cfg', 'w') as f:
                f.writelines(lines)
        except Exception as e:
            log(f"  Warning: could not write appendconfig: {e}")

    def build_launch_cmd(
        self,
        system: str,
        rom: str,
        core_path: str = None
    ) -> list[str]:
        """
        Build the RetroArch launch command for a ROM.

        Parses emulators.cfg to find the default emulator command,
        substitutes the ROM path, and adds --verbose for log output.

        If core_path is provided (used during autofix), replaces the
        -L argument with the specified core .so path.

        Args:
            system:    System name e.g. 'arcade'
            rom:       Full path to the ROM file
            core_path: Optional override for the -L core library path

        Returns:
            Complete command list for subprocess.Popen.
        """
        self._write_appendconfig()

        # Check for a per-game override in the global emulators.cfg.
        # This mirrors exactly what runcommand.sh does when launching
        # from ES — without this, a ROM fixed by autofix would fall
        # back to the system default on recheck and fail again.
        romname          = os.path.basename(rom)
        override_core    = (
            self._get_pergame_override(system, romname)
            if core_path is None else None
        )
        if override_core:
            log(f"  Per-game override found: {override_core}")

        resolved_core, cmd_template = self._parse_emulators_cfg(
            system,
            preferred_core=override_core
        )
        # Log which core is actually being used for this launch.
        # Previously this only logged when a per-game override applied —
        # the common case (system default core, no override yet) produced
        # no log line at all, so it was impossible to tell from the log
        # which core a "default" test actually used. This caused real
        # confusion: a core that IS installed and IS the configured
        # system default (and therefore correctly excluded from autofix's
        # list of alternatives to try, since it was already tested here)
        # looked indistinguishable from a core that was never tried at all.
        # Guarded to core_path is None — during an autofix attempt
        # (core_path provided), attempt_autofix() already logs which
        # core is being tried, so logging the original default/override
        # core name here too would be actively misleading.
        if core_path is None and resolved_core:
            log(f"  Core: {resolved_core}")

        if cmd_template:
            cmd = cmd_template.replace('%ROM%', shlex.quote(rom))
            parts = shlex.split(cmd)

            # Override core if specified (autofix)
            if core_path and '-L' in parts:
                idx = parts.index('-L')
                parts[idx + 1] = core_path

            # Add verbose output for launch indicator detection
            if '--verbose' not in parts:
                parts.append('--verbose')
                # Include appendconfig if it exists
                if os.path.exists('/dev/shm/retroarch.cfg'):
                    parts.extend(['--appendconfig', '/dev/shm/retroarch.cfg'])
                return parts


        # Fallback — direct RetroArch with no specific core
        log(f"  Warning: no emulators.cfg found for {system}, using fallback")
        cmd = [
            "/opt/retropie/emulators/retroarch/bin/retroarch",
            "--config", f"/opt/retropie/configs/{system}/retroarch.cfg",
            "--verbose",
            rom
        ]
        if core_path:
            cmd = [
                "/opt/retropie/emulators/retroarch/bin/retroarch",
                "-L", core_path,
                "--config", f"/opt/retropie/configs/{system}/retroarch.cfg",
                "--verbose",
                rom
            ]
        return cmd

    def get_env(self) -> dict[str, str]:
        """
        Return the environment for launching RetroArch.
        DISPLAY is excluded — RetroPie runs in KMS/DRM mode without X11.
        """
        env = os.environ.copy()
        env.update({
            'HOME':            self._retropie_home,
            'XDG_RUNTIME_DIR': '/run/user/1000',
        })
        env.pop('DISPLAY', None)
        return env

    def get_launcher_cmd(self) -> list[str]:
        """Interface compliance — use build_launch_cmd() instead."""
        return ["/opt/retropie/emulators/retroarch/bin/retroarch"]

    # ------------------------------------------------------------------
    # Autofix — core swapping
    # ------------------------------------------------------------------

    def get_autofix_combinations(self, system: str) -> list[tuple]:
        """
        Return available core combinations to try for a system.

        Checks which cores are actually installed before returning them.
        Only returns combinations where the core .so file exists on disk.

        Args:
            system: System folder name e.g. 'arcade'

        Returns:
            List of (display_name, core_so_path) tuples for installed cores,
            excluding the currently configured default core.
        """
        combinations = self.SYSTEM_CORE_COMBINATIONS.get(system, [])
        if not combinations:
            return []

        # Get the currently configured core to exclude it from attempts
        _, current_cmd = self._parse_emulators_cfg(system)
        current_core = ''
        if current_cmd and '-L' in current_cmd:
            parts = shlex.split(current_cmd)
            if '-L' in parts:
                current_core = parts[parts.index('-L') + 1]

        available = []
        for display_name, core_dir, core_so in combinations:
            core_path = os.path.join(
                self.LIBRETRO_CORES_PATH, core_dir, core_so
            )
            if os.path.exists(core_path) and core_path != current_core:
                available.append((display_name, core_path))

        return available

    # ------------------------------------------------------------------
    # Autofix and MAME cfg creation
    # ------------------------------------------------------------------

    def attempt_autofix(
        self,
        system: str,
        rom: str,
        romname: str,
        dashboard,
        state: dict,
        **kwargs
    ) -> tuple[str, str]:
        """
        Attempt to fix a failing ROM by trying alternative MAME/FBA
        cores. On success, writes the MAME per-game cfg file so the
        game initialises correctly when launched via EmulationStation.

        Returns:
            ('FIXED', notes)           - Working core found, cfg written
            ('GENUINE ERROR', notes)   - All cores failed
            ('NO COMBINATIONS', notes) - No alternative cores available
        """
        core_combos = self.get_autofix_combinations(system)

        if not core_combos:
            return (
                'NO COMBINATIONS',
                f"No alternative cores installed for [{system}]"
            )

        log(f"  RetroPie autofix: trying "
            f"{len(core_combos)} alternative core(s)...")

        from modules.common.filehandling import get_cpu_temp  # noqa

        orig_build = self.build_launch_cmd
        total = len(core_combos)

        for i, (core_name, core_path) in enumerate(core_combos, 1):
            log(f"  [{i}/{total}] Trying: {core_name}")
            state['current_status'] = f"Autofix: {core_name}"
            dashboard.update(state)

            # Temporarily override build_launch_cmd for this core
            def make_cmd(cp, ob):
                def cmd(s, r):
                    return ob(s, r, core_path=cp)
                return cmd
            self.build_launch_cmd = make_cmd(core_path, orig_build)

            timeout = kwargs.get(
                'timeout',
                kwargs.get('slow_timeouts', {}).get(system, 20)
            )
            try:
                fix_status, fix_notes, fix_elapsed = self.run_test(
                    system, rom, dashboard, state, timeout=timeout
                )
            finally:
                self.build_launch_cmd = orig_build

            log(f"    Result: {fix_status} ({fix_elapsed:.1f}s) {fix_notes}")

            if fix_status == 'OK':
                self.write_mame_cfg(system, romname, core_name)
                notes = f"Fixed: {core_name} — MAME cfg created"
                log(f"  Fixed with: {core_name}")
                return 'FIXED', notes

        return 'GENUINE ERROR', "All core combinations failed"

    def log_autofix_availability(self) -> None:
        """
        Log which core combinations are available for autofix on
        this RetroPie installation.
        """
        log("RetroPie autofix core availability:")
        for system, combinations in self.SYSTEM_CORE_COMBINATIONS.items():
            _, current_cmd = self._parse_emulators_cfg(system)
            available = []
            for display_name, core_dir, core_so in combinations:
                core_path = os.path.join(
                    self.LIBRETRO_CORES_PATH, core_dir, core_so
                )
                if os.path.exists(core_path):
                    is_default = bool(current_cmd and core_so in current_cmd)
                    label = (
                        f"{display_name} (current default)"
                        if is_default else display_name
                    )
                    available.append(label)
            if available:
                alternatives = [
                    a for a in available if 'current default' not in a
                ]
                log(f"  [{system}] {len(available)} installed: "
                    f"{', '.join(available)}")
                if not alternatives:
                    log(f"  [{system}] No alternative cores — "
                        f"install more via RetroPie-Setup")

    def _remove_stale_mame_cfgs(
        self,
        system: str,
        romname: str,
        keep_dir: str
    ) -> None:
        """
        Remove stale MAME cfg files for a ROM from other core directories.

        When a ROM is fixed with a specific core, any cfg files created
        by previously tested cores are removed to prevent them overriding
        the working configuration.

        Args:
            system:   System folder name e.g. 'arcade'
            romname:  ROM filename e.g. '19xxa.zip'
            keep_dir: Core cfg directory to preserve e.g. 'mame2010'
        """
        rom_base  = os.path.splitext(romname)[0]
        roms_dir  = os.path.join(self.roms_path, system)

        for display_name, core_dir, _ in self.MAME_CORE_COMBINATIONS:
            mame_dir = core_dir.replace('lr-', '', 1)
            if mame_dir == keep_dir:
                continue
            stale = os.path.join(roms_dir, mame_dir, 'cfg', f'{rom_base}.cfg')
            if os.path.exists(stale):
                try:
                    os.remove(stale)
                    log(f"  Removed stale cfg: {stale}")
                except Exception as e:
                    log(f"  Warning: could not remove {stale}: {e}")

    def _write_game_override(
        self,
        system: str,
        romname: str,
        core_name: str
    ) -> bool:
        """
        Write a per-game emulator override to the global emulators.cfg.

        RetroPie stores per-game emulator overrides in:
            /opt/retropie/configs/all/emulators.cfg

        Format: <system>_<romname_without_extension> = "core_name"
        e.g.:   arcade_9ballshtc = "lr-mame2010"
        """
        cfg_path = "/opt/retropie/configs/all/emulators.cfg"
        rom_base = os.path.splitext(romname)[0]
        key      = f"{system}_{rom_base}"
        entry    = f'{key} = "{core_name}"\n'

        try:
            if os.path.exists(cfg_path):
                with open(cfg_path, 'r') as f:
                    lines = f.readlines()
            else:
                lines = []

            # Remove any existing override for this ROM
            lines = [
                l for l in lines
                if not l.strip().startswith(f'{key} =')
            ]
            lines.append(entry)

            with open(cfg_path, 'w') as f:
                f.writelines(lines)
            log(f"  Override written: {entry.strip()} → {cfg_path}")
            return True
        except Exception as e:
            log(f"  Warning: could not write override to {cfg_path}: {e}")
            return False

    def write_mame_cfg(
        self,
        system: str,
        romname: str,
        core_name: str
    ) -> bool:
        """
        Write MAME per-game config file so the game initialises
        correctly when launched via EmulationStation.

        MAME cores look for a per-game cfg file in a subdirectory of
        the ROM folder. Creating this file replicates what MAME does
        automatically on a successful first launch.

        Directory structure created:
            <roms_path>/<system>/<core_dir>/cfg/<romname>.cfg
            <roms_path>/<system>/<core_dir>/cfg/default.cfg

        Args:
            system:    System folder name e.g. 'arcade'
            romname:   ROM filename e.g. '1945kiii.zip'
            core_name: Core that works e.g. 'lr-mame2010'

        Returns:
            True if cfg file written successfully.
        """
        # Find core_dir from MAME_CORE_COMBINATIONS
        core_dir = None
        for display_name, cd, core_so in self.MAME_CORE_COMBINATIONS:
            if display_name == core_name:
                core_dir = cd
                break

        if not core_dir:
            log(f"  No core entry found for '{core_name}' — "
                f"skipping cfg creation")
            return False

        # MAME cfg subdirectory strips 'lr-' prefix
        # lr-mame2010 → mame2010, lr-mame2003 → mame2003
        mame_dir = core_dir.replace('lr-', '', 1)
        rom_base = os.path.splitext(romname)[0]
        cfg_dir  = os.path.join(
            self.roms_path, system, mame_dir, 'cfg'
        )
        os.makedirs(cfg_dir, exist_ok=True)

        game_cfg = (
            '<?xml version="1.0"?>\n'
            '<!-- This file is autogenerated; '
            'comments and unknown tags will be stripped -->\n'
            '<mameconfig version="10">\n'
            f'    <system name="{rom_base}" />\n'
            '</mameconfig>\n'
        )
        default_cfg = (
            '<?xml version="1.0"?>\n'
            '<!-- This file is autogenerated; '
            'comments and unknown tags will be stripped -->\n'
            '<mameconfig version="10">\n'
            '    <system name="default" />\n'
            '</mameconfig>\n'
        )

        try:
            game_path = os.path.join(cfg_dir, f'{rom_base}.cfg')
            with open(game_path, 'w') as f:
                f.write(game_cfg)
            log(f"  Created MAME cfg: {game_path}")

            default_path = os.path.join(cfg_dir, 'default.cfg')
            if not os.path.exists(default_path):
                with open(default_path, 'w') as f:
                    f.write(default_cfg)
                log(f"  Created default cfg: {default_path}")

            # After writing the new cfg, remove stale cfg files
            # for the same ROM in other core directories
            self._remove_stale_mame_cfgs(system, romname, mame_dir)

            # Write per-game emulators.cfg override only if working
            # core differs from the system default
            default_name, _ = self._parse_emulators_cfg(system)
            if default_name and default_name != core_name:
                log(f"  Working core differs from default "
                    f"({default_name}) — writing per-game override")
                self._write_game_override(system, romname, core_name)

            return True

        except Exception as e:
            log(f"  Warning: could not write MAME cfg: {e}")
            return False

    # ------------------------------------------------------------------
    # EmulationStation management
    # ------------------------------------------------------------------

    def capture_screenshot(self, dest_path: str) -> bool:
        """
        Capture the current screen on RetroPie.

        Tries three methods in order:
          1. RetroArch network SCREENSHOT command (port 55355) — most
             reliable for libretro/RetroArch games. Requires
             network_cmd_enable=true in retroarch.cfg, which
             pre_audit() sets automatically.
          2. PIL framebuffer read from /dev/fb0 — works for standalone
             emulators that write directly to the framebuffer.
          3. Returns False — screenshot not available for this game.

        Args:
            dest_path: Full path for the output PNG file.

        Returns:
            True if capture succeeded and the file exists, False otherwise.
        """
        import os
        import glob
        import time
        import shutil
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        # ------------------------------------------------------------------
        # Method 1: RetroArch network SCREENSHOT command
        # Sends a UDP packet to RetroArch's command interface.
        # RetroArch saves to its screenshot directory; we find the new
        # file and move it to dest_path.
        # ------------------------------------------------------------------
        ra_screenshot_dir = os.path.expanduser(
            '~/.config/retroarch/screenshots'
        )
        os.makedirs(ra_screenshot_dir, exist_ok=True)

        try:
            import socket
            # Glob all file types — RetroArch may save as PNG, JPEG or BMP
            # depending on configuration
            before = set(glob.glob(os.path.join(ra_screenshot_dir, '*.*')))
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b'SCREENSHOT', ('127.0.0.1', 55355))
            sock.close()

            # Wait up to 3s for RetroArch to write the file
            deadline = time.time() + 3
            new_file  = None
            while time.time() < deadline:
                time.sleep(0.25)
                after = set(glob.glob(
                    os.path.join(ra_screenshot_dir, '*.*')
                ))
                new_files = after - before
                if new_files:
                    new_file = sorted(new_files)[-1]
                    # Brief pause to let RetroArch finish writing
                    time.sleep(0.5)
                    break

            if new_file and os.path.exists(new_file):
                shutil.move(new_file, dest_path)
                log("  Screenshot: captured via RetroArch")
                return True
        except Exception:
            pass

        # ------------------------------------------------------------------
        # Method 2: PIL framebuffer — reads /dev/fb0 directly
        # Works for standalone emulators (mupen64plus, amiberry etc.)
        # that render to the framebuffer rather than through RetroArch.
        # Reads 16-bit RGB565 as reported by fbset.
        # ------------------------------------------------------------------
        try:
            from PIL import Image
            import struct

            fb_path = '/dev/fb0'
            if not os.path.exists(fb_path):
                raise FileNotFoundError('/dev/fb0 not found')

            # Read resolution from fbset output
            width, height, bpp = 1920, 1080, 16   # safe defaults
            try:
                result = subprocess.run(
                    ['fbset', '-s'], capture_output=True, timeout=3
                )
                for line in result.stdout.decode(errors='replace').splitlines():
                    if 'geometry' in line:
                        parts = line.split()
                        if len(parts) >= 5:
                            width  = int(parts[1])
                            height = int(parts[2])
                            bpp    = int(parts[5])
                        break
            except Exception:
                pass

            with open(fb_path, 'rb') as f:
                raw = f.read(width * height * (bpp // 8))

            if bpp == 16:
                # RGB565 → RGB888
                pixels = bytearray(width * height * 3)
                for i in range(width * height):
                    v = struct.unpack_from('<H', raw, i * 2)[0]
                    pixels[i * 3]     = (v >> 11 & 0x1F) << 3
                    pixels[i * 3 + 1] = (v >> 5  & 0x3F) << 2
                    pixels[i * 3 + 2] = (v        & 0x1F) << 3
                img = Image.frombytes('RGB', (width, height), bytes(pixels))
            elif bpp == 32:
                img = Image.frombytes('RGBA', (width, height), raw)
                img = img.convert('RGB')
            else:
                raise ValueError(f"Unsupported bpp: {bpp}")

            img.save(dest_path)
            log("  Screenshot: captured via PIL framebuffer")
            return True

        except ImportError:
            log("  Screenshot: PIL not available for framebuffer capture")
        except Exception as e:
            log(f"  Screenshot: framebuffer capture failed — {e}")

        log("  Screenshot: no capture method succeeded")
        return False

    @property
    def emulator_processes(self) -> list[str]:
        """
        RetroPie emulator process names to kill after each ROM test.

        Covers RetroArch (libretro cores) and all known standalone
        emulators available on RetroPie. Add entries here when new
        standalone emulators are installed.
        """
        return [
            'retroarch',    # RetroArch / libretro — all systems
            'amiberry',     # Amiga standalone
            'uae4arm',      # Amiga alternative
            'hatari',       # Atari ST standalone
            'advmame',      # AdvanceMAME
            'ppsspp',       # PSP standalone
            'mupen64plus',  # N64 standalone
        ]

    def get_launch_timeout(self, system: str) -> int | None:
        """
        Return launch timeout including RetroPie-specific system names.

        RetroPie uses different folder names for some arcade systems
        (arcade, mame-libretro, fba) that don't exist on other platforms.
        """
        RETROPIE_TIMEOUTS: dict[str, int] = {
            'arcade':       20,   # RetroPie MAME — fast crash or load
            'mame-libretro': 10,  # RetroPie MAME libretro
            'fba':          10,   # RetroPie FBNeo
        }
        return RETROPIE_TIMEOUTS.get(system) or super().get_launch_timeout(system)

    def pre_audit(self) -> None:
        """
        Prepare RetroPie for ROM testing.

        Stops EmulationStation to release the KMS/DRM display, and
        enables RetroArch network commands in retroarch.cfg so that
        the screenshot capture can send SCREENSHOT via UDP port 55355.
        Network commands are enabled permanently — they are low-risk
        (localhost only) and useful for debugging outside the audit.
        """
        self._enable_retroarch_network_cmd()
        self.stop_emulationstation()

    def _enable_retroarch_network_cmd(self) -> None:
        """
        Ensure network_cmd_enable = "true" in the global retroarch.cfg.

        Must be called before RetroArch launches — the setting is read
        once at startup and cannot be changed mid-game. Safe to call
        multiple times; no-op if already enabled.
        """
        cfg_path = '/opt/retropie/configs/all/retroarch.cfg'
        if not os.path.exists(cfg_path):
            return
        try:
            with open(cfg_path, 'r') as f:
                content = f.read()
            if 'network_cmd_enable = "true"' in content:
                return   # Already enabled
            # Replace false with true, or add the setting
            if 'network_cmd_enable' in content:
                content = content.replace(
                    'network_cmd_enable = "false"',
                    'network_cmd_enable = "true"'
                )
            else:
                content += '\nnetwork_cmd_enable = "true"\n'
            with open(cfg_path, 'w') as f:
                f.write(content)
            log("  RetroArch network commands enabled for screenshot capture")
        except Exception as e:
            log(f"  Warning: could not enable RetroArch network commands: {e}")

    def post_audit(self) -> None:
        """
        Clean up after ROM testing. Notifies user to restart ES manually
        since ES cannot be reliably restarted from an SSH session with
        proper VT/DRM context.
        """
        self.start_emulationstation()

    def stop_emulationstation(self) -> None:
        """
        Stop EmulationStation to release the KMS/DRM display.

        The audit cannot launch ROMs while EmulationStation holds the
        display. Sends SIGTERM to all ES processes, waits for them to
        exit, then sends SIGKILL if any remain.
        """
        result = self._safe_run(
            ['pgrep', '-f',
             'supplementary/emulationstation/emulationstation'],
            capture=True
        )
        if result is None or result.returncode != 0:
            log("EmulationStation is not running.")
            return

        pids = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
        log(f"Stopping EmulationStation (PIDs: {', '.join(pids)})...")

        for pid in pids:
            self._safe_run(['kill', '-TERM', pid])

        time.sleep(5)

        # Verify stopped — send KILL to anything remaining
        check = self._safe_run(
            ['pgrep', '-f',
             'supplementary/emulationstation/emulationstation'],
            capture=True
        )
        if check is not None and check.returncode == 0:
            remaining = [
                p.strip()
                for p in check.stdout.strip().split('\n')
                if p.strip()
            ]
            log(f"ES still running, sending KILL to: {', '.join(remaining)}")
            for pid in remaining:
                self._safe_run(['kill', '-KILL', pid])
            time.sleep(2)

        log("EmulationStation stopped.")

    @staticmethod
    def _safe_run(cmd: list[str], timeout: float = 10, capture: bool = False):
        """
        Run a short-lived system command with a hard timeout.

        Several cleanup commands here (chvt in particular) can block
        indefinitely under unusual VT/DRM state rather than erroring —
        confirmed in practice: a stuck chvt call after a long audit
        left the process alive with no further output and no way to
        recover except killing it manually. Every subprocess call in
        this class goes through this rather than a bare
        subprocess.run() so a single stuck call can never prevent the
        script from finishing and writing its summary, even ones (like
        pgrep) that are normally near-instant and wouldn't seem to need it.

        Returns the CompletedProcess on success, or None if the
        command timed out or failed to start. Set capture=True when
        the caller needs .returncode/.stdout (e.g. pgrep checks);
        leave it False for fire-and-forget commands.
        """
        try:
            return subprocess.run(
                cmd, capture_output=capture, text=capture, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            log(f"  Warning: '{' '.join(cmd)}' timed out after "
                f"{timeout}s — continuing without it.")
            return None
        except Exception as e:
            log(f"  Warning: '{' '.join(cmd)}' failed: {e}")
            return None

    def start_emulationstation(self) -> None:
        """
        Clean up after audit. ES must be restarted manually on tty1
        to ensure proper VT/DRM context for launching games.

        Every step here is best-effort cleanup, not a requirement for
        the audit to be considered complete — the CSV and summary are
        already written by this point regardless of what happens
        below. Each command is timeout-protected via _safe_run() so
        none of them can block the script from exiting.
        """
        self._safe_run(['pkill', '-9', 'retroarch'])
        time.sleep(2)

        self._safe_run(['tput', 'cnorm'])
        self._safe_run(['tput', 'reset'])
        self._safe_run(['stty', 'sane'])
        self._safe_run(['chvt', '1'], timeout=5)

        log("=" * 60)
        log("AUDIT COMPLETE — ACTION REQUIRED")
        log("=" * 60)
        log("EmulationStation must be restarted manually.")
        log("On the Pi console (tty1) type:")
        log("  emulationstation")
        log("Or reboot the Pi:")
        log("  sudo reboot")
        log("=" * 60)
