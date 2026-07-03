# ROM Audit Tool

A multi-platform ROM compatibility testing utility for retro gaming systems.
Automatically launches each ROM through the native emulator, detects success
or failure, and records results to a CSV file for review and remediation.

**Version 1.4.3** (Initial Public Release) — Batocera · RetroPie · Recalbox

**→ [QUICKSTART.md](QUICKSTART.md)** — new here? Start here. Up and running in ten minutes.

---

## What It Does

- Launches every ROM in your collection one by one through the native emulator
- Detects whether each game loaded successfully from emulator logs and exit codes
- Records results (OK / IMPERFECT / ERROR / TIMEOUT / MISSING BIOS / MISSING CORE) to a CSV
- Captures optional screenshots at launch, with annotation and flat-directory output
- Records MD5 or SHA1 checksums to detect silent ROM replacements between runs
- Attempts automatic core fixes for failing ROMs and writes the fix to your config
- Archives error logs per ROM for offline diagnosis
- Discovers and tests ports (Quake, Doom, Cave Story, etc.) on Recalbox and Batocera
- Compares audit runs to surface regressions, improvements and ROM replacements
- Supports resuming interrupted audits, selective recheck, and force-retest of OKs

---

## Supported Platforms

| Platform  | Version           | Status         |
|-----------|-------------------|----------------|
| Batocera  | v36, v38, v43+    | ✅ Full support |
| RetroPie  | Bullseye or later | ✅ Full support |
| Recalbox  | 9.x / 10.x        | ✅ Full support |

**Minimum Python: 3.9+**  
RetroPie on Raspberry Pi OS Bullseye ships with Python 3.9. Buster (3.7) is not supported — upgrade your OS first.

---

## Quick Start

```bash
# Connect over SSH
ssh root@batocera.local        # Batocera
ssh pi@retropie.local          # RetroPie
ssh root@recalbox.local        # Recalbox

# Copy the tool
scp -r rom_audit/ root@batocera.local:/userdata/system/

# Check prerequisites
cd /userdata/system/rom_audit
python3 scripts/prereqs.py

# Test a single system first
python3 rom_audit.py --system atari2600 --limit 5

# Full audit
python3 rom_audit.py
```

---

## Tool Locations

| Platform  | Tool root                              | Results CSV                                      |
|-----------|----------------------------------------|--------------------------------------------------|
| Batocera  | `/userdata/system/rom_audit/`          | `rom_audit/rom_audit.csv`                        |
| RetroPie  | `~/RetroPie/rom_audit/`                | `rom_audit/rom_audit.csv`                        |
| Recalbox  | `/recalbox/share/system/rom_audit/`    | `rom_audit/rom_audit.csv`                        |

---

## Common Commands

```bash
# Audit
python3 rom_audit.py                          # Full audit, all systems
python3 rom_audit.py --system mame            # One system
python3 rom_audit.py --system ports           # Ports only (Recalbox/Batocera)
python3 rom_audit.py --no-dashboard           # Plain log output (SSH/tmux)
python3 rom_audit.py --limit 10               # Test 10 ROMs per system

# Retest
python3 rom_audit.py --recheck                # Retest all non-OK results
python3 rom_audit.py --recheck-all            # Retest everything including OKs
python3 rom_audit.py --recheck-all --system naomi   # Fresh baseline one system
python3 rom_audit.py --test "pacman.7z" --system arcade   # Single ROM

# Screenshots
python3 rom_audit.py --screenshot             # Capture screenshot per ROM
python3 rom_audit.py --screenshot --annotate  # Burn system/ROM/timestamp in
python3 rom_audit.py --screenshot --screenshot-flat   # Flat dir, named per ROM
python3 rom_audit.py --screenshot-delay 6    # Extra wait for slow-loading systems

# Checksums
python3 rom_audit.py --checksum md5           # Record MD5 per ROM in CSV
python3 rom_audit.py --checksum sha1          # Record SHA1 per ROM in CSV

# Autofix
python3 rom_audit.py --recheck --autofix      # Recheck and fix failing ROMs
python3 rom_audit.py --recheck --autofix --system mame

# Cleanup
python3 rom_audit.py --cleanup --action move --dry-run   # Preview
python3 rom_audit.py --cleanup --action move             # Quarantine broken ROMs
python3 rom_audit.py --cleanup --action move --include-imperfect   # Also quarantine IMPERFECT ROMs

# Retroactive screenshot cleanup (Recalbox — see Known Limitations)
python3 scripts/cleanup_orphaned_screenshots.py rom_audit.csv --dry-run

# Long runs (SSH — use tmux to survive disconnection)
tmux new-session -s romaudit
python3 rom_audit.py --no-dashboard
# Detach: Ctrl+B then D
# Reattach: tmux attach -t romaudit
```

