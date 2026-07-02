# ROM Audit Tool — Quickstart

Get from zero to your first audit results in under ten minutes.

---

## What you need

- A Batocera, RetroPie, or Recalbox installation with SSH access
- Python 3.9+ (already present on all three)
- Your ROM collection already in place

---

## 1. Deploy the files

Copy the project to your device over SSH. Replace the IP address and
destination path for your platform:

**Batocera**
```bash
scp -r rom_audit/ root@batocera.local:/userdata/system/
```

**RetroPie**
```bash
scp -r rom_audit/ pi@retropie.local:~/RetroPie/
```

**Recalbox**
```bash
scp -r rom_audit/ root@recalbox.local:/recalbox/share/system/
```

Default credentials: Batocera `root`/`linux` · RetroPie `pi`/`raspberry`
· Recalbox `root`/`recalboxroot`

---

## 2. SSH in and check prerequisites

```bash
ssh root@batocera.local        # or pi@retropie.local, root@recalbox.local
cd /userdata/system/rom_audit  # adjust path for your platform

python3 scripts/prereqs.py
```

Fix anything it flags before continuing. Run with `--verbose` for detail.

---

## 3. Run your first audit

Start small — one system, five ROMs — so you can see the dashboard
and confirm everything is working before committing to a full run:

```bash
python3 rom_audit.py --system snes --limit 5
```

You'll see a live dashboard. Each ROM is launched, held for a few
seconds, and closed. Results appear as they complete.

> **RetroPie only:** EmulationStation must be stopped before running.
> The tool does this automatically. After the audit, restart ES from
> the console or reboot.

---

## 4. Read the results

The dashboard shows a summary at the top while the audit runs. When
it finishes:

```bash
python3 scripts/summary.py
```

| Status | Meaning |
|---|---|
| `OK` | Launched and ran correctly |
| `IMPERFECT` | Launched, but MAME flagged known accuracy issues — still playable |
| `NEEDS REVIEW` | Result from a core that can mask failures — not confirmed broken, check the screenshot |
| `ERROR` | Failed to launch |
| `GENUINE ERROR` | Failed with every available core — likely a bad dump |
| `MISSING BIOS` | Required BIOS file not present |
| `MISSING CORE` | Required emulator not installed |
| `TIMEOUT` | Emulator took too long to respond |

Results are saved to `rom_audit.csv` in the same folder. You can
reopen it in any spreadsheet application.

---

## 5. Investigate errors

View the log for any failing ROM:

```bash
# See the error note in the CSV
cat rom_audit.csv | grep ERROR

# View the full captured log for a specific ROM
ls audit_logs/snes/
cat audit_logs/snes/Broken\ Game.sfc/stdout.log
```

---

## 6. Try autofix

The tool can automatically try alternative emulator cores for failing
ROMs:

```bash
python3 rom_audit.py --system snes --recheck --autofix
```

When a fix is found, it's written to your platform's config
permanently — the ROM will use the working core from then on.

---

## 7. Run a full audit

Once you're happy with how it works, audit everything:

```bash
python3 rom_audit.py
```

This can take many hours depending on collection size. It's safe to
run overnight — use `screen` or `tmux` to keep it running after you
disconnect:

```bash
screen -S audit
python3 rom_audit.py
# Ctrl+A then D to detach; screen -r audit to reattach
```

---

## 8. Review with screenshots

Add `--screenshot` to capture an image of each game at the moment
it's running — useful for spotting ROMs that launch without error but
display nothing useful:

```bash
python3 rom_audit.py --system mame --screenshot
```

Screenshots are saved alongside the logs in `audit_logs/`.

---

## Common commands at a glance

```bash
# Test a single ROM
python3 rom_audit.py --test "Game Name.zip" --system snes

# Audit one system
python3 rom_audit.py --system mame

# Recheck only previously failed ROMs
python3 rom_audit.py --recheck

# Recheck and attempt fixes
python3 rom_audit.py --recheck --autofix

# Audit only ROMs not yet in the CSV
python3 rom_audit.py --new

# Track file checksums (set once — stays on record and gets checked
# automatically on every later run, even without this flag again)
python3 rom_audit.py --checksum md5

# Per-system summary
python3 scripts/summary.py

# Compare two CSV files (before and after fixes)
python3 scripts/compare.py old.csv rom_audit.csv --summary

# Check for ROM duplicates across systems
python3 scripts/duplicates.py
```

---

## Troubleshooting

**All ROMs in a system failing:** Check BIOS files are installed, then
run `python3 scripts/prereqs.py --verbose`.

**Implausibly high error rate across everything:** On a Raspberry Pi,
check for under-voltage — `vcgencmd get_throttled`. A non-zero result
means the Pi was throttled during the run, which causes failures that
have nothing to do with the ROMs. A better power supply is the fix.

**Games fail on a Pi with no display connected:** The GPU can't
establish a display mode with nothing plugged in. See the
Troubleshooting section of the full manual for `hdmi_force_hotplug=1`.

**"Another audit is already running":** Remove the stale PID file:
```bash
rm /userdata/system/rom_audit/rom_audit.pid     # Batocera
rm ~/RetroPie/rom_audit/rom_audit.pid            # RetroPie
rm /recalbox/share/system/rom_audit/rom_audit.pid # Recalbox
```

---

## Next steps

- Full reference: **rom_audit_user_manual.docx**
- Platform paths and config details: **README.md**
- Contributing a new platform: **DESIGN.md**
