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
import re
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
        # RetroArch / libretro — core loaded from disk
        "[INFO] Loading dynamic libretro core from:",
        # RetroArch — DRM display found, video initialised
        "[INFO] [DRM]: Found",
        # RetroArch — GL context confirmed
        "[INFO] [GL]: Found GL context:",
        # RetroArch — video display server
        "[INFO] [Video]: Found display server:",
        # AdvanceMAME — ROM found and loaded
        "rom/",                 # advmame logs "Loading rom/romname"
        "game/",                # advmame logs "Loading game/romname"
        # AdvanceMAME — video mode set, display initialised
        "mame: starting",       # advmame startup message
        "Starting game",        # advmame ROM launch
        # Generic standalone emulators — process confirmed alive
        # (advmame, amiberry, etc. may not log anything we can intercept
        # before proc.poll() confirms the process is still running)
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
        ('lr-mame2000',     'lr-mame2000',     'mame2000_libretro.so'),
        ('lr-mame2003',     'lr-mame2003',     'mame2003_libretro.so'),
        ('lr-mame2003-plus','lr-mame2003-plus', 'mame2003_plus_libretro.so'),
        ('lr-mame2010',     'lr-mame2010',     'mame2010_libretro.so'),
        ('lr-mame2015',     'lr-mame2015',     'mame2015_libretro.so'),
        ('lr-mame2016',     'lr-mame2016',     'mame2016_libretro.so'),
        ('lr-mame',         'lr-mame',         'mame_libretro.so'),
        ('lr-mess',         'lr-mess',         'mess_libretro.so'),
        ('lr-mess2016',     'lr-mess2016',     'mess2016_libretro.so'),
    ]

    FBA_CORE_COMBINATIONS = [
        ('lr-fba',          'lr-fba',          'fba_libretro.so'),
        ('lr-fbalpha2012',  'lr-fbalpha2012',  'fbalpha2012_libretro.so'),
        ('lr-fbneo',        'lr-fbneo',        'fbneo_libretro.so'),
        ('lr-fbneo-neocd',  'lr-fbneo',        'fbneo_libretro.so'),
        ('lr-neocd',        'lr-neocd',        'neocd_libretro.so'),
        ('lr-flycast',      'lr-flycast',      'flycast_libretro.so'),
    ]

    ARCADE_CORE_COMBINATIONS = MAME_CORE_COMBINATIONS + FBA_CORE_COMBINATIONS

    SYSTEM_CORE_COMBINATIONS = {
        'arcade':           ARCADE_CORE_COMBINATIONS,
        'mame-libretro':    MAME_CORE_COMBINATIONS,
        'fba':              FBA_CORE_COMBINATIONS,

        # NeoGeo
        'neogeo': [
            ('lr-fbneo',       'lr-fbneo',       'fbneo_libretro.so'),
            ('lr-fbalpha2012', 'lr-fbalpha2012', 'fbalpha2012_libretro.so'),
            ('lr-neocd',       'lr-neocd',       'neocd_libretro.so'),
        ],

        # ColecoVision
        'coleco': [
            ('lr-fbneo-cv', 'lr-fbneo',   'fbneo_libretro.so'),
            ('lr-bluemsx',  'lr-bluemsx', 'bluemsx_libretro.so'),
        ],

        # MSX / MSX2
        'msx': [
            ('lr-bluemsx', 'lr-bluemsx', 'bluemsx_libretro.so'),
            ('lr-fmsx',    'lr-fmsx',    'fmsx_libretro.so'),
        ],
        'msx2': [
            ('lr-bluemsx', 'lr-bluemsx', 'bluemsx_libretro.so'),
            ('lr-fmsx',    'lr-fmsx',    'fmsx_libretro.so'),
        ],

        # Amstrad CPC
        'amstradcpc': [
            ('lr-caprice32',  'lr-caprice32', 'cap32_libretro.so'),
            ('lr-theodore',   'lr-theodore',  'theodore_libretro.so'),
        ],

        # Atari 800
        'atari800': [
            ('lr-atari800', 'lr-atari800', 'atari800_libretro.so'),
        ],

        # Atari 2600
        'atari2600': [
            ('lr-stella2014', 'lr-stella2014', 'stella2014_libretro.so'),
            ('lr-stella',     'lr-stella',     'stella_libretro.so'),
        ],

        # Atari 7800
        'atari7800': [
            ('lr-prosystem', 'lr-prosystem', 'prosystem_libretro.so'),
        ],

        # Atari Lynx
        'atarilynx': [
            ('lr-beetle-lynx', 'lr-beetle-lynx', 'mednafen_lynx_libretro.so'),
            ('lr-handy',       'lr-handy',        'handy_libretro.so'),
        ],

        # NES
        'nes': [
            ('lr-fceumm',   'lr-fceumm',   'fceumm_libretro.so'),
            ('lr-nestopia', 'lr-nestopia', 'nestopia_libretro.so'),
            ('lr-quicknes', 'lr-quicknes', 'quicknes_libretro.so'),
            ('lr-mesen',    'lr-mesen',    'mesen_libretro.so'),
        ],

        # SNES
        'snes': [
            ('lr-snes9x',    'lr-snes9x',    'snes9x_libretro.so'),
            ('lr-snes9x2010','lr-snes9x2010','snes9x2010_libretro.so'),
            ('lr-snes9x2005','lr-snes9x2005','snes9x2005_libretro.so'),
            ('lr-snes9x2002','lr-snes9x2002','snes9x2002_libretro.so'),
            ('lr-bsnes',     'lr-bsnes',     'bsnes_libretro.so'),
        ],

        # Game Boy / GBC
        'gb': [
            ('lr-gambatte', 'lr-gambatte', 'gambatte_libretro.so'),
            ('lr-tgbdual',  'lr-tgbdual',  'tgbdual_libretro.so'),
            ('lr-mgba',     'lr-mgba',     'mgba_libretro.so'),
        ],
        'gbc': [
            ('lr-gambatte', 'lr-gambatte', 'gambatte_libretro.so'),
            ('lr-tgbdual',  'lr-tgbdual',  'tgbdual_libretro.so'),
            ('lr-mgba',     'lr-mgba',     'mgba_libretro.so'),
        ],

        # Game Boy Advance
        'gba': [
            ('lr-mgba',    'lr-mgba',    'mgba_libretro.so'),
            ('lr-gpsp',    'lr-gpsp',    'gpsp_libretro.so'),
            ('lr-vba-next','lr-vba-next','vba_next_libretro.so'),
        ],

        # Mega Drive / Genesis
        'megadrive': [
            ('lr-genesis-plus-gx','lr-genesis-plus-gx','genesis_plus_gx_libretro.so'),
            ('lr-picodrive',      'lr-picodrive',      'picodrive_libretro.so'),
        ],
        'genesis': [
            ('lr-genesis-plus-gx','lr-genesis-plus-gx','genesis_plus_gx_libretro.so'),
            ('lr-picodrive',      'lr-picodrive',      'picodrive_libretro.so'),
        ],

        # Mega CD / Sega CD
        'segacd': [
            ('lr-genesis-plus-gx','lr-genesis-plus-gx','genesis_plus_gx_libretro.so'),
            ('lr-picodrive',      'lr-picodrive',      'picodrive_libretro.so'),
        ],

        # 32X
        '32x': [
            ('lr-picodrive', 'lr-picodrive', 'picodrive_libretro.so'),
        ],

        # Master System / Game Gear
        'mastersystem': [
            ('lr-genesis-plus-gx','lr-genesis-plus-gx','genesis_plus_gx_libretro.so'),
            ('lr-picodrive',      'lr-picodrive',      'picodrive_libretro.so'),
            ('lr-smsplus-gx',     'lr-smsplus-gx',     'smsplus_libretro.so'),
        ],
        'gamegear': [
            ('lr-genesis-plus-gx','lr-genesis-plus-gx','genesis_plus_gx_libretro.so'),
            ('lr-gearsystem',     'lr-gearsystem',     'gearsystem_libretro.so'),
        ],

        # PC Engine / TurboGrafx
        'pcengine': [
            ('lr-beetle-pce-fast',   'lr-beetle-pce-fast',   'mednafen_pce_fast_libretro.so'),
            ('lr-beetle-pce',        'lr-beetle-pce',        'mednafen_pce_libretro.so'),
            ('lr-beetle-supergrafx', 'lr-beetle-supergrafx', 'mednafen_supergrafx_libretro.so'),
            ('lr-geargrafx',         'lr-geargrafx',         'geargrafx_libretro.so'),
        ],

        # PlayStation
        'psx': [
            ('lr-pcsx-rearmed', 'lr-pcsx-rearmed', 'pcsx_rearmed_libretro.so'),
        ],

        # N64
        'n64': [
            ('lr-mupen64plus',      'lr-mupen64plus',      'mupen64plus_libretro.so'),
            ('lr-mupen64plus-next', 'lr-mupen64plus-next', 'mupen64plus_next_libretro.so'),
            ('lr-parallel-n64',     'lr-parallel-n64',     'parallel_n64_libretro.so'),
        ],

        # Dreamcast
        'dreamcast': [
            ('lr-flycast', 'lr-flycast', 'flycast_libretro.so'),
        ],

        # NDS
        'nds': [
            ('lr-desmume',     'lr-desmume',     'desmume_libretro.so'),
            ('lr-desmume2015', 'lr-desmume2015', 'desmume2015_libretro.so'),
        ],

        # ZX81
        'zx81': [
            ('lr-81', 'lr-81', '81_libretro.so'),
        ],

        # ZX Spectrum
        'zxspectrum': [
            ('lr-fuse', 'lr-fuse', 'fuse_libretro.so'),
        ],

        # Amiga
        'amiga': [
            ('lr-puae',     'lr-puae',     'puae_libretro.so'),
            ('lr-puae2021', 'lr-puae2021', 'puae2021_libretro.so'),
            ('lr-uae4arm',  'lr-uae4arm',  'uae4arm_libretro.so'),
        ],

        # C64 / C128 / VIC-20 — lr-vice .so varies by machine
        'c64': [
            ('lr-vice', 'lr-vice', 'vice_x64_libretro.so'),
        ],
        'c128': [
            ('lr-vice', 'lr-vice', 'vice_x128_libretro.so'),
        ],
        'vic20': [
            ('lr-vice', 'lr-vice', 'vice_xvic_libretro.so'),
        ],

        # Vectrex

        # Odyssey 2 / Videopac
        'odyssey2': [
            ('lr-o2em', 'lr-o2em', 'o2em_libretro.so'),
        ],

        # Watara Supervision
        'supervision': [
            ('lr-potator', 'lr-potator', 'potator_libretro.so'),
        ],

        # Neo Geo Pocket
        'ngp': [
            ('lr-beetle-ngp', 'lr-beetle-ngp', 'mednafen_ngp_libretro.so'),
        ],
        'ngpc': [
            ('lr-beetle-ngp', 'lr-beetle-ngp', 'mednafen_ngp_libretro.so'),
        ],

        # WonderSwan
        'wonderswan': [
            ('lr-beetle-wswan', 'lr-beetle-wswan', 'mednafen_wswan_libretro.so'),
        ],
        'wonderswancolor': [
            ('lr-beetle-wswan', 'lr-beetle-wswan', 'mednafen_wswan_libretro.so'),
        ],

        # Virtual Boy
        'virtualboy': [
            ('lr-beetle-vb', 'lr-beetle-vb', 'mednafen_vb_libretro.so'),
        ],

        # Jaguar
        'jaguar': [
            ('lr-virtualjaguar', 'lr-virtualjaguar', 'virtualjaguar_libretro.so'),
        ],

        # Game & Watch
        'gameandwatch': [
            ('lr-gw', 'lr-gw', 'gw_libretro.so'),
        ],

        # Doom
        'ports': [
            ('lr-prboom',  'lr-prboom',  'prboom_libretro.so'),
            ('lr-tyrquake','lr-tyrquake','tyrquake_libretro.so'),
        ],

        # DOS
        'pc': [
            ('lr-dosbox',      'lr-dosbox',      'dosbox_libretro.so'),
            ('lr-dosbox-pure', 'lr-dosbox-pure', 'dosbox_pure_libretro.so'),
        ],

        # Saturn
        'saturn': [
            ('lr-beetle-saturn', 'lr-beetle-saturn', 'mednafen_saturn_libretro.so'),
            ('lr-yabause',       'lr-yabause',       'yabause_libretro.so'),
        ],

        # PSP
        'psp': [
            ('lr-ppsspp', 'lr-ppsspp', 'ppsspp_libretro.so'),
        ],

        # ScummVM
        'scummvm': [
            ('lr-scummvm', 'lr-scummvm', 'scummvm_libretro.so'),
        ],

        # Hatari (Atari ST)
        'atarist': [
            ('lr-hatari', 'lr-hatari', 'hatari_libretro.so'),
        ],

        # Sharp X1
        'x1': [
            ('lr-x1', 'lr-x1', 'x1_libretro.so'),
        ],

        # PC-88
        'pc88': [
            ('lr-quasi88', 'lr-quasi88', 'quasi88_libretro.so'),
        ],

        # PC-98
        'pc98': [
            ('lr-np2kai', 'lr-np2kai', 'np2kai_libretro.so'),
        ],

        # Sharp X68000
        'x68000': [
            ('lr-px68k', 'lr-px68k', 'px68k_libretro.so'),
        ],

        # 3DO
        '3do': [
            ('lr-opera', 'lr-opera', 'opera_libretro.so'),
        ],

        # Fairchild Channel F
        'channelf': [
            ('lr-freechaf', 'lr-freechaf', 'freechaf_libretro.so'),
        ],

        # Intellivision
        'intellivision': [
            ('lr-freeintv', 'lr-freeintv', 'freeintv_libretro.so'),
        ],

        # Pokémon Mini
        'pokemini': [
            ('lr-pokemini', 'lr-pokemini', 'pokemini_libretro.so'),
        ],

        # NEC PC-FX
        'pcfx': [
            ('lr-beetle-pcfx', 'lr-beetle-pcfx', 'mednafen_pcfx_libretro.so'),
        ],

        # SG-1000
        'sg-1000': [
            ('lr-genesis-plus-gx','lr-genesis-plus-gx','genesis_plus_gx_libretro.so'),
            ('lr-gearsystem',     'lr-gearsystem',     'gearsystem_libretro.so'),
        ],

        # TIC-80
        'tic80': [
            ('lr-tic80', 'lr-tic80', 'tic80_libretro.so'),
        ],

        # EP128
        'ep128': [
            ('lr-ep128emu', 'lr-ep128emu', 'ep128emu_libretro.so'),
        ],
    }

    # Lines that look like errors but are non-fatal MAME 2003 warnings
    NON_FATAL_MARKERS = [
        "cpunum_get_localtime() called for invalid cpu num",
        # lr-caprice32 CRTC geometry — informational only, game runs fine
        "[libretro-cap32]: Got size:",
        # RetroArch cloud sync not configured — harmless on local installs
        "Couldn't find any cloud sync driver",
        # GameMode not installed — optional performance feature, not required
        "GameMode cannot be enabled on this system",
        "GameMode unsupported - disabling",
        # Handy (lr-beetle-lynx) ROM format auto-detection — core guesses
        # the layout and loads successfully regardless. 216 frames pushed
        # confirmed on a real cart that triggered these warnings.
        "[Handy] Invalid Cart (type)",
        "[Handy] Invalid cart (no header?)",
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
        self._retroarch_verbose = self._probe_retroarch_verbose()
        self._es_binary = None   # set by stop_emulationstation()
        log(f"RetroPie v{self._version} detected")
        log(f"RetroPie home: {self._retropie_home}")
        if not self._retroarch_verbose:
            log("  Note: this RetroArch build does not support --verbose "
                "— launch indicators will rely on process detection only")

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
                ['git', '-C', '/home/pi/RetroPie-Setup', 'log', '-1',
                 '--pretty=format:%h'],
                capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0 and result.stdout.strip():
                return f"git-{result.stdout.strip()}"
        except Exception:
            pass
        return "unknown"

    def _probe_retroarch_verbose(self) -> bool:
        """
        Check whether this RetroArch binary accepts --verbose.

        Older RetroArch builds (pre-1.7 era, as shipped on some
        RetroPie 2022 images) reject it with 'Unknown command line
        option' — causing every launch to fail with that error in
        stderr before the game ever starts. Probing once at startup
        is cheaper than discovering this mid-audit on every ROM.

        Runs 'retroarch --help' and checks the output for '--verbose'.
        Falls back to True (assume supported) if the binary can't be
        found or the probe itself fails — that way a probe failure
        doesn't silently disable useful log output on builds that
        do support it.
        """
        import subprocess
        retroarch = '/opt/retropie/emulators/retroarch/bin/retroarch'
        if not os.path.exists(retroarch):
            return True   # not found — assume modern, fail gracefully later
        try:
            result = subprocess.run(
                [retroarch, '--help'],
                capture_output=True, text=True, timeout=5
            )
            combined = result.stdout + result.stderr
            return '--verbose' in combined
        except Exception:
            return True   # probe failed — assume supported

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

    def validate_rom_launch(
        self,
        system: str,
        rom: str
    ) -> tuple[bool, str]:
        """
        Pre-launch validation for RetroPie.

        Detects text-mode console games (CON: prefix in emulators.cfg)
        such as Zork/zmachine running via frotz. These games:
          - Launch instantly with no visual output on the TV screen
          - Require interactive stdin (can't be redirected to tty1)
          - Produce no launch indicators the tool can detect
          - Result in the full timeout wait before detection succeeds

        Rather than waste time waiting, detect them here and return
        NEEDS REVIEW with a clear explanation. The ROM itself may be
        perfectly valid — it just can't be tested by this tool.
        """
        romname = os.path.basename(rom)
        _, cmd_template = self._parse_emulators_cfg(system)
        if cmd_template:
            cmd = cmd_template.replace('%ROM%', shlex.quote(rom))
            if cmd.strip().startswith('CON:'):
                return False, (
                    f'Text-mode game (CON: launcher) — cannot be '
                    f'tested via this tool. These games require '
                    f'interactive stdin and produce no visual output '
                    f'detectable from SSH. Verify manually.'
                )
        return True, ''

    def get_configured_core(self, system: str, romname: str) -> str:
        """
        Resolve which libretro core this ROM will actually launch with.

        Reads emulators.cfg the same way build_launch_cmd() does, to
        detect whether the resolved core is in UNVERIFIED_CORES (e.g.
        fbneo). When it is, prepare_screenshot_path() forces a
        verification screenshot and post_process_result() routes the
        result through verify_unverified_core() — the same FBNeo
        grey-screen masking detection that exists on Batocera.

        Confirmed needed on RetroPie: Coleco ROMs launched via lr-fbneo
        (the default FBNeo core) showed the familiar "Romset is unknown"
        grey error screen and reported plain OK with no flag, because the
        base class returns '' and UNVERIFIED_CORES was never consulted.

        Returns:
            Core display name e.g. 'lr-fbneo', or '' if not determinable.
        """
        try:
            romname_only = os.path.basename(romname)
            override = self._get_pergame_override(system, romname_only)
            core_name = override if override else self._parse_emulators_cfg(system)[0]
            # FBNeo grey-screen masking only applies when running ROMs from
            # *other* systems through FBNeo (coleco, arcade etc.). NeoGeo is
            # FBNeo's native format — ROMs either work or fail with logged
            # errors. Don't trigger UNVERIFIED_CORES for native NeoGeo.
            NATIVE_FBNEO_SYSTEMS = {'neogeo', 'fba', 'neogeocd'}
            if system in NATIVE_FBNEO_SYSTEMS:
                return core_name or ''
            CORE_NAME_MAP = {
                'lr-fbneo':       'fbneo',
                'lr-fbneo-cv':    'fbneo',
                'lr-fbneo-neocd': 'fbneo',
                'lr-fba':         'fbneo',
                'lr-fbalpha2012': 'fbneo',
            }
            return CORE_NAME_MAP.get(core_name, core_name or '')
        except Exception:
            return ''

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
        NDS_DISPLAY_TIME = 15   # DRASTIC is a shell script launcher —
                                 # no RetroArch launch indicators fire,
                                 # process detection handles it instead.
                                 # Needs longer than the default to ensure
                                 # the emulator is genuinely running.
        RETROPIE_DEFAULT_DISPLAY_TIME = 5
        if system == 'gameandwatch':
            return GAMEANDWATCH_DISPLAY_TIME
        if system == 'nds':
            return NDS_DISPLAY_TIME
        return RETROPIE_DEFAULT_DISPLAY_TIME

    @property
    def non_fatal_post_launch_markers(self) -> list[str]:
        """
        Strings that look like errors but are non-fatal warnings.
        Games continue running correctly despite these appearing in the log.
        """
        return self.NON_FATAL_MARKERS

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

    def _write_appendconfig(self, core_dir: str = '') -> None:
        """
        Write /dev/shm/retroarch.cfg as runcommand.sh normally would.

        Includes cache_directory which enables RetroArch to extract ZIP
        archives before loading them — required for cores such as
        lr-nestopia, lr-gambatte and lr-mgba that do not support loading
        directly from ZIP files. Without this setting, those cores fail
        with [ERROR] Failed to load content when given a .zip path.

        core_dir: the libretro core directory e.g.
        '/opt/retropie/libretrocores/lr-bluemsx'. When set, writes
        libretro_directory which some cores (confirmed: lr-bluemsx)
        use to locate their BIOS/Machines folder. The UI's runcommand.sh
        sets this in its appendconfig; without it BlueMSX exits
        immediately because it cannot find the Machines/ subfolder.

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
            'network_cmd_enable = "true"\n',
            'network_cmd_port = "55355"\n',
            'video_driver = "gl"\n',
            'video_gpu_screenshot = "false"\n',
            f'system_directory = "{self._retropie_home}/RetroPie/BIOS"\n',
        ]

        if core_dir:
            lines.append(f'libretro_directory = "{core_dir}"\n')

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
        # Check for a per-game override in the global emulators.cfg.
        romname       = os.path.basename(rom)
        override_core = (
            self._get_pergame_override(system, romname)
            if core_path is None else None
        )
        if override_core:
            log(f"  Per-game override found: {override_core}")

        resolved_core, cmd_template = self._parse_emulators_cfg(
            system,
            preferred_core=override_core
        )

        # Extract the core directory for _write_appendconfig — some cores
        # (confirmed: lr-bluemsx) use libretro_directory to locate BIOS/
        # Machines folders. The UI's runcommand.sh sets this; without it
        # BlueMSX exits immediately.
        effective_core_path = core_path
        if not effective_core_path and cmd_template:
            import re as _re
            m = _re.search(r'-L\s+(\S+)', cmd_template)
            if m:
                effective_core_path = m.group(1)
        core_dir = os.path.dirname(effective_core_path) if effective_core_path else ''
        self._write_appendconfig(core_dir=core_dir)
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

            if cmd.startswith('CON:'):
                shell_cmd = cmd[4:].strip()
                return ['/bin/bash', '-lc', shell_cmd]

            # Commands using shell builtins (pushd/popd) or semicolons
            # must run via bash — shlex.split() would treat 'pushd' as
            # a literal binary. Confirmed: oricutron uses
            # "pushd /dir; ./binary args; popd" pattern.
            SHELL_INDICATORS = ('pushd ', 'popd', '; ', '&&', '||')
            if any(indicator in cmd for indicator in SHELL_INDICATORS):
                return ['/bin/bash', '-c', cmd]

            parts = shlex.split(cmd)

            # Override core if specified (autofix)
            if core_path and '-L' in parts:
                idx = parts.index('-L')
                parts[idx + 1] = core_path

            # Add verbose output for launch indicator detection —
            # only for RetroArch commands, and only if this build
            # supports the flag. advmame, amiberry, and other standalone
            # emulators don't accept --verbose and will fail with
            # "Unknown command line option" if it's blindly appended.
            is_retroarch = any(
                'retroarch' in p.lower() for p in parts[:2]
            )
            #if is_retroarch and self._retroarch_verbose \
            #        and '--verbose' not in parts:
            #    parts.append('--verbose')
            #    if os.path.exists('/dev/shm/retroarch.cfg'):
            #        parts.extend(['--appendconfig', '/dev/shm/retroarch.cfg'])
            if is_retroarch:
                if self._retroarch_verbose and '--verbose' not in parts:
                    parts.append('--verbose')

                if '--appendconfig' not in parts and os.path.exists('/dev/shm/retroarch.cfg'):
                    parts.extend(['--appendconfig', '/dev/shm/retroarch.cfg'])
            return parts


        # Fallback — direct RetroArch with no specific core
        log(f"  Warning: no emulators.cfg found for {system}, using fallback")
        verbose = ['--verbose'] if self._retroarch_verbose else []
        cmd = [
            "/opt/retropie/emulators/retroarch/bin/retroarch",
            "--config", f"/opt/retropie/configs/{system}/retroarch.cfg",
            *verbose,
            rom
        ]
        if core_path:
            cmd = [
                "/opt/retropie/emulators/retroarch/bin/retroarch",
                "-L", core_path,
                "--config", f"/opt/retropie/configs/{system}/retroarch.cfg",
                *verbose,
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

    def make_cmd(self, core_name: str, ob):
        """
        Return a temporary build_launch_cmd() wrapper for autofix.

        Resolve the complete emulator command for the specified emulator
        from emulators.cfg rather than simply substituting the libretro
        core path. This preserves emulator-specific command-line options
        such as subsystem arguments and wrapper scripts.

        Args:
            core_name: Emulator name to resolve from emulators.cfg.
            original_build: Original build_launch_cmd() implementation.

        Returns:
            Callable compatible with build_launch_cmd().
        """
    
        def cmd(s, r):
            resolved_core, cmd_template = self._parse_emulators_cfg(
                s,
                preferred_core=core_name
            )

            if not cmd_template:
                return ob(s, r, core_path=core_path)
            effective_core_path = ''
            parts_for_core = shlex.split(cmd_template)

            if '-L' in parts_for_core:
                effective_core_path = parts_for_core[
                    parts_for_core.index('-L') + 1
                ]

            core_dir = (
                os.path.dirname(effective_core_path)
                if effective_core_path else ''
            )

            self._write_appendconfig(core_dir=core_dir)
            cmdline = cmd_template.replace('%ROM%', shlex.quote(r))
            parts = shlex.split(cmdline)
            is_retroarch = any(
                'retroarch' in p.lower() for p in parts[:2]
            )

            if is_retroarch:
                if self._retroarch_verbose and '--verbose' not in parts:
                    parts.append('--verbose')
                if '--appendconfig' not in parts and os.path.exists('/dev/shm/retroarch.cfg'):
                    parts.extend(['--appendconfig', '/dev/shm/retroarch.cfg'])
            return parts

        return cmd
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
            #def make_cmd(cp, ob):
            #    def cmd(s, r):
            #        return ob(s, r, core_path=cp)
            #    return cmd
            #self.build_launch_cmd = make_cmd(core_path, orig_build)
            self.build_launch_cmd = self.make_cmd(core_name, orig_build)
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

            #if fix_status == 'OK':
            #    self.write_mame_cfg(system, romname, core_name)
            #    notes = f"Fixed: {core_name} — MAME cfg created"
            #    log(f"  Fixed with: {core_name}")
            #    return 'FIXED', notes
            if fix_status == 'OK':
                default_name, _ = self._parse_emulators_cfg(system)

                if default_name and default_name != core_name:
                    log(
                        f"  Working core differs from default "
                        f"({default_name}) — writing per-game override"
                    )
                    self._write_game_override(system, romname, core_name)

                self.write_mame_cfg(system, romname, core_name)

                notes = f"Fixed: {core_name} — override written"
                log(f"  Fixed with: {core_name}")
                return 'FIXED', notes

        return 'GENUINE ERROR', "All core combinations failed"

    def log_autofix_availability(self) -> None:
        """
        Log which core combinations are available for autofix on
        this RetroPie installation.
        """
        log("=" * 60)
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
            else:
                log(f"  [{system}] No cores installed")
        log("=" * 60)

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
        Write RetroPie per-game override and MAME cfg where applicable.

        MAME cores look for a per-game cfg file in a subdirectory of
        the ROM folder. Non-MAME cores still need the RetroPie
        per-game emulator override written when autofix finds a
        working core that differs from the system default.

        Args:
            system: System folder name e.g. 'arcade'.
            romname: ROM filename e.g. '1945kiii.zip'.
            core_name: Core that works e.g. 'lr-mame2010'.

        Returns:
            True if override or cfg handling completed successfully.
        """
        # Always write per-game emulators.cfg override if the working
        # core differs from the system default.
        default_name, _ = self._parse_emulators_cfg(system)
        if default_name and default_name != core_name:
            log(
                f"  Working core differs from default "
                f"({default_name}) — writing per-game override"
            )
            self._write_game_override(system, romname, core_name)

        # Find core_dir from MAME_CORE_COMBINATIONS.
        core_dir = None
        for display_name, cd, core_so in self.MAME_CORE_COMBINATIONS:
            if display_name == core_name:
                core_dir = cd
                break

        if not core_dir:
            log(
                f"  No MAME cfg entry found for '{core_name}' — "
                f"override handled, skipping MAME cfg creation"
            )
            return True

        # MAME cfg subdirectory strips 'lr-' prefix.
        mame_dir = core_dir.replace('lr-', '', 1)
        rom_base = os.path.splitext(romname)[0]
        cfg_dir = os.path.join(
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

            self._remove_stale_mame_cfgs(system, romname, mame_dir)

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
          2. fbgrab — standalone binary, works for advmame and other
             emulators that render directly to the framebuffer. No
             Python dependencies required.
          3. PIL framebuffer read from /dev/fb0 — fallback if fbgrab
             is not installed.
          4. Returns False — screenshot not available for this game.

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
            else:
                # UDP command was sent but RetroArch produced no file.
                # This can happen when:
                #   1. video_driver is not set to "gl" — RetroArch 1.19.1+
                #      defaults to a different driver on fresh installs
                #      (video_driver commented out in retroarch.cfg) and
                #      the SCREENSHOT command silently fails for non-gl
                #      drivers. Confirmed fix: uncomment/set
                #      video_driver = "gl" in
                #      /opt/retropie/configs/all/retroarch.cfg
                #   2. The core option configuration doesn't match the
                #      ROM set — e.g. lr-atari800 showing a blank blue
                #      screen because atari800_system is set to
                #      "400/800 (OS B)" instead of "XL/XE (64K)". The
                #      emulator runs but the ROM never actually loads,
                #      so there is nothing meaningful to capture.
                #      Check /opt/retropie/configs/all/retroarch-core-options.cfg
                log("  INFO: RetroArch received SCREENSHOT command "
                    "(port 55355 active) but produced no file. "
                    "Possible causes: (1) video_driver not set to 'gl' "
                    "in /opt/retropie/configs/all/retroarch.cfg — "
                    "uncomment/set video_driver = \"gl\" to fix; "
                    "(2) core option mismatch preventing ROM from loading "
                    "(e.g. atari800_system wrong for your ROM set — "
                    "check retroarch-core-options.cfg).")
        except Exception:
            pass

        # ------------------------------------------------------------------
        # Method 2: fbgrab — standalone framebuffer capture binary.
        # Available on most Raspberry Pi systems without any Python
        # dependencies. Works for advmame and other standalone emulators
        # that render directly to the framebuffer rather than RetroArch.
        # ------------------------------------------------------------------
        try:
            result = subprocess.run(
                ['fbgrab', dest_path],
                capture_output=True, timeout=5
            )
            if result.returncode == 0 and os.path.exists(dest_path):
                log("  Screenshot: captured via fbgrab")
                return True
        except FileNotFoundError:
            pass   # fbgrab not installed — try next method
        except Exception as e:
            log(f"  Screenshot: fbgrab failed — {e}")

        # ------------------------------------------------------------------
        # Method 3: PIL framebuffer — reads /dev/fb0 directly
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
            'drastic',      # NDS standalone
            'oricutron',    # Oric standalone
            'vice',         # C64/C128/etc standalone
            'atari800',     # Atari 800 standalone
            'frotz',        # Z-machine text adventures
            'dosbox',       # DOS standalone
            'scummvm',      # ScummVM standalone
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

        Kills any running emulator first (in case a game is active when
        the audit starts), then stops EmulationStation to release the
        KMS/DRM display. Running a game when the audit starts causes
        permission errors: the active RetroArch process owns
        /dev/shm/retroarch.cfg, and ES's child processes can't be
        killed by the audit user.
        """
        # Kill any running emulator first — must happen before ES stop
        # so we have permission to write /dev/shm/retroarch.cfg and
        # can cleanly kill ES without child-process permission errors.
        log("Checking for running emulators...")
        killed_any = False
        for proc in self.emulator_processes:
            result = self._safe_run(
                ['pkill', '-9', proc], capture=True
            )
            if result and result.returncode == 0:
                log(f"  Killed running emulator: {proc}")
                killed_any = True
        if killed_any:
            time.sleep(2)  # Allow display/framebuffer to release

        self._enable_retroarch_network_cmd()
        self.stop_emulationstation()

    def _enable_retroarch_network_cmd(self) -> None:
        """
        Ensure network_cmd_enable = "true" is active for RetroArch launches.

        Two writes for belt-and-braces coverage:
        1. The global retroarch.cfg — persistent, survives reboots.
        2. /dev/shm/retroarch.cfg — used as --appendconfig on every
           launch, which OVERRIDES per-system configs. This is the
           critical one: RetroArch is launched with
           --config /opt/retropie/configs/{system}/retroarch.cfg
           (per-system), not the global. Per-system configs take
           precedence over the global, so writing only to the global
           has no effect when a per-system config exists and doesn't
           set network_cmd_enable. Writing to /dev/shm ensures the
           setting is active regardless of which config chain is loaded.
        """
        # 1. Write to global config for persistence
        cfg_path = '/opt/retropie/configs/all/retroarch.cfg'
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, 'r') as f:
                    content = f.read()

                changed = False
                changes = []

                if 'network_cmd_enable = "true"' not in content:
                    if '# network_cmd_enable' in content:
                        content = re.sub(
                            r'#\s*network_cmd_enable\s*=\s*\S+',
                            'network_cmd_enable = "true"',
                            content
                        )
                    elif 'network_cmd_enable = "false"' in content:
                        content = content.replace(
                            'network_cmd_enable = "false"',
                            'network_cmd_enable = "true"'
                        )
                    else:
                        content += '\nnetwork_cmd_enable = "true"\n'
                    changed = True
                    changes.append('network_cmd_enable = "true"')

                if 'network_cmd_port = "55355"' not in content:
                    if '# network_cmd_port' in content:
                        content = re.sub(
                            r'#\s*network_cmd_port\s*=\s*\S+',
                            'network_cmd_port = "55355"',
                            content
                        )
                    elif 'network_cmd_port' not in content:
                        content += '\nnetwork_cmd_port = "55355"\n'
                    changed = True
                    changes.append('network_cmd_port = "55355"')

                if changed:
                    with open(cfg_path, 'w') as f:
                        f.write(content)
                    log(f"  RetroArch global config updated: {cfg_path}")
                    for change in changes:
                        log(f"    Set: {change}")
                else:
                    log("  RetroArch global config: network commands already enabled")
            except Exception as e:
                log(f"  Warning: could not update {cfg_path}: {e}")

        # 2. Write to /dev/shm/retroarch.cfg (appendconfig) — this
        # overrides per-system configs and is the reliable path.
        try:
            shm_cfg = '/dev/shm/retroarch.cfg'
            existing = ''
            if os.path.exists(shm_cfg):
                with open(shm_cfg, 'r') as f:
                    existing = f.read()
            additions = []
            if 'network_cmd_enable' not in existing:
                additions.append('network_cmd_enable = "true"')
            if 'network_cmd_port' not in existing:
                additions.append('network_cmd_port = "55355"')
            if additions:
                with open(shm_cfg, 'a') as f:
                    f.write('\n' + '\n'.join(additions) + '\n')
                log(f"  RetroArch appendconfig updated: {shm_cfg}")
                for a in additions:
                    log(f"    Set: {a}")
                log("  Network commands will be active from the first "
                    "ROM launch — no reboot needed.")
        except Exception as e:
            log(f"  Warning: could not update /dev/shm/retroarch.cfg: {e}")

        # 3. Patch any per-system retroarch.cfg that explicitly sets
        # network_cmd_enable = false or has it commented out — per-system
        # configs override both the global and appendconfig for settings
        # they explicitly define. Only patch files that exist and actively
        # disable or comment out the setting; leave others untouched.
        configs_root = '/opt/retropie/configs'
        if os.path.isdir(configs_root):
            for system_dir in os.listdir(configs_root):
                sys_cfg = os.path.join(
                    configs_root, system_dir, 'retroarch.cfg'
                )
                if not os.path.exists(sys_cfg):
                    continue
                try:
                    with open(sys_cfg, 'r') as f:
                        content = f.read()
                    # Only touch files that actively block the setting
                    needs_fix = (
                        'network_cmd_enable = "false"' in content
                        or (
                            '# network_cmd_enable' in content
                            and 'network_cmd_enable = "true"' not in content
                        )
                    )
                    if not needs_fix:
                        continue
                    content = re.sub(
                        r'#\s*network_cmd_enable\s*=\s*\S+',
                        'network_cmd_enable = "true"',
                        content
                    )
                    content = content.replace(
                        'network_cmd_enable = "false"',
                        'network_cmd_enable = "true"'
                    )
                    with open(sys_cfg, 'w') as f:
                        f.write(content)
                    log(f"  Fixed network_cmd_enable in [{system_dir}] config")
                except Exception as e:
                    log(f"  Warning: could not patch [{system_dir}] config: {e}")

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

        Detects which ES binary is running (stable or dev branch) and
        stores it for start_emulationstation() to restart the same one.
        """
        # Match both stable and dev branch paths:
        # supplementary/emulationstation/emulationstation
        # supplementary/emulationstation-dev/emulationstation
        result = self._safe_run(
            ['pgrep', '-f', '-a', 'supplementary/emulationstation'],
            capture=True
        )
        if result is None or result.returncode != 0:
            log("EmulationStation is not running.")
            self._es_binary = None
            return

        # Detect the actual binary path from the running process
        self._es_binary = None
        for line in result.stdout.strip().split('\n'):
            for candidate in [
                '/opt/retropie/supplementary/emulationstation-dev/emulationstation',
                '/opt/retropie/supplementary/emulationstation/emulationstation',
            ]:
                if candidate in line:
                    self._es_binary = candidate
                    break
            if self._es_binary:
                break

        pids = self._safe_run(
            ['pgrep', '-f', 'supplementary/emulationstation'],
            capture=True
        )
        pid_list = [
            p.strip()
            for p in (pids.stdout.strip().split('\n') if pids else [])
            if p.strip()
        ]
        log(f"Stopping EmulationStation "
            f"({'dev' if self._es_binary and 'dev' in self._es_binary else 'stable'}) "
            f"(PIDs: {', '.join(pid_list)})...")

        for pid in pid_list:
            self._safe_run(['kill', '-TERM', pid])

        time.sleep(5)

        check = self._safe_run(
            ['pgrep', '-f', 'supplementary/emulationstation'],
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
        #self._safe_run(['tput', 'reset'])
        self._safe_run(['stty', 'sane'])
        self._safe_run(['chvt', '1'], timeout=5)

        log("Restarting EmulationStation on tty1...")

        # Always use /usr/bin/emulationstation — this is the bash wrapper
        # that correctly sets up the environment and runs as the pi user.
        # Launching the binary directly (emulationstation-dev/emulationstation)
        # via sudo openvt runs it as root, causing joystick/config issues.
        # The wrapper resolves to the correct dev or stable binary itself.
        result = self._safe_run(
            ['sudo', 'openvt', '-c', '1', '-s', '-f',
             '/usr/bin/emulationstation'],
            timeout=10,
            capture=True
        )

        if result is not None and result.returncode == 0:
            log("EmulationStation restart command issued.")
            return

        log("=" * 60)
        log("AUDIT COMPLETE — ACTION REQUIRED")
        log("=" * 60)
        log("To restart EmulationStation from this SSH session:")
        log("  sudo openvt -c 1 -s -f emulationstation 2>&1")
        log("Or to cleanly stop and let it restart automatically:")
        log("  echo '' > /tmp/es-restart && killall emulationstation")
        log("Or reboot the Pi:")
        log("  sudo reboot")
        log("=" * 60)
