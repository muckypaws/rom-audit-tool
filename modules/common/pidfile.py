"""
PID file management for the ROM Audit Tool.

Prevents multiple audit instances running simultaneously by writing the
current process ID to a known file on startup and removing it on clean
exit. On startup, if a PID file exists, the process it refers to is
checked to determine whether it is still running. Stale PID files left
by a previous crash are detected and removed automatically.
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import sys

from modules.common.logging import log


def write_pid(pid_file: str) -> None:
    """
    Write the current process ID to the PID file.

    Should be called once at startup after the existing PID check
    has confirmed no other instance is running.

    Args:
        pid_file: Full path to the PID file to create.
    """
    try:
        with open(pid_file, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        log(f"WARNING: Could not write PID file {pid_file}: {e}")


def remove_pid(pid_file: str) -> None:
    """
    Remove the PID file on clean exit.

    Safe to call even if the PID file does not exist (e.g. if startup
    failed before the file was written). Called in a finally block to
    ensure cleanup happens even after exceptions or KeyboardInterrupt.

    Args:
        pid_file: Full path to the PID file to remove.
    """
    try:
        if os.path.exists(pid_file):
            os.remove(pid_file)
    except Exception as e:
        log(f"WARNING: Could not remove PID file {pid_file}: {e}")


def check_running(pid_file: str) -> None:
    """
    Check whether another audit instance is already running.

    If a PID file exists:
        - The stored PID is checked using os.kill(pid, 0) which tests
          process existence without sending an actual signal.
        - If the process is running, logs an error and exits.
        - If the process is not running (stale PID file from a crash),
          logs a warning, removes the stale file, and continues.

    If no PID file exists, returns normally.

    Args:
        pid_file: Full path to the PID file to check.
    """
    if not os.path.exists(pid_file):
        return

    try:
        with open(pid_file, 'r') as f:
            existing_pid = int(f.read().strip())
    except (ValueError, IOError):
        # Unreadable or malformed PID file - treat as stale
        log(f"WARNING: Unreadable PID file found at {pid_file}, removing.")
        remove_pid(pid_file)
        return

    try:
        # Signal 0 checks process existence without sending a real signal
        os.kill(existing_pid, 0)
        # Process exists - another instance is running
        log(f"ERROR: An audit is already running (PID {existing_pid}).")
        log(f"       Attach to it with: tmux attach -s romaudit")
        log(f"       If this is incorrect, delete {pid_file} and retry.")
        sys.exit(1)

    except ProcessLookupError:
        # Process does not exist - stale PID file from a previous crash
        log(f"WARNING: Stale PID file found (PID {existing_pid} is gone).")
        log(f"         Previous run may have crashed. Continuing.")
        remove_pid(pid_file)

    except PermissionError:
        # Process exists but we can't signal it - treat as running
        log(f"ERROR: An audit may already be running (PID {existing_pid}).")
        log(f"       If this is incorrect, delete {pid_file} and retry.")
        sys.exit(1)
