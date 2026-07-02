"""
Platform detection and log analysis utilities for the ROM Audit Tool.

Provides the core detection algorithms used across all platform
implementations. Platform-specific matching criteria (launch indicators,
error markers) are injected by the caller, keeping the algorithms
in one place while the data remains platform-specific.
"""

from __future__ import annotations  # Python 3.7 compatibility

import os
from modules.common.logging import log


def detect_platform():
    """
    Detect the emulation platform from the current environment.

    Checks for platform-specific files and directories to determine
    which emulation system is running. Returns an appropriate Platform
    instance ready for use.

    To add support for a new platform, import its class here and add
    a detection condition before the RuntimeError.

    Returns:
        A Platform instance appropriate for the detected system.

    Raises:
        RuntimeError: If no supported platform is detected.
    """
    from modules.platforms.batocera import BatoceraPlaftorm
    from modules.platforms.retropie import RetroPiePlatform
    from modules.platforms.recalbox import RecalboxPlatform

    if os.path.exists('/usr/bin/batocera-version') or \
       os.path.exists('/userdata/system'):
        log("Detected platform: Batocera")
        return BatoceraPlaftorm()

    # Recalbox checked before RetroPie — both may have some common paths
    # on some installations. recalbox.conf is definitive.
    if os.path.exists('/recalbox/share/system/recalbox.conf'):
        log("Detected platform: Recalbox")
        return RecalboxPlatform()

    if os.path.exists('/opt/retropie') and \
       os.path.exists('/home/pi/RetroPie'):
        log("Detected platform: RetroPie")
        return RetroPiePlatform()

    raise RuntimeError(
        "Unsupported platform. Could not detect Batocera, Recalbox "
        "or RetroPie."
    )


def is_launched(content: str, indicators: list[str]) -> bool:
    """
    Determine whether an emulator was successfully launched.

    Checks the log content for any of the provided launch indicator
    strings. The specific indicators are supplied by the platform
    implementation, keeping the matching algorithm in common while
    the matching criteria remain platform-specific.

    Args:
        content:    Log file content to search.
        indicators: List of strings that indicate a successful launch.

    Returns:
        True if any indicator is found in the content.
    """
    return any(indicator in content for indicator in indicators)


def parse_error(
    stdout: str,
    stderr: str,
    error_markers: list[str],
    exit_marker: str,
    non_fatal: list[str] | None = None
) -> tuple[str, str]:
    """
    Attempt to extract an error status and description from log content.

    Checks for a known exit failure marker in stdout, then scans stderr
    for a specific error message matching any of the provided markers.
    The markers are supplied by the platform implementation.

    If exit_marker is an empty string (as used by RetroPie which has no
    Python-style exit message), the exit marker check is skipped and
    error detection relies solely on error_markers appearing in the logs.

    Args:
        stdout:        Contents of the stdout log.
        stderr:        Contents of the stderr log.
        error_markers: List of strings that identify an error line.
        exit_marker:   String indicating a non-zero exit. Empty string
                       disables this check (used by RetroPie).

    Returns:
        Tuple of (status, notes) where status is "ERROR" and notes
        contains the matching error line if an error was found,
        or (None, "") if no error was detected.
    """
    non_fatal = non_fatal or []

    def _is_fatal(line: str) -> bool:
        if not any(m in line for m in error_markers):
            return False
        # Non-fatal markers are warnings that match error patterns but
        # do not indicate a real failure (e.g. MAME samples path warnings)
        if any(nf in line for nf in non_fatal):
            return False
        return True

    # Only check exit marker if the platform provides one
    if exit_marker and exit_marker in stdout:
        for line in stderr.splitlines():
            if _is_fatal(line):
                return "ERROR", line.strip()[:80]
        return "ERROR", "Non-zero exit from launcher"

    # Check both stdout and stderr directly for error markers.
    # For RetroPie stdout and stderr are the same runcommand.log file,
    # so checking both is harmless and covers all platforms.
    for log_content in (stdout, stderr):
        for line in log_content.splitlines():
            if _is_fatal(line):
                return "ERROR", line.strip()[:80]

    return None, ""
