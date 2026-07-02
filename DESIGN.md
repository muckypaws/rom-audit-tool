# DESIGN.md — Contributing a New Platform

This document is for developers who want to add support for a new
emulation platform (EmuELEC, Lakka, RetroDeck, ArkOS, JELOS, or
anything else). It explains the architecture, what you must implement,
what you can optionally override, and the lessons learned from the
three platforms already shipped that you should not have to rediscover
the hard way.

Read this alongside `CLAUDE.md`, which covers debugging methodology
and the hard-won decisions behind specific existing behaviours.

---

## Design Principles

The ROM Audit Tool is built around five principles. Everything in this
document flows from them. If a proposed change conflicts with one of
these, the change needs rethinking — not the principle.

**1. The audit engine is platform-agnostic.**
`rom_audit.py` contains no platform name comparisons, no `isinstance()`
checks, and no `hasattr()` probing. It calls the Platform interface
and trusts the implementation. This means the engine works correctly
for every platform without ever being touched when a new one is added.

**2. Platform-specific behaviour belongs only in platform modules.**
If something needs to work differently on Batocera than on RetroPie,
that difference lives in `modules/platforms/batocera.py` and
`modules/platforms/retropie.py` respectively — not as a branch in
`rom_audit.py` or in any `modules/common/` file. The common layer
contains algorithms; the platform layer contains the data and
decisions those algorithms operate on.

**3. Detection algorithms are shared; detection criteria are not.**
`is_launched()` and `parse_error()` in `modules/common/detection.py`
contain the matching logic once. Each platform supplies its own
`launch_indicators`, `error_markers`, and `exit_marker` as data.
Platforms that need fundamentally different logic override the method
directly — but they override it on their own class, not by editing
the shared algorithm.

**4. New capabilities are added to the base class, not discovered via `hasattr()`.**
When the engine needs a new capability, a concrete method with an inert
default (`return []`, `return None`, `pass`) is added to `base.py` and
called unconditionally. Platforms that need non-default behaviour
override it. This keeps the interface explicit and complete — the
base class is the definitive specification of what a platform can do.

**5. A platform implementation should be replaceable without changing `rom_audit.py`.**
The practical test: if you deleted `batocera.py` and wrote it from
scratch, `rom_audit.py` should need zero edits. If it does need edits,
a platform detail has leaked into the engine — find it and push it
back into the platform class.

---

## Architecture in one paragraph

`rom_audit.py` is platform-agnostic. It never branches on platform
name, never probes for methods with `hasattr()`, and never does
`isinstance()` checks. Everything platform-specific is expressed as
a property or method on the `Platform` abstract base class in
`modules/platforms/base.py`. The audit loop in `rom_audit.py` calls
the base class interface; your platform subclass provides the data and
behaviour behind that interface. The common detection algorithms
(`is_launched()`, `parse_error()`) live in `modules/common/detection.py`
and are injected with platform-specific strings — your platform
supplies the strings, not a reimplementation of the algorithm.

---

## Before you start: understand the three existing platforms

They represent three genuinely different launcher architectures. At
least skim the relevant one before writing a line of new code:

| Platform | Launcher | Config mechanism | Autofix writes to |
|---|---|---|---|
| Batocera | `emulatorlauncher.py` (Python, configgen) | `batocera.conf` | `batocera.conf` per-game key |
| Recalbox | `emulatorlauncher.py` (same ancestry) | `_readme.txt` priority lists + sidecar `.recalbox.conf` | Sidecar file next to ROM |
| RetroPie | `runcommand.sh` (bash) | `/opt/retropie/configs/` per-system | `emulators.cfg` per-game key |

The new platform you're adding probably resembles one of these more
than the others — identify which and read that implementation first.
EmuELEC, ArkOS, and JELOS are all close Batocera/Recalbox descendants
(same configgen lineage); Lakka is closer to RetroPie's structure.

---

## Step 1 — Create the module file

```
modules/platforms/yourplatform.py
```

Start with:

```python
from __future__ import annotations   # REQUIRED — see note below

import os
import subprocess
from modules.platforms.base import Platform
from modules.common.logging import log
```

