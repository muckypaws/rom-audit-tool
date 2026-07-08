"""
Recalbox platform implementation for the ROM Audit Tool.

Recalbox shares configgen ancestry with Batocera — the emulatorlauncher
and recalbox.conf per-game override format are essentially identical.

Key differences from Batocera:
  - emulatorlauncher requires explicit -emulator and -core arguments.
    ES resolves these from gamelist.xml, sidecar files or recalbox.conf
    before calling the launcher. We must do the same resolution.
  - Per-ROM config priority: gamelist.xml <emulator>/<core> tags (ES
    primary), sidecar files (romname.ext.recalbox.conf), recalbox.conf.
  - All output goes to stdout only — stderr is always empty.
  - EmulationStation must be stopped before testing (holds the
    framebuffer on GPi Case and similar hardware).
  - Screenshot via RetroArch UDP (primary) + fbgrab fallback. On Pi4
    with KMS/DRM, RetroArch renders to an overlay plane above /dev/fb0;
    fbgrab captures the background, not the game. UDP uses RetroArch's
    GPU screenshot (video_gpu_screenshot = true). fbgrab fallback used
    for standalone emulators that render directly to the framebuffer.
  - Annotation via ffmpeg (available on Recalbox).
  - ROMs exist in two locations: share/roms (user) and share_init/roms
    (built-in). Both are scanned; user path takes priority on duplicates.
  - Factory defaults in share_init/system/recalbox.conf vary by version
    — 9.2.3 has many system defaults, 10.0.5 has almost none (relies
    on gamelist.xml and readme-based resolution instead).

Tested on:
  - Recalbox 9.2.3 Pulstar (Buildroot 2023.02.2) — RPi Zero W, GPi Case v1
  - Recalbox 10.0.5 — RPi 4
"""

from __future__ import annotations   # Python 3.9 compatibility

import os
import re
import sys
import subprocess

from modules.platforms.base import Platform
from modules.common.logging import log
from modules.common import detection
from modules.common import autofix as autofixer
from modules.common import configeditor


