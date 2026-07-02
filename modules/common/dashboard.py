"""
Terminal dashboard for the ROM Audit Tool.

Renders a live ANSI terminal dashboard using escape codes. Works on
Batocera, RetroPie, Linux, macOS, and SSH sessions without any
external library dependencies.
"""

from __future__ import annotations  # Python 3.7 compatibility

import os
import sys
import time
import shutil

from modules.common import logging as audit_log
from modules.common.filehandling import get_cpu_temp


# ---------------------------------------------------------------------------
# ANSI colour codes
# ---------------------------------------------------------------------------

ANSI_RESET   = "\033[0m"
ANSI_BOLD    = "\033[1m"
ANSI_GREEN   = "\033[32m"
ANSI_RED     = "\033[31m"
ANSI_YELLOW  = "\033[33m"
ANSI_CYAN    = "\033[36m"
ANSI_MAGENTA = "\033[35m"
ANSI_WHITE   = "\033[37m"

MIN_HEIGHT = 22
MIN_WIDTH  = 80   # 65 was the previous floor but never actually fit the
                   # counts line's own 3-fields-per-row minimum (66 chars)
                   # — 80 is a genuine, standard terminal width, not an
                   # arbitrary number; don't lower this without re-checking
                   # every fixed-width line below against it.


class Dashboard:
    """
    Live ANSI terminal dashboard for the ROM Audit Tool.

    Redraws a structured display on each update call using ANSI escape
    codes. Falls back gracefully to plain scrolling output when stdout
    is not a TTY (e.g. when piped or redirected).
    """

    def __init__(self) -> None:
        self._fallback_active: bool = False
        self._last_render: float = 0.0
        self._render_interval: float = 0.5

    def start(self) -> bool:
        """
        Start the dashboard.

        Requests the terminal to resize to a comfortable width and
        activates dashboard mode which suppresses normal log output
        to stdout (log lines still go to the log file and buffer).

        Only activates when stdout is an actual interactive terminal.
        Without this check, _draw() was unconditionally reprinting the
        entire panel — including the last 7 buffered log lines — on
        every render cycle (every 0.5s) regardless of whether stdout
        was a real terminal capable of being redrawn in place. Piped
        or redirected into a file, that produced exactly what it
        sounds like: the same "Testing: X" line reappearing every
        cycle (not a re-test — the same buffered line being reprinted
        because there's no real screen to overwrite), interleaved with
        whatever log() was separately writing, fragmenting both. The
        class docstring always claimed a "graceful fallback to plain
        scrolling output when not a TTY" — this was the actual gap;
        that fallback never existed before. Now: not a TTY means this
        is a no-op (_fallback_active stays False, dashboard-active
        stays False), so log()'s own plain single-shot console output
        takes over automatically — the real fallback, finally.

        Returns:
            True if the live ANSI dashboard actually activated.
            False if it didn't (no TTY) — log()'s own plain
            single-shot console output takes over automatically
            regardless; this return value is purely so the caller can
            log an accurate status message. Previously always
            returned False unconditionally regardless of outcome,
            which made the caller's own fallback message inaccurate
            in the common case where the dashboard had, in fact,
            started fine.
        """
        if not sys.stdout.isatty():
            return False
        self.set_terminal_size()
        self._fallback_active = True
        audit_log.set_dashboard_active(True)
        return True

    def stop(self) -> None:
        """Stop the dashboard and restore normal console output."""
        audit_log.set_dashboard_active(False)

        if self._fallback_active:
            # Reset ANSI state. Summary is logged after stop() as plain
            # text so no buffer flush needed here.
            sys.stdout.write('\033[0m\n')
            sys.stdout.flush()

        self._fallback_active = False

    def update(self, state: dict) -> None:
        """Redraw the dashboard with current state."""
        if self._fallback_active:
            self._draw(state)

    def force_update(self, state: dict) -> None:
        """Redraw immediately, bypassing the rate limiter.

        Use for the final update before stop() to guarantee the
        last ROM result is visible in the Recent log panel.
        """
        if self._fallback_active:
            self._last_render = 0.0
            self._draw(state)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _draw(self, state: dict) -> None:
        """
        Render the ANSI dashboard to stdout.

        Rate-limited to _render_interval seconds to avoid excessive
        terminal writes during fast audit loops.
        """
        now = time.time()
        if now - self._last_render < self._render_interval:
            return
        self._last_render = now

        term  = shutil.get_terminal_size((80, 24))
        width = max(MIN_WIDTH, min(term.columns, 200))

        total     = state.get('total', 0)
        tested    = state.get('tested', 0)
        pct       = (tested / total * 100) if total else 0
        remaining = max(0, total - tested)

        platform = state.get('platform', 'Unknown')
        system   = state.get('current_system', '-')
        rom      = state.get('current_rom', '-')
        status   = state.get('current_status', '-')
        counts   = state.get('counts', {})

        ansi    = self._ansi_enabled()
        reset   = ANSI_RESET   if ansi else ''
        bold    = ANSI_BOLD    if ansi else ''
        magenta = ANSI_MAGENTA if ansi else ''
        green   = ANSI_GREEN   if ansi else ''
        red     = ANSI_RED     if ansi else ''
        yellow  = ANSI_YELLOW  if ansi else ''
        cyan    = ANSI_CYAN    if ansi else ''

        status_colour = self._status_colour(status) if ansi else ''

        bar_w  = max(10, width - 32)
        filled = int(bar_w * tested / total) if total else 0
        bar    = '#' * filled + '-' * (bar_w - filled)

        elapsed_str = self._fmt_time(state.get('elapsed', 0))
        eta_str     = self._fmt_time(state.get('eta', 0))
        temp_str    = get_cpu_temp()

        elapsed_line = f"Elapsed: {elapsed_str}    ETA: {eta_str}"
        temp_part    = f"CPU: {temp_str}"
        timing_line  = (
            elapsed_line +
            temp_part.rjust(width - len(elapsed_line))
        )

        # Per-system progress counters — "Systems Tested: X/Y" (systems
        # fully completed so far, out of distinct systems in this run)
        # and "Rom: X/Y" (progress through the CURRENT system's own rom
        # count specifically, not the overall audit total — the existing
        # Progress bar below already covers the overall figure). Right-
        # justified onto the same lines as System/ROM, same pattern as
        # the CPU temp above.
        total_systems     = state.get('total_systems', 0)
        systems_completed = state.get('systems_completed', 0)
        system_rom_index  = state.get('system_rom_index', 0)
        system_rom_total  = state.get('system_rom_total', 0)

        system_line_left  = f"System : {system}"
        system_line_right = f"Systems Tested: {systems_completed}/{total_systems}"
        system_line = (
            system_line_left +
            system_line_right.rjust(max(1, width - len(system_line_left)))
        )

        rom_line_right  = f"Rom: {system_rom_index}/{system_rom_total}"
        rom_line_prefix = "ROM    : "
        # Reserve space for the right-justified part (plus a one-space
        # gap) before truncating the rom name itself, so a long filename
        # can't push the counter off the edge of the terminal.
        rom_name_budget = max(
            10, width - len(rom_line_prefix) - len(rom_line_right) - 1
        )
        rom_line_left = rom_line_prefix + self._truncate(rom, rom_name_budget)
        rom_line = (
            rom_line_left +
            rom_line_right.rjust(max(1, width - len(rom_line_left)))
        )

        lines = [
            "",
            magenta + "=" * width + reset,
            bold + self._centre(
                f" ROM Audit Tool v{state.get('version', '')} "
                f" ·  {platform} ", width
            ) + reset,
            magenta + "=" * width + reset,
            system_line,
            rom_line,
        ]

        # Status line — right-justified checksum display if available.
        # Shows the algorithm + hash from the CSV at ROM start (so the
        # user can see what's on record), then appends ✓ or ✗ once
        # verification completes. Same right-justify pattern as
        # System/ROM counters.
        checksum_info   = state.get('checksum_info', '')
        checksum_result = state.get('checksum_result', '')
        status_left = f"Status : {status_colour}{status}{reset}"
        if checksum_info:
            algo, _, hsh = checksum_info.partition(':')
            result_mark = (
                f" {green}✓{reset}" if checksum_result == 'OK'
                else f" {red}✗{reset}" if checksum_result == 'MISMATCH'
                else ''
            )
            cs_right = f"{algo.upper()}: {hsh}{result_mark}"
            #    if len(hsh) > 16 else f"{algo.upper()}: {hsh}{result_mark}"
            # Strip ANSI from left side for length calculation
            status_left_plain = f"Status : {status}"
            status_line = (
                status_left +
                cs_right.rjust(max(1, width - len(status_left_plain)))
            )
        else:
            status_line = status_left

        lines += [
            status_line,
            magenta + "-" * width + reset,
            f"Progress: [{bar}] {tested}/{total} ({pct:5.1f}%)",
            magenta + "-" * width + reset,
            (
                f"{green}{'OK:':<14}{counts.get('OK', 0):<8}{reset}"
                f"{green}{'FIXED:':<14}{counts.get('FIXED', 0):<8}{reset}"
                f"{red}{'ERROR:':<14}{counts.get('ERROR', 0):<8}{reset}"
            ),
            (
                f"{red}{'GENUINE:':<14}{counts.get('GENUINE ERROR', 0):<8}{reset}"
                f"{yellow}{'MISSING CORE:':<14}{counts.get('MISSING CORE', 0):<8}{reset}"
                f"{yellow}{'MISSING BIOS:':<14}{counts.get('MISSING BIOS', 0):<8}{reset}"
            ),
            (
                f"{yellow}{'TIMEOUT:':<14}{counts.get('TIMEOUT', 0):<8}{reset}"
                f"{cyan}{'IMPERFECT:':<14}{counts.get('IMPERFECT', 0):<8}{reset}"
                f"{magenta}{'NEEDS REVIEW:':<14}{counts.get('NEEDS REVIEW', 0):<8}{reset}"
            ),
            f"REMAINING: {remaining}",
            magenta + "-" * width + reset,
            timing_line,
            magenta + "=" * width + reset,
        ]

        log_lines = audit_log.get_log_buffer()
        if log_lines:
            lines.append(magenta + "-" * width + reset)
            lines.append("Recent log:")
            for line in log_lines[-7:]:
                colour, reset_col = self._line_colour(line)
                lines.append(
                    "  " + colour +
                    self._truncate(line, width - 2) +
                    reset_col
                )

        if sys.stdout.isatty():
            print("\033[2J\033[H", end="")

        print("\n".join(lines), flush=True)

    # ------------------------------------------------------------------
    # Colour helpers
    # ------------------------------------------------------------------

    def _ansi_enabled(self) -> bool:
        return sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"

    def _status_colour(self, text: str) -> str:
        upper = str(text).upper()
        if "NEEDS REVIEW" in upper:
            return ANSI_MAGENTA
        if "OK" in upper:
            return ANSI_GREEN
        if "ERROR" in upper:
            return ANSI_RED
        if "MISSING" in upper or "TIMEOUT" in upper:
            return ANSI_YELLOW
        if "LAUNCHED" in upper:
            return ANSI_CYAN
        return ""

    def _line_colour(self, line: str) -> tuple:
        """Return (colour, reset) tuple for a log line."""
        upper = line.upper()
        # Summary / completion lines — checked first so stats line
        # (which contains 'FIXED:') doesn't match the OK/FIXED rules
        if 'AUDIT COMPLETE' in upper:
            return (ANSI_GREEN, ANSI_RESET)
        if ('OK:' in line and '|' in line and 'Error:' in line):
            # Stats summary bar: OK:N | Fixed:N | Error:N ...
            return (ANSI_CYAN, ANSI_RESET)
        if ('Results  :' in line or 'Log file :' in line
                or 'Error logs:' in line or 'Elapsed  :' in line):
            return (ANSI_CYAN, ANSI_RESET)
        # Per-ROM result lines
        if '-> OK' in upper or '-> FIXED' in upper:
            return (ANSI_GREEN, ANSI_RESET)
        if '-> NEEDS REVIEW' in upper:
            return (ANSI_MAGENTA, ANSI_RESET)
        if ('-> ERROR' in upper or '-> GENUINE ERROR' in upper
                or 'ALL CORE COMBINATIONS FAILED' in upper
                or 'ALL FIX COMBINATIONS FAILED' in upper):
            return (ANSI_RED, ANSI_RESET)
        if ('-> MISSING' in upper or '-> TIMEOUT' in upper
                or '-> LAUNCHED' in upper):
            return (ANSI_YELLOW, ANSI_RESET)
        return ('', '')

    # ------------------------------------------------------------------
    # Static utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_time(seconds) -> str:
        """Format a duration in seconds as HH:MM:SS."""
        try:
            seconds = max(0, int(seconds))
        except (TypeError, ValueError):
            return '--:--:--'
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        text = str(text)
        if max_len <= 0:
            return ""
        if len(text) <= max_len:
            return text
        if max_len == 1:
            return "…"
        return text[:max_len - 1] + "…"

    @staticmethod
    def _centre(text: str, width: int) -> str:
        if len(text) >= width:
            return text[:width]
        pad   = width - len(text)
        left  = pad // 2
        right = pad - left
        return " " * left + text + " " * right

    @staticmethod
    def set_terminal_size(cols: int = 120, rows: int = 35) -> None:
        """
        Request the terminal to resize to the specified dimensions.

        Uses the xterm resize escape sequence supported by most terminal
        emulators. Has no effect if stdout is not a TTY.

        Args:
            cols: Desired terminal width in characters.
            rows: Desired terminal height in lines.
        """
        if sys.stdout.isatty():
            print(f"\033[8;{rows};{cols}t", end='', flush=True)

# ---------------------------------------------------------------------------
# ETA calculation
# ---------------------------------------------------------------------------

def calculate_eta(
    recent_times: list,
    roms_remaining: int,
    fallback_secs: float = 20.0
) -> float:
    """
    Calculate estimated time remaining using a rolling average of recent
    test durations.

    Args:
        recent_times:   List of recent ROM test durations in seconds.
        roms_remaining: Number of ROMs still to be tested.
        fallback_secs:  Per-ROM estimate when no timing data exists yet.

    Returns:
        Estimated seconds remaining.
    """
    if roms_remaining <= 0:
        return 0.0
    if not recent_times:
        return fallback_secs * roms_remaining
    avg = sum(recent_times) / len(recent_times)
    return avg * roms_remaining