**`from __future__ import annotations` is not optional.** Without it,
`str | None` union type hints in method signatures crash at class
definition time on Python 3.9, which ships with at least one of the
supported platforms. Every new platform file must have this import.
This was a real crash, not a theoretical risk.

---

## Step 2 — Implement the mandatory interface

These are `@abstractmethod` properties/methods. The class will not
instantiate without them. See `base.py` for the full docstring of each.

### Paths

```python
@property
def name(self) -> str:
    return 'YourPlatform'          # Human-readable, used in logs

@property
def roms_path(self) -> str:
    return '/path/to/roms'         # Base directory containing system subfolders

@property
def stdout_log(self) -> str:
    return '/path/to/stdout.log'   # File the tool monitors for launch indicators

@property
def stderr_log(self) -> str:
    return '/path/to/stderr.log'   # File the tool monitors for error markers
                                   # May be the same path as stdout_log

@property
def results_csv(self) -> str:
    return '/path/to/rom_audit/rom_audit.csv'

@property
def log_file(self) -> str:
    return '/path/to/rom_audit/rom_audit.log'

@property
def pid_file(self) -> str:
    return '/path/to/rom_audit/rom_audit.pid'

@property
def error_log_base(self) -> str:
    return '/path/to/rom_audit/audit_logs'
```

### Detection strings

These are injected into the shared detection algorithms. They are
data, not logic.

```python
@property
def launch_indicators(self) -> list[str]:
    # Strings whose presence in the log means the emulator was invoked.
    # WARNING: this means "the launcher *attempted* to start the
    # emulator", NOT "the game is running". A launch indicator can fire
    # immediately — before the emulator has had a chance to fail.
    # This is a critical distinction: see CLAUDE.md "Three Categories
    # of False-OK" for what happens when you get this wrong.
    return ['Starting emulator', 'Running command:']

@property
def error_markers(self) -> list[str]:
    # Strings that indicate a specific, named failure.
    return ['ERROR: could not load ROM', 'No such file or directory']

@property
def exit_marker(self) -> str:
    # String indicating the launcher itself exited with an error code.
    # Set to '' if the platform does not log one.
    return 'Exiting with status 1'
```

### Launcher command

```python
@abstractmethod
def get_launcher_cmd(self) -> list[str]:
    # Base command used by the default build_launch_cmd() implementation.
    # The default appends: ['-system', system, '-rom', rom]
    # If your platform uses different argument structure, override
    # build_launch_cmd() directly instead.
    return ['/usr/bin/python3', '-m', 'yourplatform.launcher']
```

### Environment

```python
def get_env(self) -> dict[str, str]:
    env = os.environ.copy()
    # Add display server variables to match what the frontend sets.
    # SSH sessions don't inherit what the frontend sets at launch —
    # this has caused real bugs. At minimum, match what the frontend
    # sets for XDG_SESSION_TYPE, SDL_NOMOUSE, and LANGUAGE.
    env['XDG_SESSION_TYPE'] = 'wayland'  # or 'x11' if applicable
    env['SDL_NOMOUSE'] = '1'
    env['LANGUAGE'] = ''
    return env
```

---

## Step 3 — Override what's different from the defaults

These all have working defaults in `base.py`. Override only where your
platform actually differs.

### `version` property

```python
@property
def version(self) -> str:
    # Override to return the detected platform version string.
    # Used in the dashboard header. Returns '' by default.
    try:
        return open('/etc/yourplatform-version').read().strip()
    except Exception:
        return ''
```

### `subprocess_capture`

Controls whether run_test() redirects the subprocess stdout/stderr to
your `stdout_log`/`stderr_log` paths.

```python
@property
def subprocess_capture(self) -> bool:
    # True (default): the subprocess writes to the monitored log files
    #   via stdout/stderr redirection. Use this for platforms like
    #   Batocera/Recalbox where the launcher writes meaningful output
    #   to its own stdout/stderr.
    #
    # False: the platform writes logs to a fixed path regardless of
    #   stdout/stderr. run_test() sends subprocess output to /dev/null
    #   and clears/reads stdout_log directly. Use this for platforms
    #   like RetroPie's runcommand.sh which always writes to a fixed
    #   log path.
    return True
```

### `build_launch_cmd()`

Override if the launcher uses positional arguments or a different
structure from the Batocera default (`[-system X, -rom Y]`):