---

## Helper Scripts

```bash
python3 scripts/prereqs.py                   # Check platform prerequisites
python3 scripts/summary.py                   # Pass/fail totals per system
python3 scripts/summary.py --sort errors     # Worst systems first
python3 scripts/romlist.py --errors          # List all failing ROMs
python3 scripts/romlist.py --system arcade   # One system
python3 scripts/compare.py old.csv new.csv   # Compare two audit runs
python3 scripts/compare.py a.csv b.csv --checksum   # Flag ROM replacements
python3 scripts/duplicates.py                # Find duplicate ROMs
python3 scripts/filter.py --status ERROR     # Filter CSV by status
python3 scripts/cleanup_orphaned_screenshots.py rom_audit.csv --dry-run   # Recalbox one-off cleanup
python3 scripts/prune_orphaned_entries.py                    # Find stale companion/deleted CSV rows
python3 scripts/prune_orphaned_entries.py --remove            # Remove confirmed companion rows
python3 scripts/compute_checksums.py                          # Bulk-populate checksums, no testing
python3 scripts/compute_checksums.py --force                  # Recompute everything — integrity sweep
python3 scripts/clear_system_overrides.py --system mame --remove   # Wipe a system's batocera.conf entries clean (Batocera)
```

---

## Result Statuses

| Status          | Meaning                                                        |
|-----------------|----------------------------------------------------------------|
| `OK`            | Game loaded successfully                                       |
| `FIXED`         | Game fixed automatically by autofix                           |
| `IMPERFECT`     | Game loads but MAME flags known accuracy issues — playable but not arcade-perfect |
| `NEEDS REVIEW`  | A result from a core known to silently mask failures (see Autofix) — not confirmed broken, just unconfirmed; check the screenshot |
| `ERROR`         | Game failed to load                                            |
| `GENUINE ERROR` | Failed with every available core — wrong dump or corrupt file  |
| `MISSING BIOS`  | Required BIOS file not found                                   |
| `MISSING CORE`  | Required emulator core not installed, or doesn't support this file's extension |
| `TIMEOUT`       | Emulator took too long to respond                              |
| `LAUNCHED`      | Emulator started but load confirmation was inconclusive        |

---

## Ports Support

The tool discovers and tests standalone ports (Quake, Doom, Prince of Persia, Cave Story etc.) automatically on Recalbox and Batocera by reading each port's `gamelist.xml` manifest. Ports that require user-supplied proprietary files are skipped automatically.

```bash
python3 rom_audit.py --system ports
```

---

## Screenshots

```bash
# Basic — saves alongside error logs
python3 rom_audit.py --screenshot

# Annotated — burns system/ROM/timestamp into image
python3 rom_audit.py --screenshot --annotate

# Flat directory — all screenshots in one folder, named system_rom.png
python3 rom_audit.py --screenshot --screenshot-flat

# With extra delay for BIOS splash screens (seconds)
python3 rom_audit.py --screenshot --screenshot-delay 8
```

Screenshots are useful for catching games that launch without error but display
a "wrong hardware" screen (e.g. Atari 800 games requiring 130XE configuration).

---

## Checksums

Record a hash per ROM to detect silent file replacements between audit runs:

