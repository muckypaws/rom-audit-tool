#!/usr/bin/env python3
"""
ROM Audit Tool — Prerequisites Check

Verifies that the system meets all requirements before running the
audit tool. Run this first on any new installation.

Reports each check as PASS, WARN or FAIL with explanatory notes.
A FAIL result will prevent the audit tool from working correctly.
A WARN result indicates a non-critical issue worth investigating.

Usage:
    python3 scripts/prereqs.py
    python3 scripts/prereqs.py --verbose
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import sys
import shutil
import platform
import subprocess
import importlib.util
import argparse

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

PASS  = '\033[32mPASS\033[0m'
WARN  = '\033[33mWARN\033[0m'
FAIL  = '\033[31mFAIL\033[0m'
INFO  = '\033[36mINFO\033[0m'

_verbose = False
_results = []   # (status, label, detail)


def result(status: str, label: str, detail: str = '') -> None:
    _results.append((status, label, detail))
    pad   = 45
    badge = {'PASS': PASS, 'WARN': WARN, 'FAIL': FAIL, 'INFO': INFO}.get(
        status, status
    )
    line  = f"  [{badge}]  {label:<{pad}}"
    if detail:
        line += f"  {detail}"
    print(line)


def section(title: str) -> None:
    print()
    print(f"  {title}")
    print(f"  {'─' * (len(title) + 2)}")


def verbose(msg: str) -> None:
    if _verbose:
        print(f"         {msg}")


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------

def check_python_version() -> None:
    section("Python")
    major, minor = sys.version_info.major, sys.version_info.minor
    version_str  = f"{major}.{minor}.{sys.version_info.micro}"

    if (major, minor) >= (3, 9):
        result('PASS', 'Python version', version_str)
    elif (major, minor) >= (3, 7):
        result('WARN', 'Python version',
               f"{version_str} — 3.9+ recommended")
    else:
        result('FAIL', 'Python version',
               f"{version_str} — 3.9+ required")


def check_stdlib_modules() -> None:
    section("Python standard library")
    required = [
        'csv', 'os', 'sys', 'subprocess', 'glob', 'shutil',
        'argparse', 'signal', 'time', 'datetime', 're',
        'collections', 'xml.etree.ElementTree',
    ]
    for mod in required:
        if importlib.util.find_spec(mod):
            verbose(f"  {mod}: found")
        else:
            result('FAIL', f'Module: {mod}', 'not found — stdlib missing?')
            return
    result('PASS', 'All required stdlib modules', 'present')


def detect_platform() -> str:
    """Return 'batocera', 'recalbox', 'retropie', or 'unknown'."""
    if (os.path.exists('/usr/bin/batocera-version') or
            os.path.exists('/userdata/system')):
        return 'batocera'
    if os.path.exists('/recalbox/share/system/recalbox.conf'):
        return 'recalbox'
    if (os.path.exists('/opt/retropie') and
            os.path.exists('/etc/rpi-issue') or
            os.path.exists('/opt/retropie')):
        # Check for RetroPie more carefully
        if os.path.exists('/opt/retropie'):
            return 'retropie'
    return 'unknown'


def check_platform() -> str:
    section("Platform detection")
    plat = detect_platform()

    if plat == 'batocera':
        try:
            r = subprocess.run(
                ['batocera-version'],
                capture_output=True, text=True
            )
            ver = r.stdout.strip().split()[0] if r.stdout.strip() else 'unknown'
            result('PASS', 'Platform', f"Batocera {ver}")
        except Exception:
            result('WARN', 'Platform', 'Batocera detected but version unreadable')

    elif plat == 'recalbox':
        try:
            ver_file = '/recalbox/share/system/recalbox.conf'
            # Recalbox version often in the frontend log or a version file
            ver_path = '/recalbox/recalbox.version'
            if os.path.exists(ver_path):
                with open(ver_path) as f:
                    ver = f.read().strip()
            else:
                ver = 'version unreadable'
            result('PASS', 'Platform', f"Recalbox {ver}")
        except Exception:
            result('PASS', 'Platform', 'Recalbox (version unreadable)')

    elif plat == 'retropie':
        try:
            with open('/opt/retropie/VERSION') as f:
                ver = f.read().strip()
            result('PASS', 'Platform', f"RetroPie {ver}")
        except Exception:
            result('PASS', 'Platform', 'RetroPie (version file unreadable)')

    else:
        os_info = platform.platform()
        result('WARN', 'Platform',
               f"Unknown — Batocera/RetroPie not detected ({os_info})")

    return plat


def check_batocera(verbose_mode: bool) -> None:
    section("Batocera specific")

    # configgen launcher
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    launcher_v40 = f"/usr/lib/python{py_ver}/site-packages/configgen/emulatorlauncher.py"
    launcher_cmd = "/usr/bin/python3 -m configgen.emulatorlauncher"

    if os.path.exists(launcher_v40):
        result('PASS', 'configgen launcher', launcher_v40)
    else:
        # Try finding any configgen
        found = glob_first('/usr/lib/python*/site-packages/configgen/emulatorlauncher.py')
        if found:
            result('PASS', 'configgen launcher', found)
        else:
            result('FAIL', 'configgen launcher',
                   'not found — emulatorlauncher.py missing')

    # ROM directory
    roms = '/userdata/roms'
    if os.path.isdir(roms):
        systems = [d for d in os.listdir(roms)
                   if os.path.isdir(os.path.join(roms, d))]
        result('PASS', 'ROMs directory', f"{roms} ({len(systems)} system(s))")
    else:
        result('FAIL', 'ROMs directory', f"{roms} not found")

    # batocera.conf
    conf = '/userdata/system/batocera.conf'
    if os.path.exists(conf):
        result('PASS', 'batocera.conf', conf)
    else:
        result('WARN', 'batocera.conf', f"{conf} not found — autofix will fail")

    # Libretro cores
    cores_path = '/usr/lib/libretro'
    if os.path.isdir(cores_path):
        cores = [f for f in os.listdir(cores_path) if f.endswith('_libretro.so')]
        result('PASS', 'Libretro cores', f"{len(cores)} core(s) in {cores_path}")
    else:
        result('WARN', 'Libretro cores',
               f"{cores_path} not found — no autofix combinations available")

    # Writable output directory
    audit_dir = '/userdata/system/rom_audit'
    _check_writable_dir(audit_dir, 'Audit output directory')

    # Disk space
    _check_disk_space('/userdata/system')

    # Log files directory
    logs = '/userdata/system/logs'
    if os.path.isdir(logs):
        result('PASS', 'Logs directory', logs)
    else:
        result('WARN', 'Logs directory',
               f"{logs} missing — log capture may fail")


def check_recalbox(verbose_mode: bool) -> None:
    section("Recalbox specific")

    # recalbox.conf
    conf = '/recalbox/share/system/recalbox.conf'
    if os.path.exists(conf):
        result('PASS', 'recalbox.conf', conf)
    else:
        result('FAIL', 'recalbox.conf',
               f'{conf} not found — Recalbox not properly installed')

    # ROM directory
    roms = '/recalbox/share/roms'
    if os.path.isdir(roms):
        systems = [d for d in os.listdir(roms)
                   if os.path.isdir(os.path.join(roms, d))]
        result('PASS', 'ROMs directory',
               f"{roms} ({len(systems)} system(s))")
    else:
        result('FAIL', 'ROMs directory', f"{roms} not found")

    # share_init ROMs (built-in Recalbox content)
    share_init = '/recalbox/share_init/roms'
    if os.path.isdir(share_init):
        systems_init = [d for d in os.listdir(share_init)
                        if os.path.isdir(os.path.join(share_init, d))]
        result('PASS', 'Built-in ROMs',
               f"{share_init} ({len(systems_init)} system(s))")
    else:
        result('WARN', 'Built-in ROMs', f"{share_init} not found")

    # Libretro cores
    cores_path = '/usr/lib/libretro'
    if os.path.isdir(cores_path):
        cores = [f for f in os.listdir(cores_path)
                 if f.endswith('_libretro.so')]
        result('PASS', 'Libretro cores',
               f"{len(cores)} core(s) in {cores_path}")
    else:
        result('FAIL', 'Libretro cores',
               f"{cores_path} not found — cannot run libretro cores")

    # emulatorlauncher
    launcher = '/usr/lib/python3.11/site-packages/configgen/emulatorlauncher.py'
    if os.path.exists(launcher):
        result('PASS', 'emulatorlauncher', launcher)
    else:
        # Search for it
        import glob
        found = glob.glob(
            '/usr/lib/python*/site-packages/configgen/emulatorlauncher.py'
        )
        if found:
            result('PASS', 'emulatorlauncher', found[0])
        else:
            result('FAIL', 'emulatorlauncher',
                   'configgen/emulatorlauncher.py not found')

    # Log directory
    logs = '/recalbox/share/system/logs'
    if os.path.isdir(logs):
        result('PASS', 'Log directory', logs)
    else:
        result('WARN', 'Log directory',
               f"{logs} not found — will be created on first launch")

    # BIOS directory
    bios = '/recalbox/share/bios'
    if os.path.isdir(bios):
        bios_files = []
        for root, dirs, files in os.walk(bios):
            bios_files.extend(files)
        result('PASS', 'BIOS directory',
               f"{bios} ({len(bios_files)} file(s))")
    else:
        result('WARN', 'BIOS directory',
               f"{bios} not found — some systems need BIOS files")

    # Screenshot tool
    if shutil.which('fbgrab'):
        result('PASS', 'Screenshot tool', 'fbgrab available')
    else:
        result('WARN', 'Screenshot tool',
               'fbgrab not found — --screenshot will not work')

    # Framebuffer
    if os.path.exists('/dev/fb0'):
        result('PASS', 'Framebuffer', '/dev/fb0 present')
    else:
        result('WARN', 'Framebuffer',
               '/dev/fb0 not found — screenshot capture may not work')

    # Disk space
    _check_disk_space('/recalbox/share/system')


def check_retropie(verbose_mode: bool) -> None:
    section("RetroPie specific")

    # Detect home directory
    retropie_home = _find_retropie_home()
    if retropie_home:
        result('PASS', 'RetroPie home', retropie_home)
    else:
        result('FAIL', 'RetroPie home',
               'No RetroPie/ directory found under any user home')
        retropie_home = os.path.expanduser('~')

    retropie_dir = os.path.join(retropie_home, 'RetroPie')

    # /opt/retropie
    if os.path.isdir('/opt/retropie'):
        result('PASS', '/opt/retropie', 'present')
    else:
        result('FAIL', '/opt/retropie', 'not found — RetroPie not installed')

    # RetroPie configs
    configs = '/opt/retropie/configs'
    if os.path.isdir(configs):
        systems = [d for d in os.listdir(configs)
                   if os.path.isdir(os.path.join(configs, d))]
        result('PASS', 'RetroPie configs',
               f"{configs} ({len(systems)} system(s))")
    else:
        result('FAIL', 'RetroPie configs', f"{configs} not found")

    # Libretro cores
    cores_path = '/opt/retropie/libretrocores'
    if os.path.isdir(cores_path):
        cores = []
        for root, dirs, files in os.walk(cores_path):
            cores.extend(f for f in files if f.endswith('_libretro.so'))
        result('PASS', 'Libretro cores',
               f"{len(cores)} core(s) in {cores_path}")
    else:
        result('WARN', 'Libretro cores',
               f"{cores_path} not found")

    # RetroArch binary
    retroarch = '/opt/retropie/emulators/retroarch/bin/retroarch'
    if os.path.exists(retroarch):
        result('PASS', 'RetroArch binary', retroarch)
    else:
        result('FAIL', 'RetroArch binary',
               f"{retroarch} not found — cannot launch ROMs")

    # ROM directory
    roms = os.path.join(retropie_dir, 'roms')
    if os.path.isdir(roms):
        systems = [d for d in os.listdir(roms)
                   if os.path.isdir(os.path.join(roms, d))]
        result('PASS', 'ROMs directory', f"{roms} ({len(systems)} system(s))")
    else:
        result('FAIL', 'ROMs directory', f"{roms} not found")

    # /dev/shm for appendconfig
    if os.path.isdir('/dev/shm') and os.access('/dev/shm', os.W_OK):
        result('PASS', '/dev/shm writable', 'appendconfig available')
    else:
        result('FAIL', '/dev/shm',
               'not writable — RetroArch appendconfig will fail')

    # /tmp/retroarch for ZIP extraction
    if os.path.isdir('/tmp/retroarch'):
        result('PASS', '/tmp/retroarch', 'exists (ZIP extraction enabled)')
    else:
        result('INFO', '/tmp/retroarch',
               'does not exist yet — will be created by tool on first run')

    # global emulators.cfg
    global_cfg = '/opt/retropie/configs/all/emulators.cfg'
    if os.path.exists(global_cfg):
        result('PASS', 'Global emulators.cfg', global_cfg)
    else:
        result('WARN', 'Global emulators.cfg',
               f"{global_cfg} not found — autofix overrides cannot be written")

    # Writable output directory
    audit_dir = os.path.join(retropie_dir, 'rom_audit')
    _check_writable_dir(audit_dir, 'Audit output directory')

    # Disk space
    _check_disk_space(retropie_dir)

    # fbset for screen detection
    if shutil.which('fbset'):
        result('PASS', 'fbset', 'available — screen resolution detection OK')
    else:
        result('INFO', 'fbset',
               'not found — will fall back to sysfs resolution detection')


def check_tools() -> None:
    section("System tools")

    # Kill tools
    if shutil.which('pkill'):
        result('PASS', 'pkill', 'available')
    elif shutil.which('killall'):
        result('WARN', 'pkill',
               'not found — killall will be used (BusyBox mode)')
    else:
        result('FAIL', 'pkill / killall',
               'neither found — cannot kill emulators after testing')

    # Python 3 on PATH
    py = shutil.which('python3')
    if py:
        result('PASS', 'python3 on PATH', py)
    else:
        result('FAIL', 'python3', 'not found in PATH')


def check_module_structure() -> None:
    section("ROM Audit Tool structure")

    # Locate rom_audit.py
    script_dir  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rom_audit   = os.path.join(script_dir, 'rom_audit.py')
    modules_dir = os.path.join(script_dir, 'modules')

    if os.path.exists(rom_audit):
        result('PASS', 'rom_audit.py', rom_audit)
    else:
        result('FAIL', 'rom_audit.py',
               f"not found at {rom_audit}")

    required_modules = [
        os.path.join('modules', 'common', 'logging.py'),
        os.path.join('modules', 'common', 'detection.py'),
        os.path.join('modules', 'common', 'filehandling.py'),
        os.path.join('modules', 'common', 'dashboard.py'),
        os.path.join('modules', 'common', 'autofix.py'),
        os.path.join('modules', 'common', 'configeditor.py'),
        os.path.join('modules', 'platforms', 'base.py'),
        os.path.join('modules', 'platforms', 'batocera.py'),
        os.path.join('modules', 'platforms', 'retropie.py'),
    ]

    missing = []
    for mod_rel in required_modules:
        full = os.path.join(script_dir, mod_rel)
        if not os.path.exists(full):
            missing.append(mod_rel)
        else:
            verbose(f"  {mod_rel}: found")

    if missing:
        for m in missing:
            result('FAIL', f'Missing: {m}', 'required module not found')
    else:
        result('PASS', 'All modules present',
               f"{len(required_modules)} module(s) found")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_retropie_home() -> str:
    """Find the home directory containing a RetroPie/ folder."""
    import pwd
    candidates = []
    for env_var in ('USER', 'SUDO_USER', 'LOGNAME'):
        user = os.environ.get(env_var, '').strip()
        if user and user != 'root':
            candidates.append(user)
    candidates.append('pi')

    for username in candidates:
        try:
            home = pwd.getpwnam(username).pw_dir
            if os.path.isdir(os.path.join(home, 'RetroPie')):
                return home
        except KeyError:
            continue

    try:
        for entry in pwd.getpwall():
            home = entry.pw_dir
            if os.path.isdir(os.path.join(home, 'RetroPie')):
                return home
    except Exception:
        pass

    return None


def _check_writable_dir(path: str, label: str) -> None:
    if os.path.isdir(path):
        if os.access(path, os.W_OK):
            result('PASS', label, path)
        else:
            result('FAIL', label, f"{path} exists but is not writable")
    else:
        # Try to create it
        try:
            os.makedirs(path, exist_ok=True)
            result('PASS', label, f"{path} (created)")
        except Exception as e:
            result('FAIL', label, f"cannot create {path}: {e}")


def _check_disk_space(path: str) -> None:
    try:
        stat = shutil.disk_usage(path)
        free_mb = stat.free // (1024 * 1024)
        if free_mb >= 500:
            result('PASS', 'Disk space',
                   f"{free_mb} MB free at {path}")
        elif free_mb >= 100:
            result('WARN', 'Disk space',
                   f"only {free_mb} MB free at {path} — audit logs may fill disk")
        else:
            result('FAIL', 'Disk space',
                   f"only {free_mb} MB free at {path} — insufficient")

        # Screenshot-specific advisory — each screenshot ~200KB,
        # 9000 ROMs ≈ 1.8GB. Mention threshold so user can plan.
        if free_mb < 2048:
            result('INFO', 'Screenshot mode',
                   f"--screenshot uses ~200KB per ROM; "
                   f"{free_mb} MB free may limit large audits. "
                   f"Tool disables screenshots automatically below 500 MB.")
    except Exception as e:
        result('WARN', 'Disk space', f"could not check: {e}")


def glob_first(pattern: str):
    """Return first match for a glob pattern, or None."""
    import glob
    matches = glob.glob(pattern)
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global _verbose

    parser = argparse.ArgumentParser(
        description='ROM Audit Tool — Prerequisites check'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show additional detail for each check'
    )
    args = parser.parse_args()
    _verbose = args.verbose

    print()
    print('  ROM Audit Tool — Prerequisites Check')
    print('  =====================================')
    print(f'  Python {sys.version.split()[0]} on {platform.system()} '
          f'{platform.machine()}')

    check_python_version()
    check_stdlib_modules()
    plat = check_platform()
    check_tools()
    check_module_structure()

    if plat == 'batocera':
        check_batocera(_verbose)
    elif plat == 'recalbox':
        check_recalbox(_verbose)
    elif plat == 'retropie':
        check_retropie(_verbose)
    else:
        section("Platform checks")
        result('WARN', 'Platform-specific checks',
               'skipped — unknown platform')

    # Summary
    passes  = sum(1 for s, _, _ in _results if s == 'PASS')
    warns   = sum(1 for s, _, _ in _results if s == 'WARN')
    fails   = sum(1 for s, _, _ in _results if s == 'FAIL')
    total   = passes + warns + fails

    print()
    print('  ' + '─' * 55)
    print(f'  Results: {passes} passed, {warns} warnings, {fails} failed '
          f'({total} checks)')
    print()

    if fails > 0:
        print('  \033[31mFAIL — resolve the FAIL items before running '
              'the audit tool.\033[0m')
    elif warns > 0:
        print('  \033[33mWARN — tool should work but review the warnings '
              'above.\033[0m')
    else:
        print('  \033[32mAll checks passed — ready to audit.\033[0m')
    print()

    sys.exit(1 if fails > 0 else 0)


if __name__ == '__main__':
    main()