```python
def build_launch_cmd(self, system: str, rom: str) -> list[str]:
    # Example: positional args like runcommand.sh
    return ['/opt/yourplatform/runcommand.sh', '0', '_SYS_', system, rom]
```

### `parse_error()`

Override when your platform needs exit-code-aware analysis beyond what
the generic marker matching provides. This is the method `run_test()`
calls — do not call `detection.parse_error()` directly from `run_test()`
bypassing this. That mistake existed in the codebase for a period and
caused all exit codes to be silently ignored. The fix is one call-site
change but the impact was severe.

```python
def parse_error(
    self,
    stdout_content: str,
    stderr_content: str
) -> tuple[str, str]:
    # First check exit codes if your platform logs them:
    import re
    m = re.search(r'Process exitcode:\s*(-?\d+)', stdout_content)
    if m:
        code = int(m.group(1))
        if code not in TOLERATED_EXIT_CODES.get('your_emulator', {code}):
            # Handle negative (signal) codes specially
            if code < 0:
                signals = {-11: 'SIGSEGV', -6: 'SIGABRT', -15: 'SIGTERM'}
                if code in (-15, -9):
                    pass  # Our own kill — expected
                else:
                    name = signals.get(code, f'signal {-code}')
                    return 'ERROR', f'Process crashed: {name} (exitcode {code})'

    # Fall back to marker-based detection
    from modules.common import detection
    return detection.parse_error(
        stdout_content, stderr_content,
        self.error_markers, self.exit_marker
    )
```

### `validate_rom_launch()`

Pre-launch gate. Return `(False, reason)` to record MISSING CORE
without ever spawning a subprocess. Use this for BIOS checks, core
availability checks, or extension compatibility checks (e.g. a `.7z`
ROM against a `.zip`-only core). The base returns `(True, '')`.

### `capture_screenshot()`

Implement the platform-specific capture tool:

```python
def capture_screenshot(self, dest_path: str) -> bool:
    import subprocess
    try:
        # Use shutil.move() not copy if writing to a platform path
        # that users can browse — copy leaves the original behind and
        # accumulates clutter. This was a real bug on Recalbox.
        result = subprocess.run(
            ['fbgrab', dest_path],
            capture_output=True, timeout=10
        )
        return result.returncode == 0 and os.path.exists(dest_path)
    except Exception:
        return False
```

### `kill_emulators()`

The base kills `retroarch` and any processes in `self.emulator_processes`.
Override only if the kill *mechanism* itself needs to differ (a
different process name, a different signal). If you just need extra
time after the kill before logs get read — see `get_post_kill_delay()`
below instead; that's the proper place for a pure timing need now,
rather than overriding this method just to add a `time.sleep()`.

### `get_post_kill_delay(system)`

Seconds to wait after the emulator is confirmed dead, before reading
its logs for the final verdict. Default is `POST_KILL_FLUSH_DELAY`
(0.3s, ordinary flush-timing slack). Override per system where there's
a *confirmed* need for more, mirroring `get_launch_timeout()`'s
pattern. Recalbox's own `kill_emulators()` override documents the
precedent this exists to generalise: EGL/GLES graphics context release
on RPi Zero/GPi Case needs real time, and not waiting produces exit
code 1. Batocera hit the identical exit code on Dreamcast/Flycast-
family systems — confirmed (for at least one ROM; see CLAUDE.md for
the full account, including that this investigation wasn't fully
closed out) via exact timing math: a ROM ran an entire 12s display
window successfully, genuinely playable, and still failed right at
the kill, ruling out "killed too early" and pointing at the kill
signal's aftermath specifically. Don't guess at this number — get
confirmed evidence the default isn't enough before overriding it; two
rounds of guessing a bigger number on Dreamcast didn't fully resolve
the underlying issue either time.

### `get_display_time(system)`

Seconds to display a launched game before killing it. Default is the
module-level `DISPLAY_TIME` constant (3s). Override per system where a
core needs longer before its own state is meaningful to read —
confirmed need on RetroPie's `lr-gw` core (Game & Watch), which needs
several seconds running before RetroArch's own "Content ran for..."
timer even starts counting; killed at the previous flat 5s default,
the timer was permanently zero, not delayed. The general principle:
if a result looks wrong specifically for one system and a longer
manual run (UI-launched, not killed early) looks right, this is the
first thing to check — not a parsing bug.