```bash
python3 rom_audit.py --checksum md5
python3 scripts/compare.py before.csv after.csv --checksum
```

The compare script flags three scenarios: ROM replaced (same status, different hash),
suspicious fix (status improved AND hash changed), and confirmed identical file.

---

## Autofix

Autofix tries alternative emulator cores for failing ROMs and writes the working
configuration to your system so the fix persists after reboot.

Some cores (FBNeo, currently) can report a clean pass on a bad dump with no
error text logged — autofix flags any result from one of these as
`NEEDS REVIEW` rather than trusting it outright. Add `--heuristic` to also
run an automatic pixel-based check on the forced verification screenshot,
which can clear the flag automatically when the screen genuinely shows real
content:

```bash
python3 rom_audit.py --recheck --autofix --heuristic
```

| Platform  | Where the fix is written                          |
|-----------|---------------------------------------------------|
| Batocera  | `/userdata/system/batocera.conf`                  |
| RetroPie  | `/opt/retropie/configs/all/emulators.cfg`         |
| Recalbox  | Sidecar `.recalbox.conf` file next to the ROM     |

---

## Known Limitations

**Sub-model configuration (Atari 800, VICE, MSX):** Some games require a specific
hardware variant (e.g. `atari800_system=130XE (128K)`) that must be set manually in
`batocera.conf`. The tool reports these as OK because the emulator exits cleanly —
use `--screenshot` to catch them visually.

**Tape-based systems (ZX Spectrum, C64, Amstrad CPC):** The tool confirms the
emulator launched without error. Tape loading happens inside the emulator and cannot
be detected externally.

**Visually broken while technically running:** A ROM can launch, stay running for
the entire intended display window, and be cleanly terminated exactly as expected —
while showing a frozen or garbled screen the whole time, due to unemulated hardware
(MAME's "005" Sega security board is a confirmed example). This is undetectable from
logs or exit codes by definition. Use `--screenshot` for manual visual review on ROMs
you suspect of this, or `--heuristic` during autofix for an automatic pixel-based
check on cores already known to do this (see Autofix) — `--screenshot` alone is still
the right tool for a ROM not covered by that flag.

**Gameplay testing:** The tool confirms a game loads, not that it plays correctly.

**No display attached (Raspberry Pi):** an unplugged HDMI cable, or a display shared
across several boards under parallel testing, can make every launch fail — not a ROM
problem, the emulator never gets a usable display surface. See Troubleshooting in the
user manual for `hdmi_force_hotplug=1`, a config.txt setting that resolves this at the
firmware level without affecting `--screenshot`.

**Sustained under-voltage (Raspberry Pi):** can cause widespread failures across ROMs
that work fine once power is stable — confirmed to produce exactly this pattern.
Check with `vcgencmd get_throttled` if a system's error rate looks implausibly high;
see Troubleshooting in the user manual for details. Not currently detected
automatically by the tool.

---

## Reporting Issues

Please use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.yml) when
filing an issue — it asks for the things that actually make a report fixable:
the exact command used, the CSV row for the affected ROM, and the archived
stdout/stderr logs from `audit_logs/<system>/<romname>/`. Almost every fix in
this project's history started from reading an actual log, not a description
alone, so a report without logs is usually a dead end before it starts.

A screenshot helps enormously for anything involving "the game didn't actually
load" — several confirmed bugs turned out to be the tool watching
EmulationStation's own menu rather than the game itself, and that's often
invisible without seeing the actual screen. If the ROM isn't copyrighted
(homebrew, public domain), attaching it directly is the fastest path to a fix;
for commercial ROMs, the filename, file size, and checksum are enough to
identify the specific dump without needing the file itself.

---

## Licence

GPL v3. See [LICENSE](LICENSE).  
You may use, modify and distribute this software freely provided any distributed
modifications remain under the same licence and include the original attribution.

---

## Author

Jason — [muckypaws.com](https://muckypaws.com)  
Developed with Claude (Anthropic).