class RecalboxPlatform(Platform):
    """
    Platform implementation for Recalbox 9.x.

    Recalbox uses the same configgen as Batocera so autofix logic is
    reused. The primary differences are explicit emulator/core arguments,
    sidecar-based per-game config, and ES lifecycle management.
    """

    def __init__(self) -> None:
        # Maps actual ROM path → friendly display name for ports.
        # Populated by discover_ports_roms() and consulted by
        # get_rom_display_name() so the CSV shows 'Quake' not 'pak0.pak'.
        self._port_display_names: dict[str, str] = {}
        # Maps actual ROM path → (emulator, core) for ports.
        # Populated alongside _port_display_names during discovery.
        self._port_core_info: dict[str, tuple[str, str]] = {}

    # Extension → (emulator, core) for standard port file types.
    # Only add an extension here when that extension unambiguously
    # identifies a single core across ALL ports — .pak always means
    # Quake, .wad always means Doom. DO NOT add .exe here; whether
    # an .exe is a valid Wolfenstein data file or a spurious Windows
    # binary depends on the port name, not the extension alone.
    _PORT_EXT_CORES: dict[str, tuple[str, str]] = {
        '.pak':  ('libretro', 'tyrquake'),   # Quake engine PAK archives
        '.wad':  ('libretro', 'prboom'),     # Doom WAD files
        '.pk3':  ('libretro', 'ecwolf'),     # ECWolf engine package
        '.lua':  ('corsixth', 'corsixth'),   # CorsixTH (Theme Hospital) Lua config
    }

    # Display name substring → (emulator, core) for .zip and directory ports
    _PORT_NAME_CORES: dict[str, tuple[str, str]] = {
        'cave story':       ('libretro', 'nxengine'),
        'cavestory':        ('libretro', 'nxengine'),
        'dinothawr':        ('libretro', 'dinothawr'),
        'rick dangerous':   ('libretro', 'xrick'),
        # Prince of Persia uses SDL Port of Prince of Persia (sdlpop).
        # The binary is /usr/bin/prince but configgen registers it as
        # emulator 'sdlpop' — using 'prince' produces "not a known emulator".
        'prince of persia': ('sdlpop',   'sdlpop'),
        'quake 2':          ('libretro', 'vitaquake2'),
        'quake':            ('libretro', 'tyrquake'),
        'doom':             ('libretro', 'prboom'),
        'sigil':            ('libretro', 'prboom'),
        'wolfenstein':      ('libretro', 'ecwolf'),
        'wolf3d':           ('libretro', 'ecwolf'),
        'bomberman':        ('libretro', 'mrboom'),
        # Standalone emulators confirmed via configgen generators
        'theme hospital':   ('corsixth', 'corsixth'),
        'corsix':           ('corsixth', 'corsixth'),
        'caesar':           ('julius',   'julius'),
        'openbor':          ('openbor',  'openbor'),
        'frotz':            ('frotz',    'frotz'),
        'simcoupe':         ('simcoupe', 'simcoupe'),
        'solarus':          ('solarus',  'solarus'),
    }

    def _resolve_port_core(
        self, rom_path: str, display_name: str
    ) -> tuple[str, str]:
        """
        Determine the libretro emulator/core for a port ROM.

        Resolution order:
          1. .game files — content IS the core name (normalised)
          2. File extension — .pak → tyrquake, .wad → prboom, .pk3 → ecwolf
          3. Display name — substring match against known port names

        Args:
            rom_path:     Full path to the port ROM file.
            display_name: Friendly name from gamelist.xml (e.g. 'Quake').

        Returns:
            (emulator, core) tuple.  emulator is always 'libretro'.
            core is '' if unknown — caller must handle this case.
        """
        import re as _re

        ext = os.path.splitext(rom_path)[1].lower()

        # .game files contain the libretro core name directly.
        # Validate: must be short and contain no XML markers.
        # Some .game files are actual game data (XML config) not core names.
        # e.g. 2048.game → "2048", gong.game → "Gong"  (core names)
        # vs   dinothawr.game → full XML config          (game data, skip)
        if ext == '.game':
            try:
                content = open(rom_path, encoding='utf-8',
                               errors='replace').read().strip()
                if content and len(content) <= 50 and '<' not in content:
                    core = _re.sub(r'[\s.]+', '', content).lower()
                    if core:
                        return 'libretro', core
            except Exception:
                pass

        # Known extension→(emulator, core) mappings
        if ext in self._PORT_EXT_CORES:
            return self._PORT_EXT_CORES[ext]

        # Name-based fallback (case-insensitive substring match)
        name_lower = display_name.lower()
        for key, (emulator, core) in self._PORT_NAME_CORES.items():
            if key in name_lower:
                return emulator, core

        return 'libretro', ''

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Recalbox"

    @property
    def roms_path(self) -> str:
        return "/recalbox/share/roms"

    def get_rom_display_name(self, system: str, rom_path: str) -> str:
        """Return friendly name for ports, basename for everything else."""
        if system == 'ports' and rom_path in self._port_display_names:
            return self._port_display_names[rom_path]
        return os.path.basename(rom_path)

    def discover_ports_roms(self) -> list[tuple[str, str]]:
        """
        Discover launchable ports via gamelist.xml and populate the
        display name mapping used by get_rom_display_name().

        Scans both user (share/roms/ports) and factory (share_init/roms/ports)
        directories. User path wins when the same display name appears in both.

        Returns:
            List of (system, actual_path) tuples ready for the audit loop.
        """
        from modules.common import filehandling
        # seen_names: display_name → path already recorded (user wins)
        seen_names: dict[str, str] = {}
        results: list[tuple[str, str]] = []

        native_exts = {'.exe', '.bat', '.com', '.cmd'}
        # Extensions that are an engine's own data/asset package rather
        # than an independently launchable game — skip these specifically
        # when a proper data-file sibling for the SAME core already
        # exists in the SAME folder. Scoped to .pk3 for now since that's
        # the confirmed, demonstrated case (ecwolf); extend this set only
        # if another core shows the identical pattern.
        engine_package_exts = {'.pk3'}

        for base in [self.roms_path] + self.additional_roms_paths:
            ports_path = os.path.join(base, 'ports')
            entries = list(filehandling.discover_ports(ports_path))

            # First pass: resolve every entry's core up front and group
            # by folder, so a .pk3's fate can depend on whether a real
            # .exe-family sibling for the same core exists alongside it
            # — something that can't be known by looking at one entry
            # in isolation. Confirmed case: a Wolfenstein 3D gamelist.xml
            # lists CATALOG.EXE, WOLF3D.EXE, and ecwolf.pk3 as three
            # separate <game> entries with no <hidden> markers at all.
            # ecwolf_libretro expects WOLF3D.EXE as its ROM argument —
            # ecwolf.pk3 is the engine's own data package, never meant
            # to be launched directly as the ROM itself. Without this
            # check, ecwolf.pk3 was being discovered and tested as its
            # own independent entry, launching the wrong file as the
            # core's ROM argument and failing every time.
            resolved: dict[str, tuple[str, str, str]] = {}  # path -> (system, disp, core)
            by_folder: dict[str, list[str]] = {}
            for system, path, disp in entries:
                _, core = self._resolve_port_core(path, disp)
                resolved[path] = (system, disp, core)
                folder = os.path.dirname(path)
                by_folder.setdefault(folder, []).append(path)

            skip_paths: set[str] = set()
            for folder, paths in by_folder.items():
                package_paths = [
                    p for p in paths
                    if os.path.splitext(p)[1].lower() in engine_package_exts
                ]
                if not package_paths:
                    continue
                # Check both gamelist entries AND filesystem for native
                # executable siblings — older Recalbox gamelists omit
                # WOLF3D.EXE entirely, so checking only resolved entries
                # misses the sibling. The .pk3 should be skipped whenever
                # a .exe/.bat/.com exists in the same folder on disk.
                native_exts_on_disk = {
                    os.path.splitext(f)[1].lower()
                    for f in os.listdir(folder)
                    if os.path.isfile(os.path.join(folder, f))
                }
                has_native_sibling = bool(
                    native_exts_on_disk & native_exts
                )
                native_cores_here = {
                    resolved[p][2] for p in paths
                    if os.path.splitext(p)[1].lower() in native_exts
                    and resolved[p][2]
                }
                for p in package_paths:
                    pkg_core = resolved[p][2]
                    if pkg_core in native_cores_here or (
                        has_native_sibling and pkg_core
                    ):
                        skip_paths.add(p)

            for system, path, disp in entries:
                if path in skip_paths:
                    log(f"  Ports: '{disp}' skipping engine package "
                        f"{os.path.basename(path)} — a proper data-file "
                        f"sibling for the same core already exists in "
                        f"this folder")
                    continue

                # Native DOS/Windows executables are skipped UNLESS a libretro
                # core is explicitly mapped to handle them.  ecwolf_libretro
                # expects WOLF3D.EXE (and similar) as its ROM path — the .exe
                # is used as a data container, not executed natively.  A blanket
                # skip in the generic discover_ports() would discard the correct
                # gamelist.xml path and fall through to ecwolf.pk3, which is
                # the engine package, not the game data the core expects.
                # Other executables with no registered core are still skipped.
                if os.path.splitext(path)[1].lower() in native_exts:
                    _, core = self._resolve_port_core(path, disp)
                    if not core:
                        log(f"  Ports: '{disp}' skipping native executable "
                            f"with no registered core: {os.path.basename(path)}")
                        continue
                if disp in seen_names:
                    continue   # User path already recorded; skip share_init copy
                seen_names[disp] = path
                self._port_display_names[path] = disp
                self._port_core_info[path] = self._resolve_port_core(path, disp)
                results.append((system, path))
        return results

    @property
    def additional_roms_paths(self) -> list[str]:
        """
        Recalbox ships built-in ROMs in share_init alongside the user's
        writable share/roms. EmulationStation scans both; so does the
        audit tool. User path takes priority on filename clashes.
        """
        return ["/recalbox/share_init/roms"]

    @property
    def system_subdir_markers(self) -> list[str]:
        """
        Recalbox uses subdirectories within system ROM folders to group
        ROMs that need a different core — e.g. 'Commodore Plus4' inside
        the c64 folder, with .core.cfg and .recalbox.conf overriding the
        core at launch time. These subdirs should be scanned for ROMs
        under the parent system.
        """
        return ['.core.cfg', '.recalbox.conf']

    @property
    def stdout_log(self) -> str:
        return "/recalbox/share/system/logs/es_launch_stdout.log"

    @property
    def stderr_log(self) -> str:
        # Always empty on Recalbox — all output goes to stdout_log.
        return "/recalbox/share/system/logs/es_launch_stderr.log"

    @property
    def results_csv(self) -> str:
        return "/recalbox/share/system/rom_audit/rom_audit.csv"

    @property
    def log_file(self) -> str:
        return "/recalbox/share/system/rom_audit/rom_audit.log"

    @property
    def pid_file(self) -> str:
        return "/recalbox/share/system/rom_audit/rom_audit.pid"

    @property
    def error_log_base(self) -> str:
        return "/recalbox/share/system/rom_audit/audit_logs"

    @property
    def conf_path(self) -> str:
        return "/recalbox/share/system/recalbox.conf"

    @property
    def libretro_core_path(self) -> str:
        return "/usr/lib/libretro"

    @property
    def faulty_roms_path(self) -> str:
        return "/recalbox/share/faultyroms"

    @property
    def screenshot_warmup(self) -> int:
        """
        Recalbox shows a loading screen (logo + Pac-Man ghosts) between
        the launch indicator and the game appearing. This 4-second warmup
        ensures screenshots are taken after the loading screen clears.
        """
        return 4

    @property
    def subprocess_capture(self) -> bool:
        # We run emulatorlauncher.py directly and capture stdout.
        # Use PYTHONUNBUFFERED=1 to ensure real-time output.
        return True

    # ------------------------------------------------------------------
    # Log analysis markers
    # ------------------------------------------------------------------

    @property
    def launch_indicators(self) -> list[str]:
        """
        Recalbox configgen logs "Running command:" immediately before
        invoking RetroArch. This is the reliable launch indicator.
        """
        return [
            "Running command:",
            "/usr/bin/retroarch",
        ]

    @property
    def error_markers(self) -> list[str]:
        return [
            "Error: Missing required argument",
            "No bios found",
            "Bios not found",
            "Exception",
        ]

    @property
    def bios_error_markers(self) -> list[str]:
        return [
            "No bios found",
            "Bios not found",
            "cannot load BIOS",
            "BIOS not found",
        ]

    @property
    def exit_marker(self) -> str:
        return ""   # Handled by parse_error() exit code check

    @property
    def post_launch_error_markers(self) -> list[str]:
        return [
            "NOT FOUND",
            "NO GOOD DUMP KNOWN",
        ]

    # ------------------------------------------------------------------
    # Launcher
    # ------------------------------------------------------------------

    def get_launcher_cmd(self) -> list[str]:
        return [
            "/usr/bin/python3", "-u",   # -u = unbuffered (real-time output)
            "/usr/lib/python3.11/site-packages/configgen/emulatorlauncher.py",
        ]

    def get_env(self) -> dict[str, str]:
        """
        Recalbox launcher environment.

        SDL_VIDEO_GL_DRIVER and SDL_VIDEO_EGL_DRIVER are critical —
        without them RetroArch cannot initialise the GLES display on
        the RPi and exits immediately with code 1.
        PYTHONUNBUFFERED ensures configgen output is captured in real
        time by our subprocess pipe.
        """
        env = os.environ.copy()
        env.update({
            "HOME":                  "/recalbox/share/system",
            "LANG":                  "en_US.UTF-8",
            "SDL_NOMOUSE":           "1",
            "SDL_VIDEO_GL_DRIVER":   "/usr/lib/libGLESv2.so",
            "SDL_VIDEO_EGL_DRIVER":  "/usr/lib/libGLESv2.so",
            "label":                 "RECALBOX",
            "PYTHONUNBUFFERED":      "1",
        })
        return env

    # Emulators that require an active display server (X11/Wayland) to
    # initialise SDL2 video. These cannot be tested when ES is stopped
    # because stopping ES removes the display entirely on Recalbox.
    # Confirmed: solarus-run aborts with SIGABRT (-6) when DISPLAY and
    # WAYLAND_DISPLAY are both unset, even with an otherwise-identical
    # command to the working UI launch.
    SDL2_DISPLAY_REQUIRED = {'solarus', 'solarus-run'}

    # For standalone emulators the key is the emulator name.
    # For libretro cores the key is the core name.
    # Any one listed file present in /recalbox/share/bios/ is sufficient.
    STANDALONE_BIOS_REQUIREMENTS: dict[str, list[str]] = {
        'gsplus':   ['apple2gs.rom', 'apple2gs2.rom', 'apple2gs3.rom'],
        'linapple': ['apple2e.rom', 'APPLE2E.ROM'],
    }

    def validate_rom_launch(
        self,
        system: str,
        rom: str
    ) -> tuple[bool, str]:
        """
        Validate that an emulator core and required BIOS can be found.

        Returns (False, reason) when:
          - No emulator core can be resolved for the system.
          - An emulator or libretro core with known BIOS requirements
            has no BIOS files present. These silently launch with
            garbage screens rather than logging an error to stdout.

        Args:
            system: System folder name.
            rom:    Full ROM path.

        Returns:
            (can_launch, reason). False causes MISSING CORE or
            MISSING BIOS to be written immediately without launching.
        """
        # Ports use a gamelist.xml manifest — emulatorlauncher resolves
        # the emulator and core internally. No ports.emulator/ports.core
        # exists in recalbox.conf so skip validation entirely for ports.
        if system == 'ports':
            return True, ''

        emulator, core = self._resolve_emulator_core(system, rom)

        if not core:
            return False, (
                f"No emulator core found for [{system}] — "
                f"system may require a standalone emulator not "
                f"installed on this device"
            )

        # SDL2 standalones require an active display server. Stopping ES
        # removes the display entirely on Recalbox — confirmed: DISPLAY
        # and WAYLAND_DISPLAY both empty, no X/Wayland sockets present.
        # These cannot be tested in headless mode.
        if emulator and emulator.lower() in self.SDL2_DISPLAY_REQUIRED:
            return False, (
                f'{emulator} requires an active display (SDL2) — '
                f'cannot be tested after EmulationStation is stopped '
                f'(no display server available). '
                f'Verify manually via EmulationStation.'
            )

        bios_dir = '/recalbox/share/bios'
        # Check by core name first (covers libretro-gsplus etc.),
        # then by emulator name (covers standalone gsplus etc.)
        required = (
            self.STANDALONE_BIOS_REQUIREMENTS.get(core, []) or
            self.STANDALONE_BIOS_REQUIREMENTS.get(emulator, [])
        )
        if required:
            if not any(
                os.path.exists(os.path.join(bios_dir, f))
                for f in required
            ):
                return False, (
                    f"MISSING BIOS: [{system}] requires one of "
                    f"{required} in {bios_dir}"
                )

        # Check the resolved core actually supports this file's extension.
        # Older libretro MAME cores (mame2003_plus, mame2003) only accept
        # .zip, while newer ones (mame2010, mame0258, mame2015) also accept
        # .7z. A .7z ROM can be launched directly against a .zip-only core
        # and the core's underlying library may open it without logging an
        # error — but EmulationStation's own pre-launch check would reject
        # it before ever reaching the emulator. Catching this here mirrors
        # that real-world rejection instead of reporting a false OK.
        extensions = self._get_extensions_for(system, emulator, core)
        if extensions:
            rom_ext = os.path.splitext(rom)[1].lower()
            if rom_ext not in extensions:
                supported = ', '.join(sorted(extensions))
                return False, (
                    f"{rom_ext} not supported by default core "
                    f"{emulator}-{core} for [{system}] "
                    f"(supports: {supported}). EmulationStation would "
                    f"reject this file before launch — see _readme.txt"
                )

        return True, ''

    def build_launch_cmd(self, system: str, rom: str) -> list[str]:
        """
        Build the emulatorlauncher command for a ROM.

        Recalbox's emulatorlauncher requires explicit -emulator and
        -core arguments — it does not read the sidecar or recalbox.conf
        itself. We resolve them in the same priority order ES uses:
          1. ROM sidecar file  (romname.zip.recalbox.conf)
          2. recalbox.conf     (system.emulator / system.core)
          3. Built-in fallback (libretro / first installed core)

        Args:
            system: System name e.g. 'mame', 'snes'
            rom:    Full path to the ROM file

        Returns:
            Complete command list for subprocess.Popen.
        """
        emulator, core = self._resolve_emulator_core(system, rom)

        # Ports: emulatorlauncher requires both -emulator and -core.
        # Core is resolved from the ROM file type (see _resolve_port_core).
        if system == 'ports':
            port_emulator, port_core = self._port_core_info.get(
                rom, ('libretro', '')
            )
            if not port_core:
                port_emulator, port_core = self._resolve_port_core(
                    rom, os.path.basename(rom)
                )
            if port_core:
                log(f"  Emulator: {port_emulator}  Core: {port_core}")
                return self.get_launcher_cmd() + [
                    "-system",   system,
                    "-rom",      rom,
                    "-emulator", port_emulator,
                    "-core",     port_core,
                ]
            # Unknown core — pass emulator only; will likely error but
            # at least produces a useful log line rather than crashing.
            log(f"  Emulator: {port_emulator}  Core: (unknown)")
            return self.get_launcher_cmd() + [
                "-system",   system,
                "-rom",      rom,
                "-emulator", port_emulator,
            ]

        if emulator is None or core is None:
            log(f"  Emulator: {emulator}  Core: {core} (incomplete — skipping)")
            return self.get_launcher_cmd() + [
                "-system", system,
                "-rom",    rom,
            ]
        log(f"  Emulator: {emulator}  Core: {core}")
        return self.get_launcher_cmd() + [
            "-system",   system,
            "-rom",      rom,
            "-emulator", emulator,
            "-core",     core,
        ] + self._get_controller_args()

    def _get_controller_args(self) -> list[str]:
        """
        Build controller arguments for emulatorlauncher.py matching
        exactly what EmulationStation passes.

        ES passes: -p1index 0 -p1guid <guid> -p1name <name>
                   -p1nbaxes N -p1nbhats N -p1nbbuttons N
                   -p1devicepath /dev/input/eventN

        configgen uses the GUID + name to look up button mappings.
        The quit-combo is derived from the hotkey + start button IDs
        in es_input.cfg (confirmed: hotkey id=7, start id=8 → 7+8).

        Key differences from a naive implementation:
        - devicepath is /dev/input/eventN not /dev/input/jsN
        - index is always 0 when ES runs (it holds the device open);
          our tool also gets 0 since ES is stopped before we run
        - nbaxes and nbhats are required fields
        """
        import xml.etree.ElementTree as ET

        input_cfg = '/recalbox/share/system/.emulationstation/es_input.cfg'
        if not os.path.exists(input_cfg):
            return []
        try:
            root = ET.parse(input_cfg).getroot()
            cfg = root.find('inputConfig')
            if cfg is None:
                return []

            guid        = cfg.get('deviceGUID', '')
            name        = cfg.get('deviceName', '')
            nb_buttons  = cfg.get('deviceNbButtons', '12')
            nb_axes     = cfg.get('deviceNbAxes', '4')
            nb_hats     = cfg.get('deviceNbHats', '1')

            if not (guid and name):
                return []

            # Find the event device path — ES uses /dev/input/eventN,
            # not /dev/input/jsN. Match by GUID via udev if possible,
            # otherwise fall back to event0.
            device_path = '/dev/input/event0'
            try:
                for entry in sorted(os.listdir('/dev/input')):
                    if entry.startswith('event'):
                        candidate = f'/dev/input/{entry}'
                        # Try to read the device name via ioctl-free method
                        proc_path = f'/proc/bus/input/devices'
                        with open(proc_path) as f:
                            content = f.read()
                        for block in content.split('\n\n'):
                            if entry in block and name[:10].lower() in block.lower():
                                device_path = candidate
                                break
            except Exception:
                pass

            log(f"  Controller: {name[:40]} guid={guid[:16]}... "
                f"path={device_path}")
            return [
                '-p1index',      '0',
                '-p1guid',       guid,
                '-p1name',       name,
                '-p1nbaxes',     nb_axes,
                '-p1nbhats',     nb_hats,
                '-p1nbbuttons',  nb_buttons,
                '-p1devicepath', device_path,
            ]
        except Exception as e:
            log(f"  Warning: could not read controller config: {e}")
            return []

    def _parse_system_readme(
        self,
        system: str
    ) -> list[tuple[str, str, frozenset]]:
        """
        Parse _readme.txt in the system ROM folder for supported emulators
        and the file extensions each one supports.

        Recalbox ships a _readme.txt in every system folder listing the
        supported emulators in order of compatibility/ease of use:

            libretro-mame2003_plus supports files of .zip and is ...
            libretro-mame2010 supports files of .zip .7z and is ...
            advancemame-advancemame supports files of .zip and is ...

        Critically, not every core supports every extension — older MAME
        libretro cores (mame2003_plus, mame2003) only accept .zip, while
        newer ones (mame2010, mame0258, mame2015) also accept .7z. The
        default core for a system is often the oldest/most compatible one
        and may NOT support .7z even though .7z files pass when launched
        directly (the core's underlying decompression can open the file,
        but EmulationStation's own pre-launch check rejects it). Capturing
        the extension list lets validate_rom_launch() catch this mismatch
        before launching, matching what ES would actually do.

        Format: {emulator_type}-{core_name} supports files of .ext .ext...

        Args:
            system: System folder name e.g. 'mame'.

        Returns:
            Ordered list of (emulator, core, extensions) tuples, highest
            priority first. extensions is a frozenset of lowercase
            extensions including the leading dot, e.g. {'.zip', '.7z'}.
            Empty list if readme not found or unparseable.
        """
        results = []
        for base in [self.roms_path] + self.additional_roms_paths:
            readme_path = os.path.join(base, system, '_readme.txt')
            if not os.path.exists(readme_path):
                continue
            try:
                with open(readme_path, encoding='utf-8', errors='replace') as f:
                    content = f.read()
                for line in content.splitlines():
                    line = line.strip()
                    if 'supports files of' not in line:
                        continue
                    token = line.split('supports files of')[0].strip()
                    if '-' not in token:
                        continue
                    emulator_type, core_name = token.split('-', 1)
                    emulator_type = emulator_type.strip()
                    core_name     = core_name.strip()
                    if not (emulator_type and core_name):
                        continue
                    # Extensions sit between "supports files of" and
                    # "and is" — each token starting with a dot.
                    ext_part = line.split('supports files of', 1)[1]
                    ext_part = ext_part.split(' and is')[0]
                    extensions = frozenset(
                        tok.lower() for tok in ext_part.split()
                        if tok.startswith('.')
                    )
                    results.append((emulator_type, core_name, extensions))
                if results:
                    break
            except Exception:
                pass
        return results

    def _get_extensions_for(
        self,
        system: str,
        emulator: str,
        core: str
    ) -> frozenset:
        """
        Look up the supported file extensions for a specific
        emulator/core combination from the system's _readme.txt.

        Args:
            system:   System folder name e.g. 'mame'.
            emulator: Emulator type e.g. 'libretro'.
            core:     Core name e.g. 'mame2003_plus'.

        Returns:
            Frozenset of supported extensions, or empty frozenset if
            the combination isn't listed in the readme (unknown —
            callers should treat this as "no data, don't block").
        """
        for e, c, exts in self._parse_system_readme(system):
            if e == emulator and c == core:
                return exts
        return frozenset()

    def _find_standalone_binary(self, name: str) -> str | None:

        """
        Find a standalone emulator binary, case-insensitively.

        Handles two patterns seen in Recalbox:
          - /usr/bin/{name}        — simple binary (e.g. /usr/bin/daphne)
          - /usr/bin/{name}/{name} — binary in same-named subdir
                                     (e.g. /usr/bin/oricutron/oricutron)

        Case-insensitive so /usr/bin/GSplus is found when looking for
        'gsplus' (the readme uses lowercase, binary uses mixed case).

        Args:
            name: Emulator name to search for e.g. 'gsplus'.

        Returns:
            Full path to binary if found, None otherwise.
        """
        name_lower = name.lower()
        # Some standalone binaries use a different name from their system/emulator
        # e.g. the 'solarus' emulator is actually 'solarus-run' in /usr/bin
        name_aliases = {
            'solarus': ['solarus-run', 'solarus'],
            'wasm4':   ['wasm4', 'w4'],
        }
        candidates = name_aliases.get(name_lower, [name_lower])
        try:
            for entry in os.listdir('/usr/bin'):
                if entry.lower() in candidates:
                    full = f'/usr/bin/{entry}'
                    if os.path.isfile(full):
                        return full
                    # Binary inside same-named subdirectory
                    if os.path.isdir(full):
                        for sub in os.listdir(full):
                            if sub.lower() in candidates:
                                candidate = f'{full}/{sub}'
                                if os.path.isfile(candidate):
                                    return candidate
        except OSError:
            pass
        return None

    # Emulators that return non-zero exit codes on normal exit.
    # Maps lowercase binary basename → set of exit codes that are OK.
    # gsplus returns 1 when the user quits normally.
    TOLERATED_EXIT_CODES: dict[str, set[int]] = {
        'gsplus': {0, 1},
    }

    def _get_system_priority_list(
        self,
        system: str
    ) -> list[tuple[str, str]]:
        """
        Return ordered (emulator, core) list for a system, filtered to
        what is actually installed on this device.

        Uses the system _readme.txt as the source of truth, verified
        against installed libretro cores or standalone binaries.
        Falls back to SYSTEM_CORE_PRIORITY if no readme is found.

        Args:
            system: System folder name.

        Returns:
            Ordered list of available (emulator, core) tuples.
        """
        from_readme = self._parse_system_readme(system)
        available   = []
        for emulator_type, core_name, _extensions in from_readme:
            if emulator_type == 'libretro':
                so = os.path.join(
                    self.libretro_core_path, f'{core_name}_libretro.so'
                )
                if os.path.exists(so):
                    available.append((emulator_type, core_name))
            else:
                binary = (
                    self._find_standalone_binary(emulator_type) or
                    self._find_standalone_binary(core_name)
                )
                if binary:
                    available.append((emulator_type, core_name))
        if available:
            return available

        # Nothing from readme is installed. Log what was found in readme
        # so the user knows why the system can't be tested.
        if from_readme:
            types = ', '.join(
                f"{e}-{c}" for e, c, *_ in from_readme
            )
            log(f"  [{system}] No installed emulators found. "
                f"Readme lists: {types}")

        # Hardcoded fallback for systems not in readme
        fallback = self._get_fallback_core(system)
        return [('libretro', fallback)] if fallback else []

    def _get_autofix_combinations(
        self,
        system: str,
        rom: str
    ) -> list[tuple[str, str]]:
        """
        Return (emulator, core) combinations to try during autofix,
        filtered to ones that both:
          1. Are actually installed on this device, and
          2. Support the ROM's file extension per _readme.txt.

        Without the extension filter, autofix could "fix" a ROM with a
        core that EmulationStation would reject before ever launching —
        e.g. a .7z ROM defaulting to mame2003_plus (zip-only), where
        autofix tries other zip-only cores and never reaches one of the
        .7z-capable cores (mame2010, mame0258, mame2015) even though
        they're installed, because the extension was never checked.

        Args:
            system: System folder name e.g. 'mame'.
            rom:    Full path to the ROM file (used to get the extension).

        Returns:
            Ordered list of (emulator, core) tuples — installed AND
            extension-compatible, highest priority first.
        """
        rom_ext = os.path.splitext(rom)[1].lower()
        from_readme = self._parse_system_readme(system)
        available: list[tuple[str, str]] = []

        for emulator_type, core_name, extensions in from_readme:
            # Skip combos that don't support this file's extension —
            # but only when we have authoritative extension data. An
            # empty extensions set means the readme line had no parsed
            # extensions; don't block on data we don't trust.
            if extensions and rom_ext not in extensions:
                continue
            if emulator_type == 'libretro':
                so = os.path.join(
                    self.libretro_core_path, f'{core_name}_libretro.so'
                )
                if os.path.exists(so):
                    available.append((emulator_type, core_name))
            else:
                binary = (
                    self._find_standalone_binary(emulator_type) or
                    self._find_standalone_binary(core_name)
                )
                if binary:
                    available.append((emulator_type, core_name))

        if available:
            return available

        # Nothing installed supports this extension. Fall back to the
        # unfiltered priority list so autofix still has something to try
        # rather than reporting NO COMBINATIONS when partial data exists.
        return self._get_system_priority_list(system)

    def _read_gamelist(
        self,
        system: str,
        rom_path: str
    ) -> dict[str, str]:
        """
        Read emulator and core from gamelist.xml for a specific ROM.

        ES uses gamelist.xml <core> and <emulator> tags as per-ROM
        configuration. This is the primary resolution mechanism for
        systems like 240ptestsuite where each ROM is a different
        system's calibration tool with its own required emulator.

        Searches share/roms first (user content), then share_init/roms
        (built-in Recalbox content).

        Args:
            system:   System folder name e.g. '240ptestsuite'.
            rom_path: Full path to the ROM file.

        Returns:
            Dict with 'emulator' and/or 'core' keys, or empty dict.
        """
        import xml.etree.ElementTree as ET
        rom_filename = os.path.basename(rom_path)

        for base in [self.roms_path] + self.additional_roms_paths:
            gamelist_path = os.path.join(base, system, 'gamelist.xml')
            if not os.path.exists(gamelist_path):
                continue
            try:
                tree = ET.parse(gamelist_path)
                for game in tree.findall('game'):
                    path_elem = game.find('path')
                    if path_elem is None or not path_elem.text:
                        continue
                    if os.path.basename(
                        path_elem.text.strip()
                    ) != rom_filename:
                        continue
                    result: dict[str, str] = {}
                    for tag in ('emulator', 'core'):
                        elem = game.find(tag)
                        if elem is not None and elem.text:
                            result[tag] = elem.text.strip()
                    if result:
                        return result
            except Exception:
                pass
        return {}

    def _resolve_emulator_core(
        self,
        system: str,
        rom: str
    ) -> tuple[str, str]:
        """
        Resolve the emulator and core for a ROM launch.

        Priority (mirrors Recalbox ES resolution):
          1. gamelist.xml <emulator>/<core> tags — primary per-ROM config
             used by ES. Critical for multi-system collections like
             240ptestsuite where each ROM has its own emulator.
          2. Sidecar file (.recalbox.conf) — per-game user/autofix override.
          3. recalbox.conf system/global settings.
          4. _readme.txt priority list filtered to installed cores.
          5. Hardcoded fallback.

        Args:
            system: System folder name e.g. 'mame', '240ptestsuite'.
            rom:    Full ROM path.

        Returns:
            Tuple of (emulator, core) strings.
        """
        # 1. gamelist.xml — ES primary resolution
        gamelist = self._read_gamelist(system, rom)
        if gamelist.get('emulator') and gamelist.get('core'):
            emulator = gamelist['emulator']
            core     = gamelist['core']
            if emulator == 'libretro':
                so = os.path.join(
                    self.libretro_core_path, f'{core}_libretro.so'
                )
                if os.path.exists(so):
                    return emulator, core
                log(f"  Note: gamelist core '{core}' not installed"
                    f" — falling back")
            else:
                return emulator, core   # Standalone — trust gamelist

        # 2. Sidecar — per-game user or autofix override
        sidecar = self._read_sidecar(rom)
        if not sidecar.get('core') and '/share_init/roms/' in rom:
            sidecar = self._read_sidecar(
                rom.replace('/share_init/roms/', '/share/roms/')
            )
        if sidecar.get('emulator') and sidecar.get('core'):
            emulator = sidecar['emulator']
            core     = sidecar['core']
            if emulator == 'libretro':
                so = os.path.join(
                    self.libretro_core_path, f'{core}_libretro.so'
                )
                if os.path.exists(so):
                    return emulator, core
                log(f"  Note: sidecar core '{core}' not installed"
                    f" — falling back")
            else:
                return emulator, core

        # 3. recalbox.conf system/global override
        emulator, core = self._get_conf_defaults(system)
        if emulator and core:
            return emulator, core

        # 4. First available from readme priority list
        priority_list = self._get_system_priority_list(system)
        if priority_list:
            return priority_list[0]

        # 5. Last resort
        return 'libretro', self._get_fallback_core(system)

    def _read_sidecar(self, rom_path: str) -> dict[str, str]:
        """
        Read a Recalbox per-ROM sidecar config file.

        Sidecar files live alongside the ROM:
            /path/to/romname.zip.recalbox.conf

        Format:
            global.emulator=libretro
            global.core=mame2010

        Args:
            rom_path: Full path to the ROM file.

        Returns:
            Dict with keys 'emulator' and/or 'core', or empty dict.
        """
        sidecar_path = rom_path + '.recalbox.conf'
        result = {}
        if not os.path.exists(sidecar_path):
            return result
        try:
            with open(sidecar_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or '=' not in line:
                        continue
                    key, val = line.split('=', 1)
                    key = key.strip().lower()
                    val = val.strip()
                    if key.endswith('.emulator'):
                        result['emulator'] = val
                    elif key.endswith('.core'):
                        result['core'] = val
        except Exception:
            pass
        return result

    def _get_conf_defaults(
        self,
        system: str
    ) -> tuple[str, str]:
        """
        Read system emulator/core defaults from recalbox.conf files.

        Checks three sources in priority order:
          1. /recalbox/share/system/recalbox.conf  — user overrides
          2. /recalbox/share_init/system/recalbox.conf — factory defaults

        The factory defaults contain the system→emulator/core mappings
        that ES uses when no sidecar or user override is present (e.g.
        mame.core=mame2000, snes.emulator=pisnes, apple2.core=gsplus).

        Args:
            system: System folder name e.g. 'mame'.

        Returns:
            Tuple of (emulator, core) or ('', '') if not configured.
        """
        conf_paths = [
            self.conf_path,                                    # User config
            '/recalbox/share_init/system/recalbox.conf',       # Factory defaults
        ]

        emulator = core = ''
        global_emulator = global_core = ''

        for conf_path in conf_paths:
            if not os.path.exists(conf_path):
                continue
            try:
                with open(conf_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith(';') \
                                or line.startswith('#'):
                            continue
                        if '=' not in line:
                            continue
                        key, val = line.split('=', 1)
                        key = key.strip()
                        val = val.strip()
                        if key == f'{system}.emulator' and not emulator:
                            emulator = val
                        elif key == f'{system}.core' and not core:
                            core = val
                        elif key == 'global.emulator' \
                                and not global_emulator:
                            global_emulator = val
                        elif key == 'global.core' \
                                and not global_core:
                            global_core = val
            except Exception:
                pass

            # If we found system-specific settings in this file, stop
            if emulator or core:
                break

        return (
            emulator or global_emulator,
            core or global_core
        )

    def _get_fallback_core(self, system: str) -> str:
        """
        Best-effort fallback: return the first plausible libretro core
        for a system based on installed cores.

        Uses a priority list per system — on RPi Zero the lightest
        compatible core is preferred.

        Args:
            system: System name e.g. 'mame', 'snes'.

        Returns:
            Core name string e.g. 'mame2000', or '' if unknown.
        """
        # Priority-ordered core preferences per system on low-end hardware
        SYSTEM_CORE_PRIORITY: dict[str, list[str]] = {
            'mame':         ['mame2000', 'mame2003', 'mame2003_plus'],
            'fba':          ['fba_libretro', 'fbneo'],
            'fbneo':        ['fbneo', 'fba_libretro'],
            'snes':         ['snes9x2005', 'snes9x2002', 'snes9x'],
            'nes':          ['fceumm', 'nestopia'],
            'gb':           ['gambatte', 'mgba'],
            'gbc':          ['gambatte', 'mgba'],
            'gba':          ['mgba', 'vba_next'],
            'megadrive':    ['genesis_plus_gx', 'picodrive'],
            'mastersystem': ['genesis_plus_gx', 'picodrive'],
            'gamegear':     ['genesis_plus_gx'],
            'atari2600':    ['stella2014', 'stella'],
            'atari7800':    ['prosystem'],
            'atari800':     ['atari800'],
            'pce':          ['mednafen_pce_fast', 'mednafen_pce'],
            'neogeo':       ['fbneo', 'fba_libretro'],
            'zxspectrum':   ['fuse'],
            'amstradcpc':   ['cap32'],
            'c64':          ['vice_x64'],
        }
        candidates = SYSTEM_CORE_PRIORITY.get(system, [])
        core_path = self.libretro_core_path
        for core in candidates:
            so = os.path.join(core_path, f'{core}_libretro.so')
            if os.path.exists(so):
                return core
        return ''

    @property
    def version(self) -> str:
        """Read Recalbox version from /recalbox/recalbox.version."""
        try:
            with open('/recalbox/recalbox.version') as f:
                return f.read().strip()
        except Exception:
            pass
        return ''

    # ------------------------------------------------------------------
    # EmulationStation lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _proc_is_es(pid: str) -> bool:
        """
        Return True if /proc/<pid> is the emulationstation binary.

        Parses /proc/PID/cmdline and checks the basename of the first
        argument. This correctly identifies the binary while ignoring
        the bash starter wrapper (/bin/bash emulationstation-starter).

        Note: /proc/PID/comm is NOT used — Linux truncates comm to 15
        characters, so 'emulationstation' (16 chars) becomes
        'emulationstatio', causing exact-match checks to always fail.
        """
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                # cmdline is null-delimited; first field is the binary path
                first_arg = f.read().split(b'\x00')[0].decode(
                    errors='replace'
                )
            return os.path.basename(first_arg) == 'emulationstation'
        except (OSError, IOError):
            return False

    def _es_running(self) -> bool:
        """Return True if the emulationstation binary is running."""
        try:
            return any(
                self._proc_is_es(pid)
                for pid in os.listdir('/proc')
                if pid.isdigit()
            )
        except Exception:
            return False

    def _es_instance_count(self) -> int:
        """Return count of running emulationstation binary processes."""
        try:
            return sum(
                1 for pid in os.listdir('/proc')
                if pid.isdigit() and self._proc_is_es(pid)
            )
        except Exception:
            return 0

    def pre_audit(self) -> None:
        """
        Stop EmulationStation before ROM testing begins.

        Two real fixes consolidated here, found while investigating a
        report of ES "still running whilst the audit starts and thus
        fails":

        1. This method previously existed as TWO separate definitions
           in this file — Python silently keeps only the second, so
           the first (which tried the init script for a clean stop
           before falling back to killall) was genuine dead code that
           never executed. The version that DID run skipped straight
           to killall, a blunter stop. Consolidated into one method
           here, using the init script first (clean shutdown of both
           the ES binary and its wrapper script) with killall only as
           a fallback if that doesn't work.
        2. The previous logic, when ES was still detected running
           after the poll deadline, logged a warning and then
           proceeded to start the audit anyway — the exact "still
           running whilst the audit starts" failure mode, except the
           tool already knew about it and didn't act. Now retries with
           an escalating kill (killall -9 on the second attempt) and a
           second poll window, only proceeding with a hard failure if
           ES is still detected after both attempts — never silently
           continuing into a test run against a UI that's still up.
        """
        import time
        count = self._es_instance_count()
        if count == 0:
            log("  EmulationStation not running — skipping stop.")
            return
        if count > 1:
            log(f"  WARNING: {count} EmulationStation instances running "
                f"— stopping all.")
        else:
            log("  Stopping EmulationStation...")

        ES_STOP_POLL_INTERVAL  = 0.5
        ES_STOP_POLL_DEADLINE  = 10    # seconds to wait per attempt
        ES_STOP_MAX_ATTEMPTS   = 3     # init script, then killall,
                                        # then killall -9 — each gets
                                        # its own full poll window
                                        # rather than one fixed total

        for attempt in range(1, ES_STOP_MAX_ATTEMPTS + 1):
            if attempt == 1:
                # Init script does a clean stop of both the ES binary
                # and its wrapper — the graceful path, tried first.
                try:
                    subprocess.run(
                        ['/etc/init.d/S31emulationstation', 'stop'],
                        capture_output=True, timeout=15
                    )
                except Exception:
                    pass
            elif attempt == 2:
                log("  EmulationStation still running after the clean "
                    "stop — trying killall...")
                try:
                    subprocess.run(
                        ['killall', 'emulationstation'],
                        capture_output=True, timeout=10
                    )
                except Exception as e:
                    log(f"  Warning: killall failed: {e}")
            else:
                log("  EmulationStation still running after killall — "
                    "escalating to killall -9...")
                try:
                    subprocess.run(
                        ['killall', '-9', 'emulationstation'],
                        capture_output=True, timeout=10
                    )
                except Exception as e:
                    log(f"  Warning: killall -9 failed: {e}")

            deadline = time.time() + ES_STOP_POLL_DEADLINE
            while time.time() < deadline:
                if not self._es_running():
                    break
                time.sleep(ES_STOP_POLL_INTERVAL)

            if not self._es_running():
                log("  EmulationStation stopped.")
                break
        else:
            # Every attempt exhausted and ES is still running — this is
            # exactly the failure mode that motivated this fix. Stop
            # here rather than silently starting the audit against a
            # UI that's still up; a test run launched into that state
            # is the actual root cause of the original report, not
            # something a longer fixed buffer alone reliably solves.
            log("  ERROR: EmulationStation could not be stopped after "
                f"{ES_STOP_MAX_ATTEMPTS} attempts. Aborting audit — "
                "starting against a still-running UI would produce "
                "unreliable results across every ROM, not just an "
                "occasional one.")
            log("  Try stopping it manually "
                "(/etc/init.d/S31emulationstation stop) and re-run, "
                "or investigate why it won't release "
                "(vcgencmd get_throttled — under-voltage is a "
                "confirmed cause of slow/unreliable shutdowns).")
            sys.exit(1)

        # Extra buffer for framebuffer/EGL release even after ES is
        # confirmed gone via /proc — the process disappearing doesn't
        # guarantee the graphics context it held has been released yet.
        # Same reasoning as get_post_kill_delay() elsewhere in this
        # project (Flycast, also Recalbox's own GPi Case precedent for
        # this exact symptom) — bumped from 2s to 3s here specifically
        # because this report showed ES itself needing longer than the
        # original assumption, not just emulators after a kill.
        time.sleep(3)

    def post_audit(self) -> None:
        """
        Restart EmulationStation after the audit completes.

        Guards against starting a second instance if ES is somehow
        already running (e.g. user manually restarted it mid-audit).
        """
        log("")
        log("=" * 55)
        log("  AUDIT COMPLETE")
        log("=" * 55)
        if self._es_running():
            log("  EmulationStation already running — not restarting.")
            return
        try:
            subprocess.Popen(
                ['/etc/init.d/S31emulationstation', 'start'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            log("  EmulationStation restarting...")
            log("  (Takes ~10s — or run: "
                "/etc/init.d/S31emulationstation start)")
        except Exception as e:
            log(f"  Could not restart EmulationStation: {e}")
            log("  Run manually: /etc/init.d/S31emulationstation start")

    # ------------------------------------------------------------------
    # Screenshot capture
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Screenshot capture — RetroArch UDP (primary) + fbgrab (fallback)
    # ------------------------------------------------------------------

    def capture_screenshot(self, dest_path: str) -> bool:
        """
        Capture via RetroArch UDP screenshot command, falling back to fbgrab.

        On Pi4 with KMS/DRM, RetroArch renders to an overlay plane that
        sits above /dev/fb0. fbgrab reads fb0 and captures the background,
        not the game. RetroArch's UDP SCREENSHOT command captures from the
        GPU render context (video_gpu_screenshot = true) giving a correct
        image of the running game.

        Recalbox has network_cmd_enable = true globally — no config
        changes needed. Falls back to fbgrab for standalone emulators
        (gsplus, oricutron) which render directly to the framebuffer.

        Args:
            dest_path: Full path for the output PNG file.

        Returns:
            True if a screenshot was captured and copied successfully.
        """
        import socket
        import time as _time

        # Recursive scan helper — returns dict of full_path → mtime
        def _scan_pngs(directory: str) -> dict[str, float]:
            """Return {full_path: mtime} for all PNGs under directory."""
            found: dict[str, float] = {}
            try:
                for entry in os.listdir(directory):
                    full = os.path.join(directory, entry)
                    if os.path.isfile(full) \
                            and entry.lower().endswith('.png'):
                        try:
                            found[full] = os.path.getmtime(full)
                        except OSError:
                            pass
                    elif os.path.isdir(full):
                        found.update(_scan_pngs(full))
            except OSError:
                pass
            return found

        screenshots_dir = '/recalbox/share/screenshots'
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        # Timestamp just before sending the command — detect any PNG
        # written or modified after this point. Using mtime rather than
        # set-difference handles the case where RetroArch overwrites an
        # existing file with the same name (same ROM tested twice).
        cmd_time = _time.time()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b'SCREENSHOT', ('127.0.0.1', 55355))
            sock.close()
        except Exception as e:
            log(f"  Screenshot: UDP send failed — {e}")
            return self._capture_screenshot_fbgrab(dest_path)

        # Poll for a PNG with mtime >= cmd_time and non-zero size
        new_file = None
        deadline = _time.time() + 4.0
        while _time.time() < deadline:
            for path, mtime in _scan_pngs(screenshots_dir).items():
                if mtime >= cmd_time - 0.5:   # small clock-skew buffer
                    try:
                        if os.path.getsize(path) > 0:
                            new_file = path
                            break
                    except OSError:
                        pass
            if new_file:
                break
            _time.sleep(0.25)

        if not new_file:
            log("  Screenshot: no updated PNG after UDP command — "
                "falling back to fbgrab")
            return self._capture_screenshot_fbgrab(dest_path)

        # Brief extra buffer then relocate the file into our own audit
        # log structure. Using shutil.move() rather than a copy+leave-
        # original means the file in /recalbox/share/screenshots is gone
        # the moment we take ownership of it — no leftover accumulating
        # in Recalbox's own screenshot gallery (surfaced via its web
        # interface) for every ROM tested. /recalbox/share/screenshots
        # and our audit_logs destination are both under /recalbox/share,
        # so this is a fast same-filesystem rename, not a slow copy.
        import shutil
        _time.sleep(0.25)
        try:
            src_size = os.path.getsize(new_file)
            if src_size == 0:
                log(f"  Screenshot: source is zero bytes — removing "
                    f"junk file {new_file}")
                try:
                    os.remove(new_file)
                except OSError:
                    pass
                return False
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.move(new_file, dest_path)
            final_size = os.path.getsize(dest_path)
            log(f"  Screenshot: moved {final_size:,} bytes → {dest_path}")
            return final_size > 0
        except Exception as e:
            log(f"  Screenshot: move failed — {e}")
            return False

    def _capture_screenshot_fbgrab(self, dest_path: str) -> bool:
        """
        Fallback screenshot via fbgrab for standalone emulators.

        Standalone emulators (gsplus, oricutron etc.) render directly to
        the framebuffer rather than through RetroArch's KMS planes, so
        fbgrab can capture their output correctly.

        Args:
            dest_path: Full path for the output PNG file.

        Returns:
            True if capture succeeded and file exists.
        """
        try:
            result = subprocess.run(
                ['fbgrab', dest_path],
                capture_output=True, timeout=5
            )
            if result.returncode == 0 and os.path.exists(dest_path):
                return True
            log(f"  Screenshot: fbgrab failed — "
                f"{result.stderr.decode(errors='replace').strip()}")
        except FileNotFoundError:
            log("  Screenshot: fbgrab not found")
        except Exception as e:
            log(f"  Screenshot: {e}")

        # Clean up any zero-byte file fbgrab may have left behind
        try:
            if os.path.exists(dest_path) and os.path.getsize(dest_path) == 0:
                os.remove(dest_path)
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Autofix — sidecar-based per-game config
    # ------------------------------------------------------------------

    @property
    def emulator_processes(self) -> list[str]:
        return ['retroarch', 'retroarch32']

    def kill_emulators(self) -> None:
        """
        Kill running emulators and wait for the framebuffer to settle.

        On RPi Zero with GPi Case, the EGL/GLES display needs time to
        be released cleanly before the next game can initialise it.
        Without this delay, consecutive games fail with exit code 1.
        """
        super().kill_emulators()
        import time
        time.sleep(2)   # Let EGL/framebuffer fully release

    # Recalbox uses fba for FinalBurn Alpha
    SYSTEM_ALIAS_MAP: dict[str, str] = {
        'fba': 'fbneo',
    }

    # ------------------------------------------------------------------
    # Log analysis override
    # ------------------------------------------------------------------

    def parse_error(
        self,
        stdout: str,
        stderr: str
    ) -> tuple[str, str]:
        """
        Detect errors from Recalbox configgen output.

        Checks (in order):
          1. Logged BIOS path that does not exist — hatari/AtariST and
             similar generators print "BIOS   : /path" before launching.
             If that path is absent we mark MISSING BIOS immediately.
          2. Non-zero "Process exitcode: N" in stdout.
          3. Configgen error strings (all output goes to stdout).

        Args:
            stdout: es_launch_stdout.log contents.
            stderr: es_launch_stderr.log (always empty on Recalbox).

        Returns:
            ('MISSING BIOS'|'ERROR', notes) or (None, '').
        """
        # 1. BIOS path logged but file absent
        bios_match = re.search(r'^BIOS\s*:\s*(.+)$', stdout, re.MULTILINE)
        if bios_match:
            bios_path = bios_match.group(1).strip()
            if not os.path.exists(bios_path):
                return 'MISSING BIOS', f'BIOS not found: {bios_path}'

        # 2. Non-zero exit code.
        #
        # The regex captures an optional leading minus sign — Recalbox
        # logs negative codes using the POSIX convention where a process
        # terminated by signal N is reported as exitcode -N. Without the
        # minus sign in the pattern, negative codes are silently invisible
        # to this check (confirmed: \d+ alone never matches "-11", so a
        # genuine SIGSEGV crash fell through to no error detected at all).
        #
        # Positive codes: checked against TOLERATED_EXIT_CODES, since some
        # standalone emulators (e.g. gsplus) return 1 on a normal quit.
        #
        # Negative codes: -15 (SIGTERM) and -9 (SIGKILL) are produced by
        # our OWN kill_emulators()/SIGKILL fallback when ending a healthy
        # running game and are expected, not failures. Any OTHER negative
        # code means the OS delivered a fault signal to the process —
        # SIGSEGV, SIGABRT, SIGFPE, SIGBUS, SIGILL and similar are never
        # something we send and never a "normal" emulator exit; these are
        # always genuine crashes regardless of which binary produced them.
        code_match = re.search(r'Process exitcode:\s*(-?\d+)', stdout)
        if code_match:
            code = int(code_match.group(1))
            if code > 0:
                # Determine which binary was used from the Running command line
                cmd_match = re.search(
                    r'Running command:.*?/usr/bin/(\S+)', stdout
                )
                binary_name = ''
                if cmd_match:
                    binary_name = os.path.basename(
                        cmd_match.group(1)
                    ).lower()
                tolerated = self.TOLERATED_EXIT_CODES.get(binary_name, {0})
                if code not in tolerated:
                    return 'ERROR', f'Process exitcode: {code}'
            elif code < 0 and code not in (-15, -9):
                crash_signals = {
                    -4:  'SIGILL (illegal instruction)',
                    -6:  'SIGABRT (abort)',
                    -7:  'SIGBUS (bus error)',
                    -8:  'SIGFPE (floating point exception)',
                    -11: 'SIGSEGV (segmentation fault)',
                }
                signame = crash_signals.get(code, f'signal {-code}')
                return 'ERROR', f'Process crashed: {signame} (exitcode {code})'

        # 3. Configgen error strings
        return detection.parse_error(
            stdout, stdout,
            self.error_markers,
            ''
        )

    # ------------------------------------------------------------------
    # Autofix — writes sidecar files rather than modifying recalbox.conf
    # ------------------------------------------------------------------

    def log_autofix_availability(self) -> None:
        """
        Log which systems have autofix combinations available on this device.

        Recalbox derives available combinations at runtime from each
        system's _readme.txt rather than a static table, so this cannot
        delegate to the shared autofix.log_available_combinations() —
        that function only knows about the Batocera FIX_COMBINATIONS
        table and would silently omit every Recalbox-specific system.

        Scans every system folder that has a _readme.txt and reports,
        per system, how many emulator/core combinations are installed.
        """
        roms_paths = [self.roms_path] + list(self.additional_roms_paths)
        seen: set[str] = set()
        found: list[tuple[str, int, list[str]]] = []   # (system, count, core_names)

        for base in roms_paths:
            if not os.path.isdir(base):
                continue
            try:
                systems = sorted(os.listdir(base))
            except PermissionError:
                continue
            for system in systems:
                if system in seen:
                    continue
                system_path = os.path.join(base, system)
                if not os.path.isdir(system_path):
                    continue
                readme = os.path.join(system_path, '_readme.txt')
                if not os.path.exists(readme):
                    continue
                seen.add(system)
                from_readme = self._parse_system_readme(system)
                available_cores: list[str] = []
                for emulator_type, core_name, _exts in from_readme:
                    if emulator_type == 'libretro':
                        so = os.path.join(
                            self.libretro_core_path,
                            f'{core_name}_libretro.so'
                        )
                        if os.path.exists(so):
                            available_cores.append(core_name)
                    else:
                        binary = (
                            self._find_standalone_binary(emulator_type) or
                            self._find_standalone_binary(core_name)
                        )
                        if binary:
                            available_cores.append(core_name)
                if available_cores:
                    found.append((system, len(available_cores), available_cores))

        if not found:
            log("Autofix: no systems with installed cores found.")
            return

        log("=" * 60)
        log(f"Recalbox autofix core availability ({len(found)} system(s) "
            f"with at least one installed core):")
        for system, count, cores in found:
            core_str = ', '.join(dict.fromkeys(cores))
            log(f"  [{system}] {count} core(s) available: {core_str}")
        log("=" * 60)

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
        Attempt to fix a failing ROM by trying alternative cores.

        On Recalbox, successful fixes are written as sidecar files
        alongside the ROM rather than modifying recalbox.conf.
        For ROMs from share_init, the sidecar is written to the
        equivalent share/roms path (which is writable).

        Args:
            system:      System folder name e.g. 'mame'
            rom:         Full path to the ROM file
            romname:     ROM filename e.g. 'pacman.zip'
            dashboard:   Dashboard instance
            state:       Shared state dict
            **kwargs:    installed_cores, slow_timeouts

        Returns:
            ('FIXED', notes), ('GENUINE ERROR', notes), or
            ('NO COMBINATIONS', notes)
        """
        launch_system   = self.SYSTEM_ALIAS_MAP.get(system, system)
        combinations    = self._get_autofix_combinations(launch_system, rom)

        if not combinations:
            return (
                'NO COMBINATIONS',
                f"No supported emulator/core combinations found for "
                f"[{system}] — check _readme.txt in ROM folder"
            )

        # Override _resolve_emulator_core rather than build_launch_cmd.
        # validate_rom_launch() calls _resolve_emulator_core() to find the
        # core for its extension/BIOS checks BEFORE run_test() ever calls
        # build_launch_cmd(). If we only override build_launch_cmd,
        # validate_rom_launch keeps resolving the GLOBAL default core
        # (e.g. recalbox.conf's mame2003_plus) regardless of which core
        # this loop is trying — so every attempt fails the extension check
        # against the wrong core before a subprocess is ever spawned.
        # Overriding the shared lower-level resolver makes both
        # validate_rom_launch and build_launch_cmd agree on the core
        # actually being tested.
        original_resolve = self._resolve_emulator_core
        total = len(combinations)

        for i, (emulator, core) in enumerate(combinations, 1):

            fix_desc = f"{emulator} / {core}"
            log(f"  [{i}/{total}] Trying: {fix_desc}")

            def _fixed_resolve(s, r, _core=core, _emulator=emulator):
                return _emulator, _core

            self._resolve_emulator_core = _fixed_resolve
            try:
                status, notes, elapsed = self.run_test(
                    launch_system, rom, dashboard, state,
                    timeout=kwargs.get('slow_timeouts', {}).get(system, 20)
                )
            finally:
                self._resolve_emulator_core = original_resolve

            log(f"    Result: {status} ({elapsed:.1f}s) {notes}")

            if status == 'OK':
                self._write_sidecar(rom, emulator, core)
                log(f"  Fixed with: {fix_desc}")
                return 'FIXED', f"Fixed: {fix_desc}"

        return 'GENUINE ERROR', "All fix combinations failed"

    def _write_sidecar(
        self,
        rom_path: str,
        emulator: str,
        core: str
    ) -> None:
        """
        Write a Recalbox per-ROM sidecar config file.

        For ROMs from share_init (read-only), the sidecar is written
        to the equivalent share/roms path which is writable.

        Format:
            global.emulator=libretro
            global.core=mame2010

        Args:
            rom_path: Full path to the ROM file.
            emulator: Emulator name e.g. 'libretro'.
            core:     Core name e.g. 'mame2010'.
        """
        sidecar_path = rom_path + '.recalbox.conf'
        if '/share_init/roms/' in sidecar_path:
            sidecar_path = sidecar_path.replace(
                '/share_init/roms/', '/share/roms/'
            )
        try:
            os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
            with open(sidecar_path, 'w') as f:
                f.write(f'global.emulator={emulator}\n')
                f.write(f'global.core={core}\n')
            log(f"  Sidecar written: {sidecar_path}")
        except Exception as e:
            log(f"  Warning: could not write sidecar: {e}")