### `get_configured_core(system, romname)`

Resolve which core a specific ROM would actually launch with, given
*current* config, without launching anything. Default returns `''`
(unknown/not applicable) — most platforms don't need this, since
their own `build_launch_cmd()` either resolves the core itself or
there's no equivalent masking risk. Only override if your platform has
a config mechanism (like Batocera's per-game `batocera.conf` entries)
that could route a ROM through a core your audit logic should know
about *before* testing — specifically, before deciding whether to
force a verification screenshot for a core in `UNVERIFIED_CORES`.
Confirmed need: a ROM with an existing per-game entry pointing at a
masking-prone core sailed through the regular (non-autofix) test path
with a plain, untrusted `OK`, because nothing checked the configured
core before a normal test — this hook closes that gap. Read-only by
design; it must never have side effects, since it runs before the
actual test and shouldn't be able to affect what gets launched.

### `pre_audit()` / `post_audit()`

Use for one-time setup/teardown. RetroPie stops EmulationStation here
since KMS/DRM means ES holds the display exclusively. If your platform
has a similar constraint, handle it here. `post_audit()` runs in a
`finally` block — it is guaranteed to execute even if the audit is
interrupted.

### `pre_test_run()` / `post_test_run()`

Use for per-run setup/teardown scoped to what's about to be tested.
Batocera suspends a global `mame.core=fbneo` override here so test
results aren't masked by it. `post_test_run()` also runs in `finally`.

### `attempt_autofix()`

Autofix is platform-specific because the config mechanism differs
completely across platforms. The base class returns NO COMBINATIONS.
Implement this only once you understand your platform's config file
format well enough to write and revert changes safely. See
`batocera.py` for the `batocera.conf` approach, `retropie.py` for
`emulators.cfg`, and `recalbox.py` for per-ROM sidecar files.

**Critical**: both `validate_rom_launch()` and `build_launch_cmd()`
must agree on which core is being tested during each autofix attempt.
On Recalbox, both call `_resolve_emulator_core()` — the override that
forces a specific core during autofix overrides that shared function,
not `build_launch_cmd` directly. Overriding only `build_launch_cmd`
means `validate_rom_launch()` still resolves the global default and may
reject the attempt before the emulator is ever launched. This was a
real bug that made autofix silently try the wrong core every time.

---

## Step 4 — Register the platform in detection.py

```python
# modules/common/detection.py  —  detect_platform()

from modules.platforms.yourplatform import YourPlatform

# Add a detection condition BEFORE the final RuntimeError.
# Use the most specific file/directory that uniquely identifies
# your platform. Prefer something that only exists on that specific
# distribution rather than something shared across several (e.g.
# /recalbox/share/system/recalbox.conf rather than just /etc/os-release).
if os.path.exists('/etc/yourplatform-release'):
    log("Detected platform: YourPlatform")
    return YourPlatform()
```

Order matters: Recalbox is checked before RetroPie because both share
some common paths on some installs; `recalbox.conf` is definitive. Use
the same principle for your platform — find the most definitive
single-file signal and comment the reasoning if ordering matters.

---

## Step 5 — filehandling.py additions (if needed)

You do not normally need to touch `filehandling.py` for a new platform.
But two situations do require it:

### New companion-file pairs

If your platform has a "controlling file + raw data file(s)" pattern
analogous to `.cue`/`.bin` or `.uae`/`.adf` — a file that carries
the config the emulator actually needs, with one or more raw data
files it references — add the exclusion logic to `discover_roms()`.
See the `.uae` pre-scan block (added in 1.4.1) as the template. The
pattern is always:
1. Pre-scan for the controlling extension
2. Collect stems from the controlling file itself plus any paths it
   references internally (parsed generically by extension, not by
   hardcoded key names)
3. Skip raw files whose stems are in that set in the main loop

### New SKIP_SYSTEMS entries

If your platform has system folders that aren't game systems (media
players, file managers, streaming clients), add them to `SKIP_SYSTEMS`
with a comment explaining why.

---

## Things that must not change in rom_audit.py

