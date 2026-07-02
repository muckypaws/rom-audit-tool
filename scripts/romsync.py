#!/usr/bin/env python3
"""
romsync.py  -  Universal file sync with ROM-aware stem and MD5 deduplication.
Pure Python 3 stdlib only - no pip/external modules required.

Works on:
  - macOS (bash 3.2+, Python 3 via homebrew or system)
  - Linux (Ubuntu, Debian, Arch, etc.)
  - Batocera / Recalbox / RetroPie  (uses system Python 3)
  - Windows (Python 3 from python.org, OpenSSH from Optional Features)

Remote access shells out to: ssh, scp, rsync
These are available natively on all supported platforms.

Config saved to: ~/.romsync_config.json  (passwords never saved)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  EXTENSION CLASSIFICATION:

  COMPRESSED_EXTENSIONS  (.zip, .7z, .chd …)
    Stem-only conflict detection. MD5 never matches across
    compression formats so it is not attempted.

  RAW_EXTENSIONS  (.bin, .a52, .a78, .nes, .sfc …)
    Stem conflict detection first; if no exact stem match
    is found, MD5 is compared against all raw-format files
    in the target directory. A hash match means the same
    ROM exists under a different name — reported as a
    duplicate and skipped, never silently dropped.

  All other files (media, config, XML …)
    Always Tier 1 / 2 — straight rsync, no analysis.

  TIERED STRATEGY (applied to every directory in the tree):

  Tier 1 — Target dir does not exist
           → rsync the entire source dir

  Tier 2 — Target dir exists, no stem conflicts
           → rsync the entire source dir

  Tier 3 — Target dir has conflicting stems or MD5 matches
           → rsync excluding conflicted / duplicate files
           → selectively copy genuinely new files
           → report duplicates detected by MD5
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import hashlib
import os
import sys
import json
import shutil
import subprocess
import getpass
import argparse
import tempfile
import platform
import functools
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Optional

# Force stdout and stderr to flush after every write so output always reaches
# redirected files even when rsync subprocess calls exit unexpectedly.
# This is equivalent to running Python with -u but works without that flag.
import io as _io
sys.stdout = _io.TextIOWrapper(
    sys.stdout.buffer, encoding=sys.stdout.encoding,
    errors=sys.stdout.errors, line_buffering=True
)
sys.stderr = _io.TextIOWrapper(
    sys.stderr.buffer, encoding=sys.stderr.encoding,
    errors=sys.stderr.errors, line_buffering=True
)

# ─────────────────────────────────────────────────────────────────────────────
# Extension classification
# ─────────────────────────────────────────────────────────────────────────────

# Compressed ROM containers — stem conflict detection only.
# MD5 across different compression formats will never match.
COMPRESSED_EXTENSIONS: frozenset[str] = frozenset({
    ".zip", ".7z", ".gz", ".bz2", ".xz", ".rar", ".tar",
    ".chd",
    ".rvz", ".wbfs", ".wad", ".nsp", ".xci",
    ".pbp", ".pkg",
    ".cdi",
    ".nrg", ".mdf", ".mds", ".ccd",
    ".lha", ".lzh",   # Amiga (and classic LHA/LZH archive format generally)
    ".adf", ".adz",   # Amiga floppy disk images
})

# Raw / uncompressed ROM dumps — stem conflict detection first,
# then MD5 fallback for cross-extension duplicate detection.
# These are flat binary images; the same ROM may exist as e.g.
# both .bin and .a52, or .bin and .a78, with identical bytes.
RAW_EXTENSIONS: frozenset[str] = frozenset({
    # Generic raw dump
    ".bin", ".rom",
    # Atari
    ".a26", ".a52", ".a78",
    # Nintendo
    ".nes", ".fds",
    ".sfc", ".smc",
    ".n64", ".z64", ".v64",
    ".gb", ".gbc", ".gba", ".nro",
    ".nds",
    # Sega
    ".md", ".smd", ".gen",
    ".32x", ".gg", ".sms",
    # NEC
    ".pce",
    # Bandai
    ".ws", ".wsc",
    # Atari Lynx
    ".lnx",
    # SNK
    ".ngp", ".ngc", ".ngpc",
    # Disc images (uncompressed)
    ".iso", ".img", ".cue", ".gdi", ".sub",
    # Other
    ".3ds", ".cia", ".nca",
})

# All extensions that trigger conflict / duplicate analysis
CONFLICT_EXTENSIONS: frozenset[str] = COMPRESSED_EXTENSIONS | RAW_EXTENSIONS

# Media subdirectory names excluded by --exclude-media
# These are the conventional names used by Skyscraper, Screenscraper,
# and the various front-ends (Batocera, RetroPie, Recalbox, ES-DE).
MEDIA_DIRS: frozenset[str] = frozenset({
    "media", "images", "videos", "manuals", "marquees",
    "wheels", "boxart", "screenshots", "thumbnails",
    "fanart", "logos", "covers", "mix", "maps",
})

# Gamelist files excluded by --exclude-gamelist (or implied by --exclude-media)
GAMELIST_FILES: frozenset[str] = frozenset({
    "gamelist.xml", "gamelist.xml.old",
})

# macOS metadata excluded by default (override with --include-macos-metadata).
# These are created automatically by macOS on any volume it touches, including
# network shares and external drives, and should never be part of a ROM
# collection. Patterns use rsync's own glob syntax (used directly in the
# exclude-from file).
MACOS_METADATA_PATTERNS: tuple[str, ...] = (
    "._*",                  # AppleDouble resource fork files
    ".DS_Store",
    ".Spotlight-V100/",
    ".Trashes/",
    ".fseventsd/",
    ".TemporaryItems/",
    ".VolumeIcon.icns",
    ".com.apple.timemachine.donotpresent",
    ".AppleDouble/",
    ".AppleDB/",
    ".AppleDesktop/",
    "Network Trash Folder/",
    "Temporary Items/",
)

# ─────────────────────────────────────────────────────────────────────────────
# Platform helpers
# ─────────────────────────────────────────────────────────────────────────────

IS_WINDOWS = platform.system() == "Windows"


def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def _require(cmd: str) -> str:
    path = _which(cmd)
    if not path:
        raise RuntimeError(
            f"'{cmd}' not found on PATH.\n"
            f"  macOS   : brew install {cmd}\n"
            f"  Windows : enable OpenSSH in Settings → Optional Features\n"
            f"  Linux   : sudo apt install {cmd}   (or distro equivalent)\n"
            f"  Batocera: opkg install {cmd}"
        )
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path.home() / ".romsync_config.json"

ENDPOINT_DEFAULTS: dict = {
    "type":    "local",
    "path":    "",
    "host":    "",
    "port":    22,
    "user":    "",
    "ssh_key": "",
}

OPTIONS_DEFAULTS: dict = {
    "dry_run":                False,
    "verbose":                False,
    "exclude_media":          False,
    "exclude_gamelist":       False,
    "include_macos_metadata": False,
    "preserve_target":        False,
    "prefer_extension":       "7z",   # default tiebreaker for source-internal
                                       # duplicates (e.g. avengers.7z vs
                                       # avengers.zip both in source);
                                       # override with --prefer-extension zip
    "no_walk":                False,  # only process top-level files in the
                                       # source directory; subdirectories are
                                       # never walked or touched
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    result = base.copy()
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> dict:
    defaults = {
        "source":  ENDPOINT_DEFAULTS.copy(),
        "target":  ENDPOINT_DEFAULTS.copy(),
        "options": OPTIONS_DEFAULTS.copy(),
    }
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open() as fh:
                saved = json.load(fh)
            return _deep_merge(defaults, saved)
        except (json.JSONDecodeError, KeyError):
            pass
    return defaults


def save_config(config: dict):
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    print(f"  Settings saved → {CONFIG_FILE}")


# ─────────────────────────────────────────────────────────────────────────────
# Password handling (in-memory only — never written to config or disk)
#
# Two opt-in ways to supply a password per endpoint:
#   --ask-source-password / --ask-target-password   prompts once via getpass
#   --source-password VALUE / --target-password VALUE   on the command line
#
# Either way, the password is held only in this process's memory for the
# duration of the run and passed to ssh/scp/rsync via sshpass. It is never
# written to ~/.romsync_config.json, never logged, and the in-memory dict
# is cleared at the end of run_sync().
#
# Putting a password directly on the command line (--source-password) is
# visible in shell history and to other processes via `ps`. This is offered
# as a convenience for trusted local/home-network use, not recommended for
# shared or multi-user systems. The --ask-*-password prompt avoids both
# exposures and is the safer of the two.
# ─────────────────────────────────────────────────────────────────────────────

# Keyed the same way as _CONTROL_SOCKETS / _NO_CONTROL_MASTER: user@host:port
_PASSWORDS: dict[str, str] = {}


def set_password(cfg: dict, password: str) -> None:
    if cfg.get("type") == "ssh" and cfg.get("host"):
        _PASSWORDS[_host_key(cfg)] = password


def get_password(cfg: dict) -> Optional[str]:
    return _PASSWORDS.get(_host_key(cfg))


def clear_passwords() -> None:
    _PASSWORDS.clear()


def find_sshpass() -> Optional[str]:
    return shutil.which("sshpass")


def _sshpass_wrap(cmd: list[str], cfg: dict) -> list[str]:
    """
    Prefix cmd with sshpass if a password has been supplied for this host
    and sshpass is available on PATH. Returns cmd unmodified otherwise,
    in which case ssh/scp will prompt interactively as normal.
    """
    password = get_password(cfg)
    if not password:
        return cmd

    sshpass = find_sshpass()
    if not sshpass:
        # Only warn once per host per run to avoid spamming output
        warn_key = f"_sshpass_warned_{_host_key(cfg)}"
        if not getattr(_sshpass_wrap, warn_key, False):
            setattr(_sshpass_wrap, warn_key, True)
            print(
                f"  Note: a password was supplied for {_user_host(cfg)} but "
                f"'sshpass' is not installed,\n"
                f"        so it can't be used automatically. You will be "
                f"prompted normally instead.\n"
                f"        To enable automatic password entry:\n"
                f"          macOS  : brew install sshpass\n"
                f"          Linux  : sudo apt install sshpass\n"
            )
        return cmd

    # sshpass reads the password via -p (visible briefly in `ps` on some
    # systems) or via -e (environment variable, generally safer). We use
    # -e to avoid the password appearing in the process list.
    return [sshpass, "-e"] + cmd


def _sshpass_env(cfg: dict) -> Optional[dict]:
    """
    Return an environment dict with SSHPASS set for this host's password,
    or None if no password is set for this host. Used alongside
    _sshpass_wrap so subprocess.run can pass SSHPASS via env= rather than
    a command-line argument.
    """
    password = get_password(cfg)
    if not password:
        return None
    env = os.environ.copy()
    env["SSHPASS"] = password
    return env


# ─────────────────────────────────────────────────────────────────────────────
# SSH helpers
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# SSH ControlMaster — single authenticated connection reused by every
# subsequent ssh/scp/rsync call in this run, so the user is only prompted
# for a password once even without key-based auth.
# ─────────────────────────────────────────────────────────────────────────────

import uuid as _uuid

# One control socket path per endpoint config (keyed by host) for this run.
# Populated by ensure_control_master(); never written to disk.
_CONTROL_SOCKETS: dict[str, str] = {}

# Hosts where ControlMaster is known NOT to work (e.g. Dropbear SSH servers,
# common on Batocera/Recalbox), keyed the same way as _CONTROL_SOCKETS.
# Populated by ensure_control_master() after detecting the server banner.
_NO_CONTROL_MASTER: set[str] = set()


def _control_socket_path(cfg: dict) -> str:
    """
    Return a stable control socket path for this host, created once per run.
    Stored in the system temp dir and removed by teardown_control_masters().
    """
    key = f"{cfg.get('user','')}@{cfg['host']}:{cfg.get('port', 22)}"
    if key not in _CONTROL_SOCKETS:
        token = _uuid.uuid4().hex[:8]
        sock_dir = Path(tempfile.gettempdir())
        _CONTROL_SOCKETS[key] = str(sock_dir / f"romsync_cm_{token}.sock")
    return _CONTROL_SOCKETS[key]


def _host_key(cfg: dict) -> str:
    return f"{cfg.get('user','')}@{cfg['host']}:{cfg.get('port', 22)}"


def _detect_dropbear(cfg: dict) -> bool:
    """
    Detect whether the remote SSH server is Dropbear (common on Batocera
    and Recalbox) by reading the SSH protocol banner directly over a raw
    socket. Dropbear identifies itself as e.g. 'SSH-2.0-dropbear_2022.82'.

    Dropbear does not implement OpenSSH's ControlMaster multiplexing
    protocol — attempting to use -o ControlMaster=yes against it can
    cause the connection to be closed outright rather than falling back
    gracefully. Detecting this upfront lets us skip ControlMaster for
    these hosts rather than failing silently.
    """
    import socket as _socket
    try:
        with _socket.create_connection(
            (cfg["host"], cfg.get("port", 22)), timeout=5
        ) as sock:
            banner = sock.recv(256).decode("utf-8", errors="ignore")
        return "dropbear" in banner.lower()
    except Exception:
        # If we can't tell, assume it's fine to try ControlMaster —
        # worst case ensure_control_master's own check will catch a failure.
        return False


def ensure_control_master(cfg: dict, verbose: bool = False) -> None:
    """
    Open a background SSH ControlMaster connection for this host if one
    isn't already running and the server supports it. Any password/
    passphrase prompt happens exactly once here; every later ssh/scp/rsync
    call for this host reuses the same authenticated connection via the
    control socket.

    The password itself is never captured, stored, or written to disk —
    OpenSSH handles the prompt directly on the terminal, and the resulting
    authenticated session is simply kept alive and shared.

    Hosts running Dropbear (Batocera, Recalbox) do not support
    ControlMaster multiplexing. These are detected up front via the SSH
    banner and silently skipped — _ssh_opts() will omit ControlPath for
    them, and the user will see the normal per-operation password prompts
    instead. A one-time notice explains this and suggests key-based auth.
    """
    if cfg["type"] != "ssh":
        return

    key = _host_key(cfg)

    if key in _NO_CONTROL_MASTER:
        return  # already known unsupported, nothing to do

    if _detect_dropbear(cfg):
        _NO_CONTROL_MASTER.add(key)
        print(
            f"  Note: {cfg['host']} is running a Dropbear SSH server, which "
            f"does not support\n"
            f"        connection multiplexing. You may be prompted for a "
            f"password more than\n"
            f"        once during this run. For a single-prompt experience, "
            f"set up SSH key\n"
            f"        authentication: ssh-copy-id {_user_host(cfg)}"
        )
        return

    sock = _control_socket_path(cfg)

    # Already running?
    check = subprocess.run(
        ["ssh", "-O", "check", "-o", f"ControlPath={sock}", _user_host(cfg)],
        capture_output=True, text=True,
    )
    if check.returncode == 0:
        return  # master already up

    if verbose:
        if get_password(cfg):
            print(f"  Opening SSH connection to {_user_host(cfg)} "
                  f"using supplied password…")
        else:
            print(f"  Opening SSH connection to {_user_host(cfg)} "
                  f"(you may be prompted for a password once)…")

    ssh = _require("ssh")
    cmd = [
        ssh,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",   # suppress PQ-kex notice, see _ssh_opts()
        "-p", str(cfg.get("port", 22)),
        "-o", "ControlMaster=yes",
        "-o", f"ControlPath={sock}",
        "-o", "ControlPersist=600",   # keep alive 10 min after last use
        "-fN",                         # background, no remote command
    ]
    key_path = cfg.get("ssh_key", "")
    if key_path:
        cmd += ["-i", key_path]
    cmd.append(_user_host(cfg))
    cmd = _sshpass_wrap(cmd, cfg)

    # Run attached to the real terminal so any password prompt is visible
    # and interactive — never captured or piped through Python. If a
    # password was supplied, it travels via the SSHPASS env var only.
    result = subprocess.run(cmd, env=_sshpass_env(cfg))
    if result.returncode != 0:
        # Fall back gracefully rather than aborting the whole sync — mark
        # this host as unsupported and let normal per-call auth proceed.
        _NO_CONTROL_MASTER.add(key)
        print(
            f"  Note: could not establish a multiplexed SSH connection to "
            f"{_user_host(cfg)}\n"
            f"        (exit {result.returncode}). Falling back to "
            f"per-operation authentication.\n"
            f"        For a single-prompt experience, set up SSH key "
            f"authentication:\n"
            f"        ssh-copy-id {_user_host(cfg)}"
        )


def teardown_control_masters(verbose: bool = False) -> None:
    """
    Close every ControlMaster opened during this run. Always called at the
    end of run_sync(), even on error, so no background ssh processes or
    socket files are left behind.
    """
    for key, sock in list(_CONTROL_SOCKETS.items()):
        if key in _NO_CONTROL_MASTER:
            continue   # never actually opened for this host
        try:
            subprocess.run(
                ["ssh", "-O", "exit", "-o", f"ControlPath={sock}", "x"],
                capture_output=True, text=True,
            )
        except Exception:
            pass
        # Belt-and-braces: remove the socket file if it lingers
        try:
            if os.path.exists(sock):
                os.unlink(sock)
        except OSError:
            pass
        if verbose:
            print(f"  Closed SSH connection ({key})")
    _CONTROL_SOCKETS.clear()
    _NO_CONTROL_MASTER.clear()


def _ssh_opts(cfg: dict) -> list[str]:
    opts = [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=no",
        "-p", str(cfg.get("port", 22)),
        # Suppress OpenSSH 9.9+'s "not using a post-quantum key exchange"
        # notice. Dropbear (Batocera, Recalbox) never supports PQ kex, so
        # this prints on every single connection — and with this tool
        # making one ssh/scp call per directory/file in some tiers, that's
        # a lot of repeated noise for a warning that doesn't apply to a
        # home-network ROM sync. LogLevel=ERROR still surfaces genuine
        # errors and fatal conditions; it only hides informational notices.
        "-o", "LogLevel=ERROR",
    ]
    key = cfg.get("ssh_key", "")
    if key:
        opts += ["-i", key]
    # Reuse the ControlMaster connection if one has been established for
    # this host AND the server is known to support it. Dropbear-based
    # servers (Batocera, Recalbox) are excluded — see ensure_control_master.
    if cfg.get("type") == "ssh" and cfg.get("host"):
        host_key = _host_key(cfg)
        if host_key not in _NO_CONTROL_MASTER:
            sock = _control_socket_path(cfg)
            opts += ["-o", f"ControlPath={sock}"]
    return opts


def _user_host(cfg: dict) -> str:
    user = cfg.get("user", "")
    host = cfg["host"]
    return f"{user}@{host}" if user else host


def _ssh_run(cfg: dict, remote_cmd: str) -> subprocess.CompletedProcess:
    ssh = _require("ssh")
    cmd = [ssh] + _ssh_opts(cfg) + [_user_host(cfg), remote_cmd]
    cmd = _sshpass_wrap(cmd, cfg)
    return subprocess.run(cmd, capture_output=True, text=True, env=_sshpass_env(cfg))


def _rsync_e_arg(cfg: dict) -> str:
    """
    Build the -e 'ssh ...' argument for rsync's transport command.
    If a password has been supplied for this host and sshpass is available,
    sshpass is embedded ahead of ssh so rsync's spawned ssh process picks
    up the password non-interactively (via the SSHPASS env var, set on the
    rsync subprocess itself — see rsync_dir()).
    """
    ssh_part = "ssh " + " ".join(_ssh_opts(cfg))
    if get_password(cfg) and find_sshpass():
        return "sshpass -e " + ssh_part
    return ssh_part


def _endpoint_path(cfg: dict, path: str, trailing_slash: bool = False) -> str:
    p = path.rstrip("/") + ("/" if trailing_slash else "")
    if cfg["type"] == "ssh":
        return f"{_user_host(cfg)}:{p}"
    return p


def _q(s: str) -> str:
    """Minimal POSIX shell quoting."""
    return "'" + s.replace("'", "'\\''") + "'"


# ─────────────────────────────────────────────────────────────────────────────
# MD5 computation  (local and remote)
# ─────────────────────────────────────────────────────────────────────────────

def _md5_local(path: str) -> Optional[str]:
    """MD5 a local file. Returns hex digest or None on error."""
    try:
        h = hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        print(f"    Warning: could not MD5 {path}: {exc}")
        return None


def _md5_remote(cfg: dict, remote_path: str) -> Optional[str]:
    """
    MD5 a remote file via SSH.
    Tries md5sum (Linux / Batocera / RetroPie) then md5 (macOS).
    Returns hex digest or None on error.
    """
    # md5sum outputs:  <hash>  <filename>
    # md5     outputs: MD5 (<filename>) = <hash>
    cmd = (
        f"if command -v md5sum >/dev/null 2>&1; then "
        f"  md5sum {_q(remote_path)} | awk '{{print $1}}'; "
        f"else "
        f"  md5 {_q(remote_path)} | awk '{{print $NF}}'; "
        f"fi"
    )
    result = _ssh_run(cfg, cmd)
    digest = result.stdout.strip()
    if result.returncode != 0 or not digest or len(digest) != 32:
        print(f"    Warning: could not MD5 remote {remote_path}")
        return None
    return digest


def compute_md5(cfg: dict, full_path: str) -> Optional[str]:
    if cfg["type"] == "ssh":
        return _md5_remote(cfg, full_path)
    return _md5_local(full_path)


# ─────────────────────────────────────────────────────────────────────────────
# Directory / file listing
# ─────────────────────────────────────────────────────────────────────────────

def dir_exists(cfg: dict, path: str) -> bool:
    if cfg["type"] == "ssh":
        r = _ssh_run(cfg, f"test -d {_q(path)} && echo yes || echo no")
        if r.returncode != 0:
            raise RuntimeError(
                f"Could not check remote directory '{path}' on "
                f"{_user_host(cfg)} (ssh exit {r.returncode}): "
                f"{r.stderr.strip() or '(no error output)'}"
            )
        return r.stdout.strip() == "yes"
    return Path(path).is_dir()


def list_immediate_files(cfg: dict, path: str) -> list[str]:
    """Bare filenames of files directly inside path (non-recursive)."""
    if cfg["type"] == "ssh":
        base = path.rstrip("/")
        cmd  = (
            f"[ -d {_q(base)} ] && "
            f"find {_q(base)} -mindepth 1 -maxdepth 1 -type f -print "
            f"|| true"
        )
        r = _ssh_run(cfg, cmd)
        if r.returncode != 0:
            raise RuntimeError(
                f"Could not list remote files in '{path}' on "
                f"{_user_host(cfg)} (ssh exit {r.returncode}): "
                f"{r.stderr.strip() or '(no error output)'}"
            )
        prefix = base + "/"
        return [
            line[len(prefix):] if line.startswith(prefix) else line
            for line in (l.strip() for l in r.stdout.splitlines())
            if line
        ]
    p = Path(path)
    if not p.is_dir():
        return []
    return [f.name for f in p.iterdir() if f.is_file()]


def walk_source_dirs(cfg: dict, base: str) -> list[str]:
    """
    Return every directory path relative to base (including "" for root).
    Single SSH round-trip for remote sources.
    """
    if cfg["type"] == "ssh":
        base = base.rstrip("/")
        r    = _ssh_run(cfg, f"find {_q(base)} -type d -print")
        if r.returncode != 0:
            raise RuntimeError(f"Remote find failed:\n{r.stderr.strip()}")
        prefix = base + "/"
        dirs   = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line == base:
                dirs.append("")
            elif line.startswith(prefix):
                dirs.append(line[len(prefix):])
        return dirs

    base_path = Path(base)
    if not base_path.is_dir():
        return []
    dirs = [""]
    for p in sorted(base_path.rglob("*")):
        if p.is_dir():
            dirs.append(str(p.relative_to(base_path)).replace("\\", "/"))
    return dirs


# ─────────────────────────────────────────────────────────────────────────────
# Stem conflict detection
# ─────────────────────────────────────────────────────────────────────────────

def find_stem_conflicts(filenames: list[str]) -> set[str]:
    """
    Stems that appear with more than one CONFLICT_EXTENSION in the list.
    Only considers extensions in CONFLICT_EXTENSIONS.
    """
    stem_exts: dict[str, set[str]] = defaultdict(set)
    for name in filenames:
        p   = PurePosixPath(name)
        ext = p.suffix.lower()
        if ext in CONFLICT_EXTENSIONS:
            stem_exts[p.stem].add(ext)
    return {stem for stem, exts in stem_exts.items() if len(exts) > 1}


# ─────────────────────────────────────────────────────────────────────────────
# MD5 duplicate detection  (raw extensions only)
# ─────────────────────────────────────────────────────────────────────────────

def build_target_md5_index(
    cfg:       dict,
    dir_path:  str,
    filenames: list[str],
    verbose:   bool,
) -> dict[str, str]:
    """
    Compute MD5 for every RAW_EXTENSION file in filenames (which are bare
    names inside dir_path).  Returns { md5_hex: filename }.
    Only called when we know there are raw-format files that need checking.
    """
    index: dict[str, str] = {}
    raw_files = [f for f in filenames if PurePosixPath(f).suffix.lower() in RAW_EXTENSIONS]

    if not raw_files:
        return index

    if verbose:
        print(f"    MD5 indexing {len(raw_files)} raw file(s) on target …")

    for name in raw_files:
        full = f"{dir_path.rstrip('/')}/{name}"
        digest = compute_md5(cfg, full)
        if digest:
            index[digest] = name
            if verbose:
                print(f"      {digest[:8]}…  {name}")

    return index


# ─────────────────────────────────────────────────────────────────────────────
# rsync
# ─────────────────────────────────────────────────────────────────────────────

def rsync_dir(
    src_cfg:         dict,
    src_path:        str,
    dst_cfg:         dict,
    dst_path:        str,
    dry_run:         bool,
    verbose:         bool,
    extra_excludes:  Optional[list[str]] = None,
    preserve_target: bool = False,
    no_recurse:      bool = False,
):
    """
    rsync src_path/ to dst_path.

    Excludes are written to a temporary file and passed via --exclude-from
    rather than as individual --exclude arguments.  This avoids OS ARG_MAX
    limits (as low as 128 KB on embedded Linux kernels used by Batocera /
    RetroPie) which can be hit when syncing directories with thousands of
    duplicate ROM filenames.

    preserve_target: if True, adds rsync's --ignore-existing flag, so any
    file that already exists on the target by exact filename is left
    completely untouched, regardless of size or modification time
    differences. This protects against overwriting target files that have
    been independently verified or rebuilt (e.g. via ClrMamePro audit)
    with a possibly different — but not necessarily better — source copy.
    Genuinely new filenames are still copied normally.

    no_recurse: if True (set via --no-walk), only files directly inside
    src_path are transferred; subdirectories are never entered. Implemented
    via rsync's --dirs (-d) flag combined with --no-recursive.

    Two earlier approaches were tried and found NOT to work, kept here as
    a note so this isn't re-attempted:
      1. Manually reconstructing -a's component flags without -r
         (i.e. -lptgoD) — rsync treats the source directory argument
         itself as an entry it cannot descend into without -r or -d,
         and skips it entirely ("skipping directory .").
      2. -a plus --no-recursive alone — same "skipping directory ."
         result; --no-recursive alone is not sufficient.
    The combination that was verified to actually work (manually tested
    against a live target) is -a --no-recursive --dirs together: --dirs
    tells rsync it's allowed to process the named directory argument
    itself (transferring its immediate file contents), while
    --no-recursive still prevents it from following any subdirectories
    found within it.
    """
    rsync = _require("rsync")
    # --itemize-changes (-i) shows exactly what rsync decides per file:
    #   >f.st...... = would transfer, size/time differ
    #   .f.......   = unchanged, nothing to do
    # This lets the user verify whether files already on the target are
    # being correctly skipped rather than re-transferred/overwritten.
    if no_recurse:
        base_flags = ["-av", "--no-recursive", "--dirs",
                      "--itemize-changes", "--progress"] if verbose \
                     else ["-a", "--no-recursive", "--dirs", "--stats"]
    else:
        base_flags = ["-av", "--itemize-changes", "--progress"] if verbose \
                     else ["-a", "--stats"]
    flags = list(base_flags)
    if dry_run:
        flags.append("--dry-run")
    if preserve_target:
        flags.append("--ignore-existing")

    cmd = [rsync] + flags

    # Write excludes to a tempfile - one pattern per line.
    # rsync --exclude-from reads the file itself so the patterns never touch
    # the command line, bypassing ARG_MAX entirely.
    all_excludes = list(extra_excludes or [])
    if no_recurse:
        # Belt-and-braces: even with --no-recursive, explicitly exclude any
        # subdirectory pattern so nothing nested is ever touched.
        all_excludes.append("*/")

    tmp_path = None
    if all_excludes:
        fd, tmp_path = tempfile.mkstemp(prefix="romsync_excl_", suffix=".txt")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(all_excludes) + "\n")
        except Exception:
            os.close(fd)
            raise
        cmd += ["--exclude-from", tmp_path]
        if verbose:
            print(f"    exclude-from: {tmp_path}  ({len(all_excludes)} pattern(s))")

    if src_cfg["type"] == "ssh" or dst_cfg["type"] == "ssh":
        remote_cfg = src_cfg if src_cfg["type"] == "ssh" else dst_cfg
        cmd += ["-e", _rsync_e_arg(remote_cfg)]

    src_str = _endpoint_path(src_cfg, src_path, trailing_slash=True)
    dst_str = _endpoint_path(dst_cfg, dst_path, trailing_slash=False)
    cmd    += [src_str, dst_str]

    if dry_run or verbose:
        print(f"    cmd: {' '.join(cmd)}")

    # If either endpoint has a password set, export SSHPASS so the -e
    # 'sshpass -e ssh ...' transport command (built in _rsync_e_arg) can
    # read it. rsync's own env is inherited by the ssh process it spawns.
    rsync_env = None
    for endpoint_cfg in (src_cfg, dst_cfg):
        pw_env = _sshpass_env(endpoint_cfg)
        if pw_env is not None:
            rsync_env = pw_env
            break

    try:
        # stdout/stderr are inherited from our process so rsync output
        # follows whatever redirection the caller applied (tee, >, etc.)
        result = subprocess.run(cmd, text=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                env=rsync_env)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.returncode not in (0, 24):
            raise RuntimeError(f"rsync exited {result.returncode}")
    finally:
        # Always clean up the tempfile, even on error
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ─────────────────────────────────────────────────────────────────────────────
# Selective file copy via scp  (Tier 3 — genuinely new files only)
# ─────────────────────────────────────────────────────────────────────────────

def _scp_file(
    src_full: str, src_cfg: Optional[dict],
    dst_full: str, dst_cfg: Optional[dict],
):
    scp        = _require("scp")
    remote_cfg = (
        src_cfg if (src_cfg and src_cfg.get("type") == "ssh") else dst_cfg
    )
    port = str(remote_cfg.get("port", 22)) if remote_cfg else "22"
    key  = remote_cfg.get("ssh_key", "")   if remote_cfg else ""

    cmd = [scp, "-P", port,
           "-o", "StrictHostKeyChecking=accept-new",
           "-o", "LogLevel=ERROR"]   # suppress PQ-kex notice, see _ssh_opts()
    if key:
        cmd += ["-i", key]
    # Reuse the ControlMaster connection for this host if one was opened
    # and the server supports it — avoids a fresh password prompt on every
    # selective-copy scp call. Skipped for Dropbear-based servers.
    if remote_cfg:
        host_key = _host_key(remote_cfg)
        if host_key not in _NO_CONTROL_MASTER:
            sock = _control_socket_path(remote_cfg)
            cmd += ["-o", f"ControlPath={sock}"]

    src_str = (
        f"{_user_host(src_cfg)}:{src_full}"
        if src_cfg and src_cfg.get("type") == "ssh" else src_full
    )
    dst_str = (
        f"{_user_host(dst_cfg)}:{dst_full}"
        if dst_cfg and dst_cfg.get("type") == "ssh" else dst_full
    )
    cmd += [src_str, dst_str]
    cmd = _sshpass_wrap(cmd, remote_cfg) if remote_cfg else cmd
    result = subprocess.run(cmd, text=True,
                            env=_sshpass_env(remote_cfg) if remote_cfg else None)
    if result.returncode != 0:
        raise RuntimeError(f"scp exited {result.returncode}")


def _ensure_dst_dir(dst_cfg: dict, dst_dir: str):
    if dst_cfg["type"] == "ssh":
        r = _ssh_run(dst_cfg, f"mkdir -p {_q(dst_dir)}")
        if r.returncode != 0:
            raise RuntimeError(
                f"Could not create remote directory '{dst_dir}' on "
                f"{_user_host(dst_cfg)}: {r.stderr.strip() or '(no error output)'}"
            )
    else:
        Path(dst_dir).mkdir(parents=True, exist_ok=True)


def _ensure_dst_parent(dst_cfg: dict, dst_dir: str, dry_run: bool):
    """
    Ensure dst_dir itself exists before rsync runs into it.

    We always create the directory structure even during dry-run, because:
    - rsync --dry-run does NOT create directories on the target
    - child directories in the walk will fail if their parent was only
      "created" by a dry-run rsync call that never actually wrote anything
    - creating empty directories has no effect on the ROM collection itself
      and is the only way child Tier 1 dirs can be processed correctly

    rsync is still run with --dry-run so no files are transferred.
    """
    if dst_cfg["type"] == "ssh":
        r = _ssh_run(dst_cfg, f"mkdir -p {_q(dst_dir)}")
        if r.returncode != 0:
            raise RuntimeError(
                f"Could not create remote directory '{dst_dir}' on "
                f"{_user_host(dst_cfg)}: {r.stderr.strip() or '(no error output)'}"
            )
    else:
        Path(dst_dir).mkdir(parents=True, exist_ok=True)


def selective_copy(
    filename: str,
    src_cfg:  dict,
    src_dir:  str,
    dst_cfg:  dict,
    dst_dir:  str,
    dry_run:  bool,
):
    src_full = f"{src_dir.rstrip('/')}/{filename}"
    dst_full = f"{dst_dir.rstrip('/')}/{filename}"

    if dry_run:
        print(f"      [DRY-RUN] would copy: {filename}")
        return

    print(f"      Copying: {filename}")
    _ensure_dst_dir(dst_cfg, dst_dir)

    src_remote = src_cfg["type"] == "ssh"
    dst_remote = dst_cfg["type"] == "ssh"

    if not src_remote and not dst_remote:
        shutil.copy2(src_full, dst_full)
    elif not src_remote and dst_remote:
        _scp_file(src_full, None, dst_full, dst_cfg)
    elif src_remote and not dst_remote:
        _scp_file(src_full, src_cfg, dst_full, None)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_file = os.path.join(tmp, filename)
            _scp_file(src_full, src_cfg, tmp_file, None)
            _scp_file(tmp_file, None, dst_full, dst_cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Per-directory classification and sync
# ─────────────────────────────────────────────────────────────────────────────

TIER_LABELS = {
    1: "Tier 1 — new dir      → rsync",
    2: "Tier 2 — no conflicts → rsync",
    3: "Tier 3 — conflicts    → rsync + selective copy",
}


def _is_macos_metadata(name: str) -> bool:
    """
    Return True if a bare filename matches a known macOS metadata pattern.
    Used to filter these out of Tier 3 per-file decisions (selective copy /
    MD5 checks) in addition to the rsync --exclude-from list.
    """
    if name == ".DS_Store":
        return True
    if name.startswith("._"):
        return True
    if name in (
        ".VolumeIcon.icns",
        ".com.apple.timemachine.donotpresent",
    ):
        return True
    return False


def _build_global_excludes(
    exclude_media: bool,
    exclude_gamelist: bool,
    include_macos_metadata: bool = False,
) -> list[str]:
    """
    Build the list of rsync --exclude patterns that apply to every directory.

    --exclude-media   : excludes all known media subdirectory names and gamelists
    --exclude-gamelist: excludes gamelist.xml / gamelist.xml.old only

    macOS metadata files (._*, .DS_Store, etc.) are excluded by DEFAULT on
    every run, since they are created automatically by macOS on any volume
    it touches and have no place in a ROM collection. Pass
    include_macos_metadata=True (via --include-macos-metadata) to disable
    this and sync them through if extended attribute data is genuinely needed.
    """
    excludes: list[str] = []

    if not include_macos_metadata:
        excludes.extend(MACOS_METADATA_PATTERNS)

    if exclude_media:
        for d in sorted(MEDIA_DIRS):
            excludes.append(d + "/")       # trailing slash = directory only
        for f in sorted(GAMELIST_FILES):
            excludes.append(f)
    elif exclude_gamelist:
        for f in sorted(GAMELIST_FILES):
            excludes.append(f)
    return excludes


class DirReport:
    """Collects all findings for one directory."""
    __slots__ = ("rel_dir", "tier", "copied", "skipped", "md5_dupes",
                 "source_dupes", "errors")

    def __init__(self, rel_dir: str):
        self.rel_dir:      str                       = rel_dir
        self.tier:         int                       = 0
        self.copied:       int                       = 0
        self.skipped:      int                       = 0
        self.md5_dupes:    list[tuple[str,str,str]]  = []  # (src_name, dst_name, md5)
        self.source_dupes: list[tuple[str,str,str]]  = []  # (stem, kept_name, discarded_name)
        self.errors:       int                       = 0


def classify_and_sync_dir(
    rel_dir:                str,
    src_cfg:                dict,
    src_base:               str,
    dst_cfg:                dict,
    dst_base:               str,
    dry_run:                bool,
    verbose:                bool,
    exclude_media:          bool,
    exclude_gamelist:       bool,
    include_macos_metadata: bool = False,
    preserve_target:        bool = False,
    prefer_extension:       Optional[str] = None,
    no_walk:                bool = False,
) -> DirReport:

    report  = DirReport(rel_dir)
    label   = rel_dir if rel_dir else "(root)"
    src_dir = f"{src_base.rstrip('/')}/{rel_dir}".rstrip("/") if rel_dir else src_base
    dst_dir = f"{dst_base.rstrip('/')}/{rel_dir}".rstrip("/") if rel_dir else dst_base

    # ── Skip this directory entirely if it's a media folder ──────────────────
    # Check the last path component (the directory's own name)
    dir_name = PurePosixPath(rel_dir).name if rel_dir else ""
    if exclude_media and dir_name.lower() in MEDIA_DIRS:
        report.tier = 2   # count as skipped-tier-2 so summary is clean
        print(f"  {label}  →  SKIP (media directory excluded)")
        return report

    # ── Tier 1: target dir absent ────────────────────────────────────────────
    if not dir_exists(dst_cfg, dst_dir):
        report.tier = 1
        print(f"  {label}  →  {TIER_LABELS[1]}")

        # Ensure the destination parent directory exists before rsync runs.
        # rsync can create the final target dir but not missing parents.
        _ensure_dst_parent(dst_cfg, dst_dir, dry_run)

        # Build exclude list for rsync even on Tier 1
        tier1_excludes = _build_global_excludes(
            exclude_media, exclude_gamelist, include_macos_metadata
        )
        try:
            rsync_dir(src_cfg, src_dir, dst_cfg, dst_dir, dry_run, verbose,
                      extra_excludes=tier1_excludes or None,
                      preserve_target=preserve_target,
                      no_recurse=no_walk)
        except Exception as exc:
            print(f"    ERROR: {exc}")
            report.errors += 1
        return report

    # List files on both sides (immediate only — subdirs handled separately)
    dst_files = list_immediate_files(dst_cfg, dst_dir)
    src_files = list_immediate_files(src_cfg, src_dir)

    # Filter macOS metadata out of the working file lists so it never
    # influences stem/MD5 conflict logic, even if a user later re-enables
    # it for rsync via --include-macos-metadata (it would still pass through
    # rsync itself, just not be treated as a ROM candidate here).
    if not include_macos_metadata:
        dst_files = [f for f in dst_files if not _is_macos_metadata(f)]
        src_files = [f for f in src_files if not _is_macos_metadata(f)]

    # ── Source-internal duplicate resolution ──────────────────────────────────
    # The source directory itself can contain the same stem under more than
    # one extension (e.g. diamond.7z AND diamond.zip both present, neither
    # yet on the target). Left unresolved, both would independently pass
    # every later check and both would be copied — recreating on the target
    # exactly the duplication this tool exists to eliminate.
    #
    # Resolution uses --prefer-extension (zip or 7z) as a simple, fast
    # tiebreaker. This does NOT verify the two files contain identical ROM
    # data — only filenames are compared here. If the two archives actually
    # differ in content (different revision, region, etc.), discarding one
    # based on extension alone could lose information. Every case is
    # reported in source_dupes so it can be reviewed; for byte-level
    # certainty before trusting the choice, run romclean.py --deep against
    # the source directory.
    if prefer_extension:
        src_stem_to_files: dict[str, list[str]] = defaultdict(list)
        for name in src_files:
            p = PurePosixPath(name)
            if p.suffix.lower() in CONFLICT_EXTENSIONS:
                src_stem_to_files[p.stem].append(name)

        discard: set[str] = set()
        preferred_ext = "." + prefer_extension.lower().lstrip(".")

        for stem, names in src_stem_to_files.items():
            if len(names) < 2:
                continue
            exts_present = {PurePosixPath(n).suffix.lower() for n in names}
            if len(exts_present) < 2:
                continue   # same extension repeated isn't possible on one
                           # filesystem listing, but guard anyway

            # Pick the preferred extension if present, else keep the first
            # alphabetically for a stable, predictable result.
            keep_name = next(
                (n for n in names if PurePosixPath(n).suffix.lower() == preferred_ext),
                sorted(names)[0],
            )
            for n in names:
                if n != keep_name:
                    discard.add(n)
                    report.source_dupes.append((stem, keep_name, n))
                    if verbose:
                        print(f"    SOURCE DUPLICATE  {stem}: keeping "
                              f"{keep_name}, discarding {n}")

        if discard:
            src_files = [f for f in src_files if f not in discard]
    else:
        # No preference set — still detect source-internal duplicates so the
        # user is warned, but do NOT silently pick one. Both copies will
        # still be synced (matching this tool's "never guess" stance), the
        # warning just makes the situation visible rather than a surprise.
        src_stem_to_files: dict[str, list[str]] = defaultdict(list)
        for name in src_files:
            p = PurePosixPath(name)
            if p.suffix.lower() in CONFLICT_EXTENSIONS:
                src_stem_to_files[p.stem].append(name)

        for stem, names in src_stem_to_files.items():
            exts_present = {PurePosixPath(n).suffix.lower() for n in names}
            if len(exts_present) > 1:
                report.source_dupes.append((stem, "(no preference set)", ", ".join(sorted(names))))
                print(
                    f"    Note: source has multiple formats for '{stem}' "
                    f"({', '.join(sorted(names))}) and no --prefer-extension "
                    f"was set — both will be copied. Use --prefer-extension "
                    f"zip|7z to resolve automatically."
                )

    # ── Check for compressed stem conflicts on target ─────────────────────────
    stem_conflicts = find_stem_conflicts(dst_files)

    # Build a target stem → extensions index up front. This is needed
    # regardless of tier, both for the cross-extension check below and
    # for the Tier 3 per-file logic further down.
    dst_stem_exts: dict[str, set[str]] = defaultdict(set)
    for f in dst_files:
        p = PurePosixPath(f)
        ext = p.suffix.lower()
        if ext in CONFLICT_EXTENSIONS:
            dst_stem_exts[p.stem].add(ext)

    # ── Cross-extension overlap check ─────────────────────────────────────────
    # A directory can have ZERO internal conflicts on the target (e.g. target
    # is all .zip) and still need Tier 3 treatment, if the SOURCE contains a
    # file whose stem already exists on the target under a DIFFERENT
    # extension (e.g. source has zombraid.7z, target already has
    # zombraid.zip). Tier 2's plain rsync only compares by exact filename,
    # so it would treat zombraid.7z as an entirely new file and copy it —
    # creating exactly the duplicate this tool exists to prevent.
    cross_extension_overlap = False
    for name in src_files:
        p = PurePosixPath(name)
        ext = p.suffix.lower()
        if ext not in CONFLICT_EXTENSIONS:
            continue
        existing_exts = dst_stem_exts.get(p.stem)
        if existing_exts and ext not in existing_exts:
            # Target has this stem under a different extension — overlap.
            cross_extension_overlap = True
            break

    # Check whether any raw-format files exist on target — needed to decide
    # if MD5 indexing is worthwhile
    dst_has_raw = any(
        PurePosixPath(f).suffix.lower() in RAW_EXTENSIONS for f in dst_files
    )
    src_has_raw = any(
        PurePosixPath(f).suffix.lower() in RAW_EXTENSIONS for f in src_files
    )
    need_md5 = dst_has_raw and src_has_raw

    global_excludes = _build_global_excludes(
        exclude_media, exclude_gamelist, include_macos_metadata
    )

    if not stem_conflicts and not need_md5 and not cross_extension_overlap:
        # ── Tier 2: nothing to analyse ───────────────────────────────────────
        report.tier = 2
        print(f"  {label}  →  {TIER_LABELS[2]}")
        try:
            rsync_dir(src_cfg, src_dir, dst_cfg, dst_dir, dry_run, verbose,
                      extra_excludes=global_excludes or None,
                      preserve_target=preserve_target,
                      no_recurse=no_walk)
        except Exception as exc:
            print(f"    ERROR: {exc}")
            report.errors += 1
        return report

    # ── Tier 3: stem conflicts or raw files that may need MD5 check ──────────
    report.tier = 3
    print(f"  {label}  →  {TIER_LABELS[3]}")

    if verbose and stem_conflicts:
        for s in sorted(stem_conflicts):
            exts = sorted({
                PurePosixPath(f).suffix.lower()
                for f in dst_files
                if PurePosixPath(f).stem == s
                and PurePosixPath(f).suffix.lower() in CONFLICT_EXTENSIONS
            })
            print(f"    stem conflict: {s}  ({', '.join(exts)})")

    # Build target stem index
    dst_stem_index: dict[str, list[str]] = defaultdict(list)
    for f in dst_files:
        dst_stem_index[PurePosixPath(f).stem].append(f)

    # Build target MD5 index (raw files only, lazy — only if needed)
    dst_md5_index: dict[str, str] = {}   # md5 → filename
    if need_md5:
        dst_md5_index = build_target_md5_index(dst_cfg, dst_dir, dst_files, verbose)

    rsync_excludes: list[str] = list(global_excludes)
    to_copy:        list[str] = []

    for name in src_files:
        stem = PurePosixPath(name).stem
        ext  = PurePosixPath(name).suffix.lower()

        # ── Skip gamelist files if requested ─────────────────────────────────
        if name in GAMELIST_FILES and (exclude_gamelist or exclude_media):
            rsync_excludes.append(name)
            if verbose:
                print(f"    SKIP (gamelist excluded)  {name}")
            continue

        # ── Exact stem match on target ────────────────────────────────────────
        if stem in dst_stem_index:
            existing_exts = {
                PurePosixPath(f).suffix.lower()
                for f in dst_stem_index[stem]
            }
            if existing_exts & CONFLICT_EXTENSIONS:
                # Stem already exists on target in a ROM format — skip
                # regardless of whether target itself has one or multiple formats
                rsync_excludes.append(name)
                report.skipped += 1
                if verbose:
                    existing = ", ".join(dst_stem_index[stem])
                    print(f"    SKIP (stem exists)  {name}  ←→  {existing}")
            # else: stem exists only in non-ROM format (e.g. .xml)
            # rsync handles delta as normal
            continue

        # ── No exact stem match — check MD5 for raw formats ──────────────────
        if ext in RAW_EXTENSIONS and dst_md5_index:
            src_full = f"{src_dir.rstrip('/')}/{name}"
            if verbose:
                print(f"    MD5 checking: {name} …")
            src_digest = compute_md5(src_cfg, src_full)

            if src_digest and src_digest in dst_md5_index:
                # Same bytes exist on target under a different name
                dst_match = dst_md5_index[src_digest]
                report.md5_dupes.append((name, dst_match, src_digest))
                rsync_excludes.append(name)
                report.skipped += 1
                print(
                    f"    MD5 DUPLICATE  {name}\n"
                    f"                ≡  {dst_match}  ({src_digest[:8]}…)"
                )
                continue

        # ── Genuinely new file ────────────────────────────────────────────────
        if ext in CONFLICT_EXTENSIONS:
            # ROM/archive: selective copy (keep out of rsync to avoid
            # introducing a new conflict on re-runs)
            to_copy.append(name)
            rsync_excludes.append(name)
        # else: non-ROM file, let rsync handle it normally

    if verbose:
        print(f"    rsync excludes : {len(rsync_excludes)}")
        print(f"    selective copy : {len(to_copy)}")

    # Step A: rsync the directory excluding conflict / duplicate filenames
    try:
        rsync_dir(
            src_cfg, src_dir, dst_cfg, dst_dir,
            dry_run, verbose,
            extra_excludes=rsync_excludes,
            preserve_target=preserve_target,
            no_recurse=no_walk,
        )
    except Exception as exc:
        print(f"    ERROR during rsync: {exc}")
        report.errors += 1
        return report

    # Step B: selectively copy genuinely new ROM/archive files
    for name in to_copy:
        try:
            selective_copy(name, src_cfg, src_dir, dst_cfg, dst_dir, dry_run)
            report.copied += 1
        except Exception as exc:
            print(f"    ERROR copying {name}: {exc}")
            report.errors += 1

    return report


# ─────────────────────────────────────────────────────────────────────────────
# Main sync orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run_sync(
    config: dict,
    force_interactive: bool = False,
    source_password: Optional[str] = None,
    target_password: Optional[str] = None,
):
    src_cfg = config["source"]
    dst_cfg = config["target"]
    opts    = config["options"]

    if force_interactive or not src_cfg.get("path") or not dst_cfg.get("path"):
        print("\n╔══════════════════════════════════╗")
        print("║        romsync  setup             ║")
        print("╚══════════════════════════════════╝")
        config["source"]  = prompt_endpoint("SOURCE", src_cfg)
        config["target"]  = prompt_endpoint("TARGET", dst_cfg)
        config["options"] = prompt_options(opts)
        save_config(config)
        src_cfg = config["source"]
        dst_cfg = config["target"]
        opts    = config["options"]

    dry_run          = opts.get("dry_run",          False)
    verbose          = opts.get("verbose",          False)
    exclude_media    = opts.get("exclude_media",    False)
    exclude_gamelist = opts.get("exclude_gamelist", False)

    if exclude_media:
        print("  Media directories : EXCLUDED")
        print("  Gamelists         : EXCLUDED (implied by --exclude-media)")
    elif exclude_gamelist:
        print("  Gamelists         : EXCLUDED")

    _require("rsync")
    if src_cfg["type"] == "ssh" or dst_cfg["type"] == "ssh":
        _require("ssh")
        _require("scp")

    # Register passwords in memory only (never written to config / disk).
    # set_password() is a no-op for non-ssh endpoints.
    if source_password:
        set_password(src_cfg, source_password)
    if target_password:
        set_password(dst_cfg, target_password)

    # ── Establish SSH ControlMaster connections (one prompt per host, ever) ──
    # If both source and target are the same SSH host, ensure_control_master
    # is idempotent and will simply reuse the existing master.
    # Hosts with a supplied password and sshpass available will connect
    # without any interactive prompt at all.
    try:
        if src_cfg["type"] == "ssh":
            ensure_control_master(src_cfg, verbose)
        if dst_cfg["type"] == "ssh":
            ensure_control_master(dst_cfg, verbose)
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    try:
        _run_sync_body(
            src_cfg, dst_cfg, dry_run, verbose, exclude_media, exclude_gamelist,
            include_macos_metadata=opts.get("include_macos_metadata", False),
            preserve_target=opts.get("preserve_target", False),
            prefer_extension=opts.get("prefer_extension"),
            no_walk=opts.get("no_walk", False),
        )
    finally:
        # Always close any SSH ControlMaster connections opened for this run,
        # regardless of success, error, or interruption.
        teardown_control_masters(verbose)
        # Always wipe in-memory passwords at the end of the run.
        clear_passwords()


def _run_sync_body(
    src_cfg: dict,
    dst_cfg: dict,
    dry_run: bool,
    verbose: bool,
    exclude_media: bool,
    exclude_gamelist: bool,
    include_macos_metadata: bool = False,
    preserve_target: bool = False,
    prefer_extension: Optional[str] = None,
    no_walk: bool = False,
):
    src_base = src_cfg["path"].rstrip("/")
    dst_base = dst_cfg["path"].rstrip("/")

    if no_walk:
        # --no-walk: only the immediate contents of src_base are processed.
        # Subdirectories (fba, mame2010, media, etc.) are completely ignored
        # — not walked, not listed, not touched in any way. This is the
        # right tool when a source directory mixes the ROMs you want with
        # sibling folders you don't, and excluding them by name isn't
        # practical or they aren't known media folder names.
        print(f"\nSource: {_endpoint_path(src_cfg, src_base)}  "
              f"(--no-walk: top level only, subdirectories ignored)")
        all_dirs = [""]
    else:
        print(f"\nWalking source tree: {_endpoint_path(src_cfg, src_base)} …")
        try:
            all_dirs = walk_source_dirs(src_cfg, src_base)
        except Exception as exc:
            print(f"ERROR: could not walk source tree: {exc}")
            sys.exit(1)

    # Sort by depth (number of slashes) so parent dirs are always processed
    # before their children — ensures the destination parent exists before rsync
    # tries to write into a subdirectory.
    all_dirs.sort(key=lambda d: d.count("/") if d else -1)

    print(f"  {len(all_dirs)} director{'y' if len(all_dirs)==1 else 'ies'} to process.\n")

    tier_counts   = {1: 0, 2: 0, 3: 0}
    total_copied  = 0
    total_skipped = 0
    total_errors  = 0
    all_md5_dupes : list[tuple[str,str,str,str]] = []  # (rel_dir, src, dst, md5)
    all_source_dupes : list[tuple[str,str,str,str]] = []  # (rel_dir, stem, kept, discarded)

    for rel_dir in sorted(all_dirs):
        try:
            report = classify_and_sync_dir(
                rel_dir, src_cfg, src_base, dst_cfg, dst_base,
                dry_run, verbose, exclude_media, exclude_gamelist,
                include_macos_metadata, preserve_target, prefer_extension,
                no_walk,
            )
            tier_counts[report.tier] += 1
            total_copied  += report.copied
            total_skipped += report.skipped
            total_errors  += report.errors
            for src_name, dst_name, digest in report.md5_dupes:
                all_md5_dupes.append((rel_dir or "(root)", src_name, dst_name, digest))
            for stem, kept, discarded in report.source_dupes:
                all_source_dupes.append((rel_dir or "(root)", stem, kept, discarded))
        except Exception as exc:
            label = rel_dir if rel_dir else "(root)"
            print(f"  {label}  →  ERROR: {exc}")
            total_errors += 1

    # ── Final summary ─────────────────────────────────────────────────────────
    tag = "[DRY-RUN] " if dry_run else ""
    print(f"\n{'━'*58}")
    print(f"{tag}Complete.")
    print(f"  Tier 1 dirs (full rsync, target absent)    : {tier_counts[1]}")
    print(f"  Tier 2 dirs (full rsync, no conflicts)     : {tier_counts[2]}")
    print(f"  Tier 3 dirs (selective, conflicts present) : {tier_counts[3]}")
    print(f"  Files selectively copied (new ROMs)        : {total_copied}")
    print(f"  Files skipped (stem / MD5 conflicts)       : {total_skipped}")
    print(f"  Errors                                     : {total_errors}")

    if all_md5_dupes:
        print(f"\n{'━'*58}")
        print(f"MD5 DUPLICATES DETECTED  ({len(all_md5_dupes)} file(s))")
        print("These source files were not copied — identical bytes already")
        print("exist on the target under a different name or extension.")
        print()
        print(f"  {'Directory':<30}  {'Source file':<40}  {'Target match':<40}  MD5")
        print(f"  {'-'*30}  {'-'*40}  {'-'*40}  {'-'*8}")
        for dir_label, src_name, dst_name, digest in sorted(all_md5_dupes):
            print(f"  {dir_label:<30}  {src_name:<40}  {dst_name:<40}  {digest[:8]}…")
        print()
        print("Tip: review and clean up the target copies if needed.")
        print("     The source files remain untouched.")

    if all_source_dupes:
        print(f"\n{'━'*58}")
        print(f"SOURCE-INTERNAL DUPLICATES DETECTED  ({len(all_source_dupes)} stem(s))")
        print("The SOURCE directory contained the same ROM under more than")
        print("one extension. Only filenames were compared — this does NOT")
        print("confirm the files contain identical data.")
        print()
        print(f"  {'Directory':<30}  {'Stem':<20}  {'Kept':<30}  Discarded")
        print(f"  {'-'*30}  {'-'*20}  {'-'*30}  {'-'*20}")
        for dir_label, stem, kept, discarded in sorted(all_source_dupes):
            print(f"  {dir_label:<30}  {stem:<20}  {kept:<30}  {discarded}")
        print()
        print("Tip: for byte-level certainty before trusting this resolution,")
        print("     run romclean.py --deep against the source directory.")


# ─────────────────────────────────────────────────────────────────────────────
# Interactive prompts
# ─────────────────────────────────────────────────────────────────────────────

def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    disp   = "****" if (secret and default) else (default or "")
    suffix = f" [{disp}]" if disp else ""
    try:
        val = (
            getpass.getpass(f"  {label}{suffix}: ")
            if secret
            else input(f"  {label}{suffix}: ").strip()
        )
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default


def _prompt_bool(label: str, default: bool) -> bool:
    yn = _prompt(label + " (y/n)", "y" if default else "n").lower()
    return yn not in ("n", "no", "0", "false")


def prompt_endpoint(label: str, cfg: dict) -> dict:
    print(f"\n── {label} {'─' * (50 - len(label))}")
    kind = _prompt("Type  (local / ssh)", cfg.get("type", "local")).lower()
    while kind not in ("local", "ssh"):
        print("  Please enter 'local' or 'ssh'.")
        kind = _prompt("Type  (local / ssh)", cfg.get("type", "local")).lower()
    cfg["type"] = kind
    cfg["path"] = _prompt(
        "Path (local directory or remote absolute path)", cfg.get("path", "")
    )
    if kind == "ssh":
        cfg["host"]    = _prompt("Hostname or IP", cfg.get("host", ""))
        port_str       = _prompt("Port", str(cfg.get("port", 22)))
        cfg["port"]    = int(port_str) if port_str.isdigit() else 22
        cfg["user"]    = _prompt("Username", cfg.get("user", getpass.getuser()))
        cfg["ssh_key"] = _prompt(
            "SSH private key path (blank = agent/default)",
            cfg.get("ssh_key", ""),
        )
    return cfg


def prompt_options(opts: dict) -> dict:
    print("\n── Options ──────────────────────────────────────────────")
    opts["dry_run"] = _prompt_bool(
        "Dry run (preview, no copies)", opts.get("dry_run", False)
    )
    opts["verbose"] = _prompt_bool(
        "Verbose output", opts.get("verbose", False)
    )
    opts["exclude_media"] = _prompt_bool(
        "Exclude media directories (images/videos/etc)", opts.get("exclude_media", False)
    )
    if not opts["exclude_media"]:
        opts["exclude_gamelist"] = _prompt_bool(
            "Exclude gamelist.xml only", opts.get("exclude_gamelist", False)
        )
    else:
        opts["exclude_gamelist"] = False  # implied by exclude_media
    opts["include_macos_metadata"] = _prompt_bool(
        "Include macOS metadata (._*, .DS_Store) — usually NO",
        opts.get("include_macos_metadata", False)
    )
    opts["preserve_target"] = _prompt_bool(
        "Preserve target files (never overwrite existing filenames, even "
        "if size/date differ — recommended if target has been audited "
        "e.g. via ClrMamePro)",
        opts.get("preserve_target", False)
    )
    resolve_dupes = _prompt_bool(
        "Resolve source-internal duplicates automatically (same ROM as "
        "both .zip and .7z in source)",
        opts.get("prefer_extension") is not None
        if "prefer_extension" in opts else True
    )
    if resolve_dupes:
        pref = _prompt(
            "Prefer extension (zip/7z)",
            (opts.get("prefer_extension") or "7z")
        ).lower().lstrip(".")
        opts["prefer_extension"] = pref if pref in ("zip", "7z") else "7z"
    else:
        opts["prefer_extension"] = None
    opts["no_walk"] = _prompt_bool(
        "Top level only — ignore all subdirectories in the source "
        "(use when the source folder mixes wanted ROMs with sibling "
        "folders you don't want, e.g. fba/mame2010/mame2014)",
        opts.get("no_walk", False)
    )
    return opts


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Universal file sync with ROM-aware stem and MD5 deduplication.\n\n"
            "COMPRESSED formats (.zip .7z .chd …): stem conflict detection only.\n"
            "RAW formats (.bin .a52 .nes .sfc …):  stem check, then MD5 fallback.\n"
            "Media / config files:                 always Tier 1/2, no analysis.\n\n"
            "MD5 duplicates are REPORTED, never silently dropped.\n\n"
            "Pure Python 3 stdlib. Shells out to rsync / ssh / scp."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive first run:
  python3 romsync.py

  # Re-run with saved settings:
  python3 romsync.py --saved

  # Preview with full detail:
  python3 romsync.py --saved --dry-run --verbose

  # Force reconfiguration:
  python3 romsync.py --reconfigure

  # Non-interactive (local → SSH):
  python3 romsync.py \\
      --src-type local --src-path /mnt/nas/roms \\
      --dst-type ssh   --dst-path /home/pi/roms \\
      --dst-host retropie.local --dst-user pi

  # Pull from Batocera:
  python3 romsync.py \\
      --src-type ssh  --src-path /userdata/roms \\
      --src-host batocera.local --src-user root \\
      --dst-type local --dst-path /Volumes/ROMS
        """
    )

    parser.add_argument("--saved",            action="store_true",
                        help="Use saved settings without prompting")
    parser.add_argument("--reconfigure",      action="store_true",
                        help="Force interactive reconfiguration")
    parser.add_argument("--dry-run",          action="store_true",
                        help="Preview only — nothing is written")
    parser.add_argument("--verbose",          action="store_true",
                        help="Show per-file and per-directory detail")
    parser.add_argument("--exclude-media",    action="store_true",
                        help=(
                            "Exclude media directories (images, videos, marquees, "
                            "wheels, boxart, etc.) and gamelists. "
                            "Use when target front-end scrapes its own media."
                        ))
    parser.add_argument("--exclude-gamelist", action="store_true",
                        help=(
                            "Exclude gamelist.xml and gamelist.xml.old only. "
                            "Media files are still synced. "
                            "Implied by --exclude-media."
                        ))
    parser.add_argument("--include-macos-metadata", action="store_true",
                        help=(
                            "Include macOS metadata files (._*, .DS_Store, "
                            "Spotlight/Trash folders, etc.). "
                            "These are EXCLUDED BY DEFAULT since macOS creates "
                            "them automatically on any volume it touches and "
                            "they have no place in a ROM collection. "
                            "Use this only if extended attribute data is "
                            "genuinely needed on the target."
                        ))
    parser.add_argument("--preserve-target", action="store_true",
                        help=(
                            "Never overwrite a file that already exists on the "
                            "target by exact filename, even if its size or "
                            "modification time differs from the source. "
                            "Adds rsync's --ignore-existing flag. "
                            "Recommended when the target has been audited or "
                            "rebuilt independently (e.g. via ClrMamePro), "
                            "where a differing source copy is not necessarily "
                            "an improvement. Genuinely new filenames are "
                            "still copied normally."
                        ))
    parser.add_argument("--prefer-extension", choices=["zip", "7z"], default=None,
                        help=(
                            "Resolve source-internal duplicates (the same ROM "
                            "present in the source as both .zip and .7z) by "
                            "keeping the given extension and discarding the "
                            "other. DEFAULT: 7z. Resolution is by FILENAME "
                            "only — it does not verify the two archives "
                            "contain identical data. Every resolved case is "
                            "reported at the end of the run. For byte-level "
                            "certainty, run romclean.py --deep against the "
                            "source directory."
                        ))
    parser.add_argument("--no-prefer-extension", action="store_true",
                        help=(
                            "Disable automatic resolution of source-internal "
                            "duplicates. Both formats will be copied, and "
                            "each occurrence is reported as a warning rather "
                            "than resolved automatically. Overrides the "
                            "default --prefer-extension=7z behaviour."
                        ))
    parser.add_argument("--no-walk", action="store_true",
                        help=(
                            "Only process files directly inside the source "
                            "directory. Subdirectories are completely "
                            "ignored — not walked, not listed, not touched. "
                            "Use this when a source folder mixes the ROMs "
                            "you want with sibling folders you don't "
                            "(e.g. a RetroPie 'arcade' folder containing "
                            "fba/, mame2010/, mame2014/ alongside the loose "
                            "ROMs you actually want to sync)."
                        ))

    grp_src = parser.add_argument_group("source overrides")
    grp_src.add_argument("--src-type", choices=["local", "ssh"])
    grp_src.add_argument("--src-path", metavar="PATH")
    grp_src.add_argument("--src-host", metavar="HOST")
    grp_src.add_argument("--src-port", metavar="PORT", type=int)
    grp_src.add_argument("--src-user", metavar="USER")
    grp_src.add_argument("--src-key",  metavar="KEY")
    grp_src.add_argument("--source-password", metavar="PASSWORD",
                        help="SSH password for the source host. Visible in "
                             "shell history and 'ps' — prefer "
                             "--ask-source-password for shared systems. "
                             "Requires sshpass to be installed; falls back "
                             "to interactive prompting otherwise.")
    grp_src.add_argument("--ask-source-password", action="store_true",
                        help="Prompt once for the source host's SSH "
                             "password (not echoed, not stored). "
                             "Requires sshpass to apply it automatically; "
                             "falls back to interactive prompting otherwise.")

    grp_dst = parser.add_argument_group("target overrides")
    grp_dst.add_argument("--dst-type", choices=["local", "ssh"])
    grp_dst.add_argument("--dst-path", metavar="PATH")
    grp_dst.add_argument("--dst-host", metavar="HOST")
    grp_dst.add_argument("--dst-port", metavar="PORT", type=int)
    grp_dst.add_argument("--dst-user", metavar="USER")
    grp_dst.add_argument("--dst-key",  metavar="KEY")
    grp_dst.add_argument("--target-password", metavar="PASSWORD",
                        help="SSH password for the target host. Visible in "
                             "shell history and 'ps' — prefer "
                             "--ask-target-password for shared systems. "
                             "Requires sshpass to be installed; falls back "
                             "to interactive prompting otherwise.")
    grp_dst.add_argument("--ask-target-password", action="store_true",
                        help="Prompt once for the target host's SSH "
                             "password (not echoed, not stored). "
                             "Requires sshpass to apply it automatically; "
                             "falls back to interactive prompting otherwise.")

    args = parser.parse_args()
    config = load_config()

    cli_src = {k: v for k, v in {
        "type":    args.src_type, "path": args.src_path,
        "host":    args.src_host, "port": args.src_port,
        "user":    args.src_user, "ssh_key": args.src_key,
    }.items() if v is not None}

    cli_dst = {k: v for k, v in {
        "type":    args.dst_type, "path": args.dst_path,
        "host":    args.dst_host, "port": args.dst_port,
        "user":    args.dst_user, "ssh_key": args.dst_key,
    }.items() if v is not None}

    config["source"].update(cli_src)
    config["target"].update(cli_dst)

    if args.dry_run:                config["options"]["dry_run"]                = True
    if args.verbose:                config["options"]["verbose"]                = True
    if args.exclude_media:          config["options"]["exclude_media"]          = True
    if args.exclude_gamelist:       config["options"]["exclude_gamelist"]       = True
    if args.include_macos_metadata: config["options"]["include_macos_metadata"] = True
    if args.preserve_target:        config["options"]["preserve_target"]        = True
    if args.prefer_extension:       config["options"]["prefer_extension"]       = args.prefer_extension
    if args.no_prefer_extension:    config["options"]["prefer_extension"]       = None
    if args.no_walk:                config["options"]["no_walk"]                = True

    has_cli = bool(cli_src.get("path") and cli_dst.get("path"))

    if args.reconfigure:
        force = True
    elif args.saved or has_cli:
        force = False
    elif not config["source"].get("path") or not config["target"].get("path"):
        force = True
    else:
        force = False

    # ── Resolve passwords (command-line flag takes priority over prompt) ────
    # Both are entirely in-memory for this run; neither is ever written to
    # ~/.romsync_config.json.
    source_password = args.source_password
    if not source_password and args.ask_source_password:
        source_password = getpass.getpass("Source host SSH password: ")

    target_password = args.target_password
    if not target_password and args.ask_target_password:
        target_password = getpass.getpass("Target host SSH password: ")

    if (args.source_password or args.target_password) and not find_sshpass():
        print(
            "Note: a password was supplied via --source-password / "
            "--target-password but 'sshpass' is not installed, so it "
            "can't be applied automatically. You will be prompted "
            "normally instead.\n"
            "  macOS : brew install sshpass\n"
            "  Linux : sudo apt install sshpass\n"
        )

    run_sync(
        config,
        force_interactive=force,
        source_password=source_password,
        target_password=target_password,
    )


if __name__ == "__main__":
    main()
