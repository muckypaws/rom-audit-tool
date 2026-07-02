# romsync

A universal file sync tool with ROM-aware duplicate detection.  
Designed for consolidating retro gaming ROM collections across local drives,
NAS volumes, and remote systems (Batocera, Recalbox, RetroPie, etc.).

---

## Contents

- [Purpose](#purpose)
- [Requirements](#requirements)
- [Installation](#installation)
- [How It Works](#how-it-works)
- [Extension Classification](#extension-classification)
- [The Tiered Strategy](#the-tiered-strategy)
- [Duplicate Detection](#duplicate-detection)
- [Media and Gamelist Handling](#media-and-gamelist-handling)
- [Configuration](#configuration)
- [Usage](#usage)
- [Command Line Reference](#command-line-reference)
- [Command Line Examples](#command-line-examples)
- [Output and Reporting](#output-and-reporting)
- [Known Limitations](#known-limitations)
- [Platform Notes](#platform-notes)
- [Extending the Extension Lists](#extending-the-extension-lists)

---

## Purpose

Standard sync tools like `rsync` are unaware of ROM file formats. A collection
being consolidated across multiple sources may contain the same ROM in different
formats — for example `pacman.zip` and `pacman.7z`, or `Centipede (1982) (Atari).bin`
and `Centipede.a52`. A naive sync would copy both, leaving the target with
duplicates that waste space and can confuse front-ends.

`romsync` addresses this by:

- Detecting files that share the same **stem** (filename without extension) and
  skipping the source file if the stem already exists on the target in any known
  ROM format.
- For **raw (uncompressed) ROM formats**, falling back to **MD5 comparison** when
  the stems don't match — catching cases where the same ROM bytes exist under
  different naming conventions (e.g. No-Intro long names vs bare names).
- Using `rsync` for all actual file transfer, meaning delta transfers, checksums,
  and permissions are handled efficiently.
- Walking the **full directory tree** and classifying each directory independently,
  so deeply nested media directories are handled correctly alongside ROM directories.

---

## Requirements

### Python

Python 3.6 or later. No third-party packages required — pure stdlib only.

| Platform | Python source |
|---|---|
| macOS | Homebrew: `brew install python3` or system Python 3 |
| Ubuntu / Debian | `sudo apt install python3` |
| Batocera | Pre-installed at `/usr/bin/python3` |
| Recalbox | Pre-installed at `/usr/bin/python3` |
| RetroPie | Pre-installed via Raspberry Pi OS |
| Windows | [python.org](https://www.python.org/downloads/) |

### System tools

The following must be available on `PATH`:

| Tool | Purpose | Notes |
|---|---|---|
| `rsync` | All file transfer | Required always |
| `ssh` | Remote listing and MD5 | Required for SSH endpoints |
| `scp` | Selective remote copy | Required for SSH endpoints |

On macOS, all three are included with the system.  
On Linux, install with `sudo apt install rsync openssh-client`.  
On Windows, enable OpenSSH via Settings → Optional Features, and install rsync
via WSL or Git Bash.  
On Batocera, `rsync` and `ssh` are pre-installed.

---

## Installation

No installation is required. Copy `romsync.py` to any convenient location and
run it directly:

```bash
python3 romsync.py
```

To make it executable on macOS / Linux:

```bash
chmod +x romsync.py
./romsync.py
```

---

## How It Works

`romsync` walks the **full source directory tree** in a single pass. Each
directory in the tree is processed independently using a tiered strategy
(see below). This means a structure like:

```
roms/
├── arcade/
├── snes/
└── ports/
    └── media/
        ├── images/
        └── videos/
```

…has every directory assessed on its own merits. `ports/media/images/` gets
its own tier classification rather than being bundled blindly into `ports/`.

For remote endpoints, the tree walk is a **single SSH `find` call** — the full
directory list is retrieved in one round-trip rather than one per directory.

---

## Extension Classification

Extensions are divided into two categories that receive different treatment:

### Compressed extensions

`.zip` `.7z` `.gz` `.bz2` `.xz` `.rar` `.tar` `.chd` `.rvz` `.wbfs` `.wad`
`.nsp` `.xci` `.pbp` `.pkg` `.cdi` `.nrg` `.mdf` `.mds` `.ccd`

**Stem conflict detection only.** MD5 comparison is never attempted across
different compression formats because the same ROM compressed with different
tools will never produce the same hash.

### Raw / uncompressed extensions

`.bin` `.rom` `.a26` `.a52` `.a78` `.nes` `.fds` `.sfc` `.smc` `.n64` `.z64`
`.v64` `.gb` `.gbc` `.gba` `.nro` `.nds` `.md` `.smd` `.gen` `.32x` `.gg`
`.sms` `.pce` `.ws` `.wsc` `.lnx` `.ngp` `.ngc` `.ngpc` `.iso` `.img` `.cue`
`.gdi` `.sub` `.3ds` `.cia` `.nca`

**Stem conflict detection first, then MD5 fallback.** Because these are flat
binary images, the same ROM may exist as both `.bin` and `.a52` (or `.bin` and
`.a78`, etc.) with identical byte content. If no exact stem match is found, the
source file's MD5 is compared against all raw-format files in the target
directory.

### Everything else

Media files (`.mp4`, `.png`, `.jpg`, `.xml`, etc.), config files, and anything
not in the above lists always pass through Tier 1 or Tier 2 — straight `rsync`,
no analysis overhead.

> **Note:** Extension lists can be extended by editing the `COMPRESSED_EXTENSIONS`
> and `RAW_EXTENSIONS` sets at the top of `romsync.py`. See
> [Extending the Extension Lists](#extending-the-extension-lists).

---

## The Tiered Strategy

Each directory in the source tree is independently classified into one of three
tiers:

### Tier 1 — Target directory does not exist

The entire source directory is rsynced to the target in a single call. No file
analysis is performed. This is the fastest possible path and covers first-time
syncs and new platform directories being added to a collection.

### Tier 2 — Target directory exists, no stem conflicts

The target directory exists but contains no ROM files with conflicting stems.
A full `rsync` is performed — rsync's own delta algorithm handles files already
present. No per-file analysis is needed.

### Tier 3 — Target directory has conflicting stems or raw ROM files

The target directory either has multiple formats for the same stem (e.g.
`zaxxon.7z` already there, `zaxxon.zip` incoming) or contains raw ROM files
that require MD5 checking. In this case:

1. Per-file analysis is performed (see [Duplicate Detection](#duplicate-detection))
2. `rsync` runs with an exclude file covering conflicted / duplicate filenames
3. Genuinely new ROM files (stem not present on target in any format) are
   selectively copied via `scp`

The exclude list is written to a **temporary file** and passed to rsync via
`--exclude-from` rather than as individual `--exclude` arguments. This avoids
OS command-line length limits (`ARG_MAX`) which can be as low as 128 KB on
embedded Linux kernels (Batocera, RetroPie) and would be exceeded when
processing directories with thousands of duplicates.

---

## Duplicate Detection

For each source file in a Tier 3 directory, the following logic applies:

```
Does the stem exist on the target in any ROM format?
  YES → exclude from rsync, report as skipped
  NO  → is this a raw format file AND does the target have raw files?
    YES → compute MD5 of source file
          does the MD5 match any target file?
            YES → exclude from rsync, report as MD5 duplicate
            NO  → selectively copy (genuinely new ROM)
    NO  → selectively copy (genuinely new ROM)
```

Non-ROM files (media, XML, etc.) that don't match any of the above are left
for rsync to handle normally.

### MD5 matching caveats

MD5 matching correctly handles cases like:
- `Centipede (1982) (Atari).bin` (No-Intro naming) ≡ `Centipede.a52` (bare name)

It **cannot** handle:
- **Merged ROM sets** — a MAME merged set bundles parent and clone ROMs together;
  the bytes differ from a split set even for the same game.
- **Regional variants** — `Pac-Man (USA).bin` and `Pac-Man (Europe).bin` are
  legitimately different files; their MD5s will not match.
- **Hacked / patched ROMs** — any modification changes the MD5.

These cases are left for manual review. The summary report gives you visibility
of what was copied and what was skipped.

---

## Media and Gamelist Handling

Front-ends (Batocera, RetroPie, Recalbox, ES-DE) store scraped artwork, video
snaps, and game metadata in a `gamelist.xml` file. The paths inside this file
are platform-specific and may not be compatible across front-ends. Two flags
address this:

### `--exclude-media`

Excludes all known media subdirectories and gamelist files. Use this when:
- The target front-end will scrape its own media
- You are syncing between incompatible front-ends
- You want to sync ROMs only without any metadata

Excluded directory names: `media`, `images`, `videos`, `manuals`, `marquees`,
`wheels`, `boxart`, `screenshots`, `thumbnails`, `fanart`, `logos`, `covers`,
`mix`, `maps`

Excluded files: `gamelist.xml`, `gamelist.xml.old`

### `--exclude-gamelist`

Excludes `gamelist.xml` and `gamelist.xml.old` only. Media files (artwork,
videos) are still synced. Use this when:
- The media file paths are compatible between source and target front-ends
- You want the artwork but the target will manage its own gamelist

> `--exclude-media` implies `--exclude-gamelist`. You do not need to specify
> both.

> **Note:** XML path rewriting (transforming Batocera-style paths to RetroPie-
> style paths) is not currently implemented. If you need this, run a scrape on
> the target after syncing ROMs.

---

## Configuration

Settings are saved to `~/.romsync_config.json` after the first interactive run.
Passwords are **never** saved. SSH authentication uses your agent or default key
files; a key path can be specified explicitly.

The config file stores:

```json
{
  "source": {
    "type": "local",
    "path": "/Users/jason/Downloads/Incoming/roms",
    "host": "",
    "port": 22,
    "user": "",
    "ssh_key": ""
  },
  "target": {
    "type": "ssh",
    "path": "/userdata/roms",
    "host": "batocera.local",
    "port": 22,
    "user": "root",
    "ssh_key": "/Users/jason/.ssh/id_rsa"
  },
  "options": {
    "dry_run": false,
    "verbose": false,
    "exclude_media": false,
    "exclude_gamelist": false
  }
}
```

To reset configuration, delete `~/.romsync_config.json` or run with
`--reconfigure`.

---

## Usage

### First run — interactive setup

```bash
python3 romsync.py
```

You will be prompted for source and target details, which are saved for future
runs.

### Re-run with saved settings

```bash
python3 romsync.py --saved
```

### Preview before syncing

Always recommended before the first real sync of a large collection:

```bash
python3 romsync.py --saved --dry-run --verbose
```

### Force reconfiguration

```bash
python3 romsync.py --reconfigure
```

---

## Command Line Reference

```
python3 romsync.py [options] [source overrides] [target overrides]
```

### Behaviour flags

| Flag | Description |
|---|---|
| `--saved` | Use saved settings without prompting |
| `--reconfigure` | Force interactive reconfiguration |
| `--dry-run` | Preview only — nothing is written or copied |
| `--verbose` | Show per-file and per-directory detail |
| `--exclude-media` | Exclude media directories and gamelists |
| `--exclude-gamelist` | Exclude gamelist.xml only; media still syncs |

### Source overrides

| Flag | Description |
|---|---|
| `--src-type local\|ssh` | Source type |
| `--src-path PATH` | Source directory path |
| `--src-host HOST` | SSH hostname or IP |
| `--src-port PORT` | SSH port (default: 22) |
| `--src-user USER` | SSH username |
| `--src-key KEY` | Path to SSH private key |

### Target overrides

| Flag | Description |
|---|---|
| `--dst-type local\|ssh` | Target type |
| `--dst-path PATH` | Target directory path |
| `--dst-host HOST` | SSH hostname or IP |
| `--dst-port PORT` | SSH port (default: 22) |
| `--dst-user USER` | SSH username |
| `--dst-key KEY` | Path to SSH private key |

---

## Command Line Examples

### Local to local (first sync of a new platform)

```bash
python3 romsync.py \
  --src-type local \
  --src-path "/Users/jason/Downloads/Incoming/Atari 5200 ROMs" \
  --dst-type local \
  --dst-path /Volumes/RetroGaming/roms/atari5200
```

### Dry run with verbose output before committing

```bash
python3 romsync.py \
  --src-type local \
  --src-path /Volumes/NAS/roms \
  --dst-type local \
  --dst-path /Volumes/RetroGaming/roms \
  --dry-run --verbose
```

### Local to Batocera over SSH

```bash
python3 romsync.py \
  --src-type local \
  --src-path /Volumes/RetroGaming/roms \
  --dst-type ssh \
  --dst-path /userdata/roms \
  --dst-host batocera.local \
  --dst-user root \
  --dst-key ~/.ssh/id_rsa
```

### Pull from Batocera to local (consolidate)

```bash
python3 romsync.py \
  --src-type ssh \
  --src-path /userdata/roms \
  --src-host batocera.local \
  --src-user root \
  --dst-type local \
  --dst-path /Volumes/RetroGaming/roms
```

### Sync to RetroPie, excluding gamelists (re-scrape on target)

```bash
python3 romsync.py \
  --src-type local \
  --src-path /Volumes/RetroGaming/roms \
  --dst-type ssh \
  --dst-path /home/pi/RetroPie/roms \
  --dst-host retropie.local \
  --dst-user pi \
  --exclude-gamelist
```

### Sync ROMs only to Recalbox, no media at all

```bash
python3 romsync.py \
  --src-type local \
  --src-path /Volumes/RetroGaming/roms \
  --dst-type ssh \
  --dst-path /recalbox/share/roms \
  --dst-host recalbox.local \
  --dst-user root \
  --exclude-media
```

### Re-run saved settings with verbose output

```bash
python3 romsync.py --saved --verbose
```

### Re-run saved settings in dry-run mode (safe preview)

```bash
python3 romsync.py --saved --dry-run
```

---

## Output and Reporting

### Per-directory output

Each directory is shown with its tier classification:

```
  arcade          →  Tier 1 — new dir      → rsync
  mame            →  Tier 3 — conflicts    → rsync + selective copy
    stem conflict: zaxxon  (.7z, .zip)
    rsync excludes : 142
    selective copy : 18
  snes            →  Tier 2 — no conflicts → rsync
  ports/media     →  SKIP (media directory excluded)
```

### Final summary

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Complete.
  Tier 1 dirs (full rsync, target absent)    : 4
  Tier 2 dirs (full rsync, no conflicts)     : 12
  Tier 3 dirs (selective, conflicts present) : 3
  Files selectively copied (new ROMs)        : 47
  Files skipped (stem / MD5 conflicts)       : 312
  Errors                                     : 0
```

### MD5 duplicate report

Printed at the end of the run when cross-format duplicates are detected:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MD5 DUPLICATES DETECTED  (3 file(s))
These source files were not copied — identical bytes already
exist on the target under a different name or extension.

  Directory       Source file                        Target match      MD5
  ──────────────  ─────────────────────────────────  ────────────────  ────────
  atari5200       Centipede (1982) (Atari).bin       Centipede.a52     4a2f91c0…
  atari5200       Defender (1982) (Atari).bin        Defender.a52      9b3e72a1…
  atari7800       Dig Dug (1987) (Atari).bin         Dig Dug.a78       c4f810d3…

Tip: review and clean up the target copies if needed.
     The source files remain untouched.
```

---

## Known Limitations

### Merged ROM sets

MAME merged sets bundle parent and clone ROMs into a single archive. The bytes
differ from a split or non-merged set even for the same game title. MD5
comparison will not detect these as duplicates — they will be copied. This is
correct behaviour; the sets are genuinely different.

### Regional and revision variants

`Pac-Man (USA).bin` and `Pac-Man (Europe).bin` are different files with
different MD5s. Both will be copied if both exist in the source. Review the
MD5 duplicate report to identify cases where you may want to keep only one
region.

### Gamelist XML path rewriting

`gamelist.xml` files contain paths to media assets that are formatted
differently across front-ends:

| Front-end | Example path |
|---|---|
| Batocera / ES-DE | `./media/images/pacman.png` |
| RetroPie | `/home/pi/RetroPie/roms/arcade/media/images/pacman.png` |
| Recalbox | `/recalbox/share/roms/arcade/media/images/pacman.png` |

Cross-platform XML path rewriting is not implemented. Use `--exclude-gamelist`
when syncing between incompatible front-ends and re-scrape on the target, or
use `--exclude-media` to skip both media and gamelists entirely.

### Remote to remote (SSH → SSH)

When both source and target are remote SSH endpoints, files are staged through
a local temporary directory. This requires sufficient local disk space for the
largest individual file being transferred. For large disc images (`.chd`,
`.iso`) this may require several gigabytes of temporary space.

### Windows path separators

On Windows, local paths use backslash separators internally but the script
normalises these for rsync compatibility. If you encounter path issues on
Windows, use forward slashes in path arguments or wrap paths in quotes.

### SSH host key verification

On first connection to a new host, `StrictHostKeyChecking=accept-new` is used —
the host key is accepted and saved automatically. If a host key changes
(e.g. after an OS reinstall on the target), you may need to remove the old key
from `~/.ssh/known_hosts` manually.

---

## Platform Notes

### Batocera

- SSH is enabled by default. Default credentials: `root` / `linux`.
- SSH key authentication is strongly recommended for unattended syncs.
- ROM path: `/userdata/roms`
- Media path: `/userdata/roms/<system>/media`
- Python 3 is at `/usr/bin/python3`

### RetroPie

- SSH must be enabled via `raspi-config` or by placing an empty file named
  `ssh` on the boot partition.
- Default credentials: `pi` / `raspberry` (change this).
- ROM path: `/home/pi/RetroPie/roms`
- Python 3 is available via `sudo apt install python3`

### Recalbox

- SSH is enabled on port 22. Default credentials: `root` / `recalboxroot`.
- ROM path: `/recalbox/share/roms`
- Python 3 availability varies by version; check with `python3 --version`.

### macOS

- `rsync` shipped with macOS is an older version (2.6.x) that may not support
  all flags. Install a modern version via Homebrew: `brew install rsync`.
- Paths containing spaces must be quoted on the command line.

---

## Extending the Extension Lists

The extension sets are defined at the top of `romsync.py` and are easy to
extend. Add extensions in lowercase with the leading dot:

```python
COMPRESSED_EXTENSIONS: frozenset[str] = frozenset({
    ".zip", ".7z", ...
    ".squashfs",   # add new compressed formats here
})

RAW_EXTENSIONS: frozenset[str] = frozenset({
    ".bin", ".rom", ...
    ".j64",   # add new raw formats here (Jaguar)
    ".jag",
})
```

Similarly, media directory names can be added to `MEDIA_DIRS` and gamelist
filenames to `GAMELIST_FILES` if your front-end uses non-standard names.