- **No `hasattr()` probing**: if you need a new capability, add a
  base class method with an inert default and call it unconditionally.
  The pattern that was banned: `if hasattr(platform, 'thing'):`. The
  correct pattern: add `def thing(self): return []` to `base.py`,
  call `platform.thing()` everywhere.

- **No `isinstance()` or platform-name branching**: `rom_audit.py`
  must not contain `if platform.name == 'Batocera':`. Encode all
  per-platform differences in the platform class.

- **`self.parse_error()` not `detection.parse_error()` directly**:
  `run_test()` calls `self.parse_error()` so platform overrides are
  reached. Calling the common function directly bypasses them. This
  was a real bug that caused exit codes to be silently ignored across
  all of Recalbox — see CLAUDE.md for the full account.

- **`--system ports` filter logic**: `ports` is excluded from
  `discover_roms()` via `SKIP_SYSTEMS`; actual port ROMs come from
  `discover_ports_roms()`. The filter stripping logic in `main()` must
  correctly distinguish `['ports']` (skip standard scan entirely),
  `['ports', 'mame']` (standard scan for mame only), and `None` (no
  filter). These are three different cases — `None` and `[]` both mean
  "no restriction" to `discover_roms()`, so they are not equivalent
  to each other in the calling code. See CHANGELOG 1.4.2.

---

## Establishing launch_indicators for a new platform

This is frequently where new implementations go wrong. The temptation
is to pick a string that fires as early as possible to minimise test
latency. The problem is that the earlier the indicator, the more likely
it represents "the launcher *attempted* something" rather than "the
emulator actually started."

Recalbox's `launch_indicators` fire the instant `emulatorlauncher`
attempts to invoke RetroArch — before success or failure is known.
This is intentional (the tool then enters a display window and relies
on `parse_error()` + early-exit detection instead), but it requires
that `parse_error()` and the Phase 2 early-exit check are correctly
implemented to compensate. If you use very early indicators, both must
work correctly or you'll have silent false OKs.

**Recommended approach for a new platform:**

1. Run the launcher manually against a known-good ROM over SSH, with
   full output visible. Note the exact strings logged and when they
   appear relative to the game actually starting.

2. Do the same for a known-bad ROM (missing BIOS, wrong core,
   corrupted file). Note where the paths diverge in the log.

3. Choose the latest string that still fires for both good and bad
   launches — you want something that fires after the platform has
   committed to launching, but ideally before it decides whether
   that launch succeeded. The exit marker and error markers handle
   the bad path.

4. Check `stderr` too, not just `stdout`. Several real markers were
   only found in `stderr` even though the platform's own dashboard
   only showed `stdout` output. Always check the tool's own captured
   `stderr` for the same run before concluding a marker doesn't exist.

---

## Minimum test checklist before submitting

Run these against your new platform before opening a pull request.
Each exercises a different part of the stack.

```bash
# 1 — Prerequisites check: does the tool find what it needs?
python3 scripts/prereqs.py --verbose

# 2 — Single known-good ROM: does it report OK?
python3 rom_audit.py --test knowngoood.zip --system snes

# 3 — Single known-bad ROM (wrong core, missing BIOS): does it report ERROR?
#     Not just TIMEOUT — a proper error with a meaningful note.
python3 rom_audit.py --test knownbad.zip --system snes

# 4 — Screenshot capture: does --screenshot produce a usable image?
python3 rom_audit.py --test knowngood.zip --system snes --screenshot

# 5 — Small system run (5 ROMs): does the dashboard render correctly?
python3 rom_audit.py --system snes --limit 5

# 6 — Ports (if supported): does --system ports actually scan ports?
python3 rom_audit.py --system ports --limit 3

# 7 — Recheck: does --recheck correctly re-run only the targeted system?
python3 rom_audit.py --system snes --recheck

# 8 — Autofix (if implemented): does a known-fixable ROM get fixed?
#     Verify in the config file that the override was actually written.
python3 rom_audit.py --test fixable.zip --system mame --autofix

# 9 — Timing independence: does the SAME known-bad ROM report the
#     SAME result with and without --screenshot? If not, error
#     detection is timing-sensitive on your platform too — see
#     CLAUDE.md's account of fpoint1.zip, where take_screenshot()
#     running before kill_emulators() bought just enough extra time
#     for error text to land that the no-screenshot path didn't get,
#     turning a genuine failure into a silent OK. OK_RECHECK_DELAY in
#     base.py exists specifically to cover this for every platform —
#     this test is what confirms it's actually working on yours.
python3 rom_audit.py --test knownbad.zip --system snes
python3 rom_audit.py --test knownbad.zip --system snes --screenshot
# Compare the two results — they must match.
```

