# romsync — README

A ROM-aware file sync tool for consolidating retro gaming collections across local drives, NAS volumes, and remote systems (Batocera, Recalbox, RetroPie, and similar).

This document is written for someone picking up the tool for the first time. If you just want command examples, jump to [Quick Start](#quick-start).

---

## Contents

- [What This Tool Does](#what-this-tool-does)
- [What Makes It Different From Plain rsync](#what-makes-it-different-from-plain-rsync)
- [Installing Dependencies](#installing-dependencies)
- [Quick Start](#quick-start)
- [How a Sync Decision Is Made](#how-a-sync-decision-is-made)
- [Command Line Options Explained](#command-line-options-explained)
- [Password Handling and Remote Connections](#password-handling-and-remote-connections)
- [Worked Examples](#worked-examples)
- [Understanding the Output](#understanding-the-output)
- [Safety Features](#safety-features)
- [Companion Tool: romclean.py](#companion-tool-romcleanpy)
- [Troubleshooting](#troubleshooting)
- [Known Limitations](#known-limitations)
- [Glossary](#glossary)

---

## What This Tool Does

If you've been collecting ROMs for any length of time, you've probably ended up with the same game in more than one place, possibly in more than one file format — `pacman.zip` here, `pacman.7z` there, `Centipede (1982) (Atari).bin` in one folder and `Centipede.a52` in another. Copying everything blindly from one drive or one retro console to another, using a normal file copy or `rsync`, will happily duplicate all of that mess onto your target.

`romsync` understands ROM files well enough to avoid that. When syncing a source ROM collection to a target, it:

- Skips a file if a ROM with the **same name but a different file extension** already exists on the target (e.g. target has `pacman.7z`, so incoming `pacman.zip` is skipped).
- For raw, uncompressed ROM formats (like `.bin`, `.a52`, `.nes`), compares **file content** (MD5 checksum) when filenames don't match exactly, catching cases where the same ROM exists under two completely different naming styles.
- Detects when the **source itself** contains the same ROM twice under different extensions, and resolves it automatically (preferring `.7z` by default) rather than copying both.
- Lets you protect a target collection that has already been verified or rebuilt (e.g. with ClrMamePro) from being silently overwritten by a possibly-different source copy.
- Works whether your source and target are local folders, external drives, network shares, or remote systems reached over SSH — including Batocera, Recalbox, and RetroPie boxes.
- Remembers your settings between runs, so re-syncing as you keep consolidating your collection is a single short command.

## What Makes It Different From Plain rsync

`rsync` is excellent at syncing files, but it has no concept of "this is the same ROM under a different name." It compares files by exact filename only. `romsync` adds a ROM-aware decision layer on top of `rsync` — it works out which files are genuinely new, which are duplicates in disguise, and which need closer inspection, and only then hands the actual file transfer over to `rsync` itself. You get the speed and reliability of `rsync`'s transfer engine, with ROM-specific intelligence on top.

---

## Installing Dependencies

`romsync` is written in pure Python 3 with no required third-party Python packages. It works by calling out to standard system tools that are already present on virtually every platform: `rsync`, `ssh`, and `scp`.

### macOS

Python 3 and SSH tools are included with macOS. You will likely want two optional extras:

```bash
brew install rsync       # macOS ships an old rsync (2.6.x); a modern one is recommended
brew install sshpass     # enables single-password-entry remote syncs (see below)
```

If you don't have Homebrew installed, get it from [brew.sh](https://brew.sh) first.

### Linux (Ubuntu, Debian, Raspberry Pi OS, etc.)

```bash
sudo apt update
sudo apt install python3 rsync openssh-client sshpass
```

Most desktop Linux distributions already have Python 3, rsync, and OpenSSH installed by default — `sshpass` is the one most likely to be missing.

### Windows

1. Install Python 3 from [python.org](https://www.python.org/downloads/) (tick "Add Python to PATH" during setup).
2. Enable OpenSSH Client: **Settings → Apps → Optional Features → Add a feature → OpenSSH Client**.
3. For `rsync` on Windows, the simplest route is installing it via [Git for Windows](https://gitforwindows.org/) (which bundles a usable `rsync`) or via WSL (Windows Subsystem for Linux), inside which you can follow the Linux instructions above.
4. `sshpass` is not readily available on native Windows; if you need it, running `romsync` inside WSL and following the Linux install steps is the easiest path.

### Batocera / Recalbox / RetroPie (as a target, not where you run the script)

These are the **destination systems** the script connects to over SSH — you don't need to install anything on them. `romsync` runs on your computer (Mac, Linux, or Windows) and connects out to these devices. They already have the SSH server and basic tools needed to be a sync target.

### Verifying your setup

After installing, check that everything is on your system PATH:

```bash
python3 --version
rsync --version
ssh -V
scp -V
sshpass -V        # optional, but recommended for remote syncs
```

If any of these report "command not found," revisit the install steps above for your platform.

---

## Quick Start

The very first time you run `romsync`, it will interactively ask you for your source, your target, and your preferred options, then remember them:

```bash
python3 romsync.py
```

After that first run, you can repeat the exact same sync with:

```bash
python3 romsync.py --saved
```

**Always do a practice run first** with `--dry-run` before letting it actually copy or skip anything for real:

```bash
python3 romsync.py --saved --dry-run --verbose
```

This shows you exactly what it intends to do without touching a single file.

---

## How a Sync Decision Is Made

For every folder in your source collection, `romsync` works out the fastest, safest way to handle it. There are three tiers:

**Tier 1 — the folder doesn't exist on the target yet.** Nothing to compare against, so the whole folder is copied across in one efficient `rsync` operation.

**Tier 2 — the folder exists on the target, and there's no overlap.** No filename clashes, no duplicate ROMs under different names — `rsync` is used directly, and it efficiently figures out which files (if any) have changed.

**Tier 3 — there's an actual decision to make.** This happens when:
- The target already has the same ROM under a different extension (e.g. you're sending `mario.zip` but the target already has `mario.7z`)
- The source has raw ROM files that might be byte-identical to differently-named files already on the target
- The source itself contains the same ROM twice under different extensions

In Tier 3, every affected file is checked individually. Genuine duplicates are skipped (and reported to you); only files that are truly new to the target are copied.

You don't need to do anything to trigger this — `romsync` works it out automatically, folder by folder, as it walks through your collection.

---

## Command Line Options Explained

### The basics

| Option | What it does |
|---|---|
| (no options) | First run: walks you through setup interactively. Later runs: uses your last saved settings. |
| `--saved` | Skip the interactive setup and use whatever you configured last time. |
| `--reconfigure` | Force the interactive setup again, even if you have saved settings. |
| `--dry-run` | Show what *would* happen without actually copying or skipping anything for real. Always try this first. |
| `--verbose` | Show detailed information about every decision being made — recommended alongside `--dry-run` when checking a new sync. |

### Telling it where things are

| Option | What it does |
|---|---|
| `--src-type local` or `ssh` | Is your source a folder on this computer, or a remote system over SSH? |
| `--src-path PATH` | The folder path on the source. |
| `--src-host HOST` | If source is `ssh`, the hostname or IP address (e.g. `batocera.local`). |
| `--src-user USER` | The username to log in as on the source (e.g. `root`). |
| `--src-port PORT` | SSH port if not the default of 22. |
| `--src-key KEY` | Path to an SSH private key file, if you use key-based login instead of a password. |

The same options exist with `--dst-` instead of `--src-` for the target.

### Handling passwords for remote systems

| Option | What it does |
|---|---|
| `--ask-source-password` | Prompts you once for the source's SSH password (hidden as you type, never saved anywhere). |
| `--ask-target-password` | Same, for the target. |
| `--source-password PASSWORD` | Supplies the password directly on the command line. Convenient, but visible in your shell history — only use this on a computer only you have access to. |
| `--target-password PASSWORD` | Same, for the target. |

See [Password Handling](#password-handling-and-remote-connections) below for the full picture.

### Controlling what gets excluded

| Option | What it does |
|---|---|
| `--exclude-media` | Skip artwork, video snaps, manuals, and gamelist files entirely. Use this if your target front-end will scrape its own media, or if source and target use incompatible front-ends. |
| `--exclude-gamelist` | Skip only the `gamelist.xml` file, but still sync media (artwork/videos). Implied automatically by `--exclude-media`. |
| `--include-macos-metadata` | Normally, hidden macOS files (`._filename`, `.DS_Store`, etc.) are always excluded automatically, since they're junk created by macOS itself and have no place in a ROM collection. This flag turns that protection off — you almost never want to use it. |

### Protecting your target collection

| Option | What it does |
|---|---|
| `--preserve-target` | Never overwrite a file that's already on the target, even if the source version has a different size or date. Recommended if your target has been verified or rebuilt with a tool like ClrMamePro — a "different" source file isn't necessarily a *better* one. |
| `--prefer-extension zip` or `7z` | When the **source itself** has the same ROM under two extensions, automatically keep one and discard the other. **Defaults to `7z`** if you don't set this. |
| `--no-prefer-extension` | Turns off the automatic resolution above — both copies will be synced, and you'll just get a warning about each case instead. |

---

## Password Handling and Remote Connections

If your source or target is a remote system reached over SSH, you have three ways to deal with the password prompt:

### Option 1 — SSH keys (best long-term option)

Set up a key pair so no password is ever needed:

```bash
ssh-keygen -t ed25519                       # if you don't already have a key
ssh-copy-id root@batocera.local              # copies your public key to the target
```

After this, connecting to that system never asks for a password again, from `romsync` or anything else.

### Option 2 — Type the password once per run

Use `--ask-source-password` and/or `--ask-target-password`. You'll be asked once, the password is held only in memory for the duration of that run, and is never written to disk.

**Important caveat:** Batocera and Recalbox run a lightweight SSH server called **Dropbear**, which doesn't support the connection-reuse trick (`ControlMaster`) that OpenSSH servers support. This means that without `sshpass` installed, you may still be asked for the password more than once during a single sync against these systems — once per folder examined, and once per file individually copied. This isn't a bug; it's a genuine limitation of Dropbear. Installing `sshpass` (see below) resolves it completely.

### Option 3 — sshpass (recommended for Batocera/Recalbox/RetroPie targets)

Install `sshpass` (see [Installing Dependencies](#installing-dependencies)), then use `--ask-source-password` / `--ask-target-password` as normal. With `sshpass` present, the password you type once is automatically supplied to every individual connection romsync needs to make, and you won't be asked again for the rest of that run.

```bash
brew install sshpass        # macOS
sudo apt install sshpass    # Linux
```

If `sshpass` isn't installed, `romsync` will tell you so (once per run) and fall back to asking you for the password normally each time it's needed — it never fails silently.

---

## Worked Examples

### First-ever sync, local to local, completely empty target

```bash
python3 romsync.py \
  --src-type local --src-path "/Users/jason/Downloads/Atari ROMs" \
  --dst-type local --dst-path "/Volumes/RetroGaming/roms/atari5200"
```

### Practice run before syncing to a Batocera box over the network

```bash
python3 romsync.py \
  --src-type local --src-path "/Volumes/RetroGaming/CurrentBatocera" \
  --dst-type ssh   --dst-path /userdata/roms/mame \
  --dst-host batocera.local --dst-user root \
  --ask-target-password \
  --exclude-media --preserve-target \
  --dry-run --verbose
```

### The same sync, for real, once the practice run looks correct

```bash
python3 romsync.py \
  --src-type local --src-path "/Volumes/RetroGaming/CurrentBatocera" \
  --dst-type ssh   --dst-path /userdata/roms/mame \
  --dst-host batocera.local --dst-user root \
  --ask-target-password \
  --exclude-media --preserve-target \
  > synclog.txt 2>&1
```

Saving the output to a file (`> synclog.txt 2>&1`) gives you a permanent record of exactly what happened — useful for a large collection where the on-screen output scrolls past too quickly to read.

### Pulling ROMs off a RetroPie box back onto your computer

```bash
python3 romsync.py \
  --src-type ssh   --src-path /home/pi/RetroPie/roms \
  --src-host retropie.local --src-user pi \
  --ask-source-password \
  --dst-type local --dst-path "/Volumes/RetroGaming/roms"
```

### Re-running a sync you've done before, with your saved settings

```bash
python3 romsync.py --saved
```

### Same as above, but always check first

```bash
python3 romsync.py --saved --dry-run --verbose
```

---

## Understanding the Output

### Per-folder lines

As it works, you'll see a line for every folder, telling you which of the three tiers applied:

```
  arcade        →  Tier 1 — new dir      → rsync
  mame          →  Tier 3 — conflicts    → rsync + selective copy
  snes          →  Tier 2 — no conflicts → rsync
```

### The final summary

```
══════════════════════════════════════════════════════════
[DRY-RUN] Complete.
  Tier 1 dirs (full rsync, target absent)    : 4
  Tier 2 dirs (full rsync, no conflicts)     : 12
  Tier 3 dirs (selective, conflicts present) : 3
  Files selectively copied (new ROMs)        : 47
  Files skipped (stem / MD5 conflicts)       : 312
  Errors                                     : 0
```

### Special reports

If `romsync` finds files that are the same ROM under different names (detected by content, not just filename), it lists them at the end:

```
MD5 DUPLICATES DETECTED  (3 file(s))
  Directory   Source file                       Target match    MD5
  atari5200   Centipede (1982) (Atari).bin       Centipede.a52   4a2f91c0…
```

If the **source itself** contained the same ROM twice, that's reported separately:

```
SOURCE-INTERNAL DUPLICATES DETECTED  (2 stem(s))
  Directory   Stem        Kept            Discarded
  mame        avengers    avengers.7z     avengers.zip
```

Neither of these reports means something went wrong — they're there so you can see exactly what decisions were made and double-check them if you want to.

---

## Safety Features

A few things `romsync` does specifically to avoid surprises:

- **Never overwrites blindly.** The tiered approach exists specifically so that files which are genuinely the same ROM (just named differently) are never duplicated, and — with `--preserve-target` — files already on the target are never silently replaced.
- **Reports rather than guesses, wherever there's genuine ambiguity.** If two ROMs look like duplicates but their content doesn't match (different revision, different region, a MAME merged set vs a split set), they are reported, not deleted or skipped automatically.
- **Passwords are never written to disk.** Whether typed via prompt or passed on the command line, passwords exist only in memory for the duration of the run.
- **macOS junk files are excluded automatically.** Files like `._filename` and `.DS_Store` that macOS creates on any drive it touches are never synced into your collection by default.
- **Dry run is always available.** Every single decision `romsync` would make can be previewed with `--dry-run` before anything actually happens.

---

## Companion Tool: romclean.py

`romsync` resolves *cross-system* duplicates (same ROM on source and target) automatically, and resolves simple *source-internal* duplicates (same ROM, two extensions, in the source folder) using a quick filename-based preference.

For situations where you want **certainty** — actually extracting two archives and comparing their contents byte-for-byte rather than just trusting a filename-based guess — use the separate `romclean.py` tool, particularly its `--deep` mode:

```bash
python3 romclean.py /path/to/roms --keep 7z --deep --dry-run
```

This is slower (it has to extract and checksum the actual archive contents) but gives a definitive answer, including flagging cases where two same-named archives actually contain *different* data — something a filename-only comparison can never detect.

---

## Troubleshooting

### "Connection closed" with no password prompt, when connecting to Batocera/Recalbox

This is a known quirk: these systems run a minimal SSH server (Dropbear) that doesn't support the same connection-multiplexing feature as a full OpenSSH server. `romsync` detects this automatically and falls back to asking for your password as needed rather than failing. If you see a hard "Connection closed" with **no** password prompt at all, double check the hostname/IP and that SSH is enabled on the device.

### Files I expected to be skipped are being copied anyway

Run with `--dry-run --verbose` and look for the line for that specific folder — it will tell you which tier was used and why. If a file genuinely has a different size or date on the target, `rsync` will want to update it unless `--preserve-target` is set.

### "rsync: command not found" or similar

Revisit [Installing Dependencies](#installing-dependencies) for your platform — `romsync` relies on `rsync`, `ssh`, and `scp` already being installed and on your system PATH.

### I'm being asked for a password many times during one sync

This happens with Dropbear-based targets (Batocera, Recalbox) when `sshpass` isn't installed. Install it (see above) and use `--ask-target-password` — you'll then only be asked once for the whole run.

### A sync seems to be taking a very long time

For large collections with many cross-format duplicates, `romsync` may need to check file content (MD5) for individual files, which is slower than a straightforward copy. This is normal for Tier 3 folders with a lot of raw-format ROMs. Use `--verbose` to see progress as it happens.

---

## Known Limitations

- **MAME merged ROM sets** are not the same, byte-for-byte, as a split/non-merged set for the same game — `romsync` will correctly treat them as genuinely different files, not duplicates.
- **Regional and revision variants** (e.g. a USA release vs a European release of the same game) have different content and will both be kept; this is correct behaviour, not a bug.
- **Gamelist and media path differences between front-ends** (Batocera, RetroPie, Recalbox all store scraped artwork paths slightly differently) are not automatically rewritten. Use `--exclude-media` or `--exclude-gamelist` if you're syncing between different front-end types, and re-scrape on the target afterwards.
- **Syncing directly between two remote systems** (not involving your local computer at all) works, but temporarily stages each file through your computer's temp folder, so make sure you have enough free space for at least your largest single file.

---

## Glossary

**Stem** — the filename without its extension. `pacman.zip` and `pacman.7z` have the same stem (`pacman`) but different extensions.

**MD5 / checksum** — a short fingerprint calculated from a file's actual content. Two files with identical content always produce the same MD5, even if their names are completely different.

**Tier** — the category `romsync` assigns to each folder, describing how much checking is needed before deciding what to copy (see [How a Sync Decision Is Made](#how-a-sync-decision-is-made)).

**Dry run** — a practice run that shows you what would happen without actually changing any files.

**SSH / SFTP / SCP** — standard, secure ways for one computer to connect to another over a network to transfer files or run commands. `romsync` uses these to reach remote systems like Batocera or RetroPie boxes.

**Dropbear** — a lightweight SSH server commonly used on small embedded devices like retro gaming handhelds and boxes, including Batocera and Recalbox. It supports the same basic connections as full SSH servers, but not every advanced feature.

**ControlMaster** — an OpenSSH feature that lets multiple connections to the same server share one underlying authenticated session, avoiding repeated password prompts. Not supported by Dropbear.
