"""
Logging utilities for the ROM Audit Tool.

Provides a unified timestamped logger that writes simultaneously to:
    - The terminal (when dashboard is not active)
    - A persistent log file (always, when configured)
    - An in-memory circular buffer (for dashboard display)

The dashboard active flag switches console output off so curses can
own the terminal, while file and buffer output continues uninterrupted.
"""
from __future__ import annotations  # Python 3.7 compatibility

from collections import deque
from datetime import datetime
from typing import Optional
import io


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# File handle for persistent log output. None until setup_log_file() called.
_log_file_handle: Optional[io.TextIOWrapper] = None

# Circular buffer of recent log lines for dashboard display.
# Holds the last LOG_BUFFER_SIZE messages.
_log_buffer: deque = deque(maxlen=50)

# When True, log() suppresses console output so curses owns the terminal.
_dashboard_active: bool = False


# ---------------------------------------------------------------------------
# Setup functions
# ---------------------------------------------------------------------------

def setup_log_file(path: str) -> None:
    """
    Open a persistent log file for append-mode writing.

    Should be called once at startup before any log() calls. All
    subsequent log() calls will write to this file in addition to
    the console and buffer. The file is opened in append mode so
    multiple audit runs accumulate in the same log.

    Args:
        path: Full path to the log file to open or create.
    """
    global _log_file_handle
    try:
        _log_file_handle = open(path, 'a', encoding='utf-8')
    except Exception as e:
        print(f"WARNING: Could not open log file {path}: {e}", flush=True)


def set_dashboard_active(active: bool) -> None:
    """
    Set whether the curses dashboard is currently owning the terminal.

    When True, log() suppresses console print() calls to prevent
    curses display corruption. File and buffer output is unaffected.

    Args:
        active: True when curses dashboard is active, False otherwise.
    """
    global _dashboard_active
    _dashboard_active = active


def get_log_buffer() -> list[str]:
    """
    Return a sanitized copy of the current log buffer for terminal display.

    Used by the dashboard to display recent log lines below the
    statistics panel. Returns lines in chronological order.

    Strips ANSI escape sequences and other non-printable characters
    before returning — emulator log content is passed through the log
    buffer and ultimately displayed on the SSH terminal, and a crafted
    ROM or emulator output could in principle inject ANSI sequences that
    manipulate the terminal (change title, reposition cursor, etc.).
    The persistent log *file* is written directly by log() and is not
    affected by this sanitization — only the dashboard display path is.

    Returns:
        List of recent log line strings, oldest first, safe for terminal
        display.
    """
    import re as _re
    _ansi_escape = _re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    def _strip(line: str) -> str:
        line = _ansi_escape.sub('', line)
        # Strip remaining non-printable characters (keep tabs/newlines for
        # readability but remove anything else below 0x20 and DEL 0x7f)
        return ''.join(
            ch for ch in line
            if ch == '\t' or (0x20 <= ord(ch) < 0x7f) or ord(ch) > 0x9f
        )
    return [_strip(line) for line in _log_buffer]


def close_log_file() -> None:
    """
    Flush and close the persistent log file.

    Should be called on clean exit to ensure all buffered writes
    are flushed to disk.
    """
    global _log_file_handle
    if _log_file_handle:
        try:
            _log_file_handle.flush()
            _log_file_handle.close()
        except Exception:
            pass
        _log_file_handle = None


# ---------------------------------------------------------------------------
# Core log function
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    """
    Print a timestamped message to all configured outputs.

    Writes to:
        - Console via print() (only when dashboard is not active)
        - Persistent log file (when setup_log_file() has been called)
        - In-memory circular buffer (always, for dashboard display)

    Args:
        message: The message to log.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {message}"

    # Add to circular buffer for dashboard display
    _log_buffer.append(formatted)

    # Write to console only when dashboard is not owning the terminal
    if not _dashboard_active:
        print(formatted, flush=True)

    # Write to persistent log file if configured
    if _log_file_handle:
        try:
            _log_file_handle.write(formatted + '\n')
            _log_file_handle.flush()
        except Exception:
            pass