---

## Platform architecture notes for the Phase 2+ candidates

Brief notes on what's known about each, to save initial research time.
All of these are tentative — verify against the actual running system.

**EmuELEC** — Kodi/Retroarch-based distribution for Amlogic SBCs.
Uses a configgen lineage similar to Batocera. Likely very close to
Batocera in structure; the main difference is paths (everything under
`/emuelec/` rather than `/userdata/`) and the version detection file.
The global config analogue is `/emuelec/configs/emuelec.conf`. A
Batocera subclass with path overrides may be sufficient rather than a
full reimplementation.

**ArkOS / JELOS / TheRA / muOS** — Custom OS distributions for ARM
handheld hardware (RG351, RG552 and similar). Also configgen-based.
Same caveat as EmuELEC: likely a Batocera subclass with path and
launcher command changes. Notable difference: these may use separate
left/right microSD cards for system and ROMs, so `roms_path` may
need auto-detection or configuration rather than a hardcoded path.
`additional_roms_paths` exists exactly for this.

**Lakka** — LibreELEC-based, pure RetroArch. No EmulationStation
frontend — Retroarch itself is both the frontend and the emulator. This
is the most architecturally different from the existing three. There is
no separate launcher script; you'd be calling `retroarch` directly with
`-L <core> <rom>`. The biggest open question is log location: RetroArch
writes logs to `~/.config/retroarch/logs/retroarch.log` by default but
this path varies. Pre-audit ES management (`pre_audit()`) is not
applicable. Closer to a from-scratch implementation than a subclass.

**RetroDeck** — Flatpak-based, runs on SteamOS/desktop Linux. Uses
its own wrapper scripts around RetroArch and standalone emulators.
The ROM and config paths are inside the Flatpak sandbox at
`~/.var/app/net.retrodeck.retrodeck/`. May have a mix of libretro
and standalone emulator launches, similar to RetroPie. Worth checking
whether it exposes a launch command analogous to `runcommand.sh`.

**Steam Deck** — Worth separating from RetroDeck. EmuDeck installs
emulators as native binaries or Flatpaks directly onto SteamOS. No
single launcher script — each emulator is launched independently.
This is the most complex target: you'd likely need either a generic
RetroArch-direct mode (similar to Lakka) or a per-emulator launcher
map. Treat as research-first before attempting an implementation.

---

## File checklist for a new platform contribution

```
modules/platforms/yourplatform.py    # The implementation (this document)
modules/common/detection.py          # Register the new platform
CHANGELOG.md                         # Entry under a new version number
README.md                            # Add to the Supported Platforms table
rom_audit_user_manual.docx           # Installation section 3.N
CLAUDE.md                            # Any platform-specific decisions that
                                     # weren't obvious, with the reasoning
```

If you're adding `SKIP_SYSTEMS` entries or companion-file exclusion
logic, `modules/common/filehandling.py` also needs updating.

---

## Questions worth answering before you write any code

1. **What command does the frontend use to launch a ROM?** Run it
   manually and capture the full output (both stdout and stderr).

2. **What does the log look like for a successful launch? A failed
   one?** These are your `launch_indicators` and `error_markers`.

3. **Is the display/framebuffer available from SSH, or does the
   frontend hold it?** (RetroPie: frontend holds it → `pre_audit()`
   stops ES. Batocera/Recalbox: framebuffer shared → no stop needed.)

4. **Where does the platform write per-game emulator overrides?**
   This determines the autofix architecture.

5. **Does the platform have a concept of default-core-per-system that
   could silently mask failures?** (Batocera: yes, `mame.core=fbneo`
   → `pre_test_run()` suspension. Others: check before assuming no.)

6. **What Python version ships with this platform?** If 3.9 or below,
   `from __future__ import annotations` is mandatory.
