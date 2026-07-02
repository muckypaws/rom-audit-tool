#!/usr/bin/env python3
"""
romclean.py - Remove duplicate ROM files where the same stem exists in
              multiple formats (e.g. Zaxxon.7z and Zaxxon.zip).

Pure Python 3 stdlib only - no pip/external modules required.

Works on:
  - macOS, Linux, Batocera, Recalbox, RetroPie, Windows

Usage:
  python3 romclean.py /path/to/roms             # interactive per-conflict
  python3 romclean.py /path/to/roms --keep 7z   # keep .7z, delete .zip
  python3 romclean.py /path/to/roms --keep zip  # keep .zip, delete .7z
  python3 romclean.py /path/to/roms --dry-run   # preview, no deletes
  python3 romclean.py /path/to/roms --recursive # recurse subdirectories
"""

from __future__ import annotations

import io as _io
import sys
import os
import argparse
from pathlib import Path
from collections import defaultdict
from typing import Optional

# Force line-buffered output so redirected files populate immediately
sys.stdout = _io.TextIOWrapper(
    sys.stdout.buffer, encoding=sys.stdout.encoding,
    errors=sys.stdout.errors, line_buffering=True
)
sys.stderr = _io.TextIOWrapper(
    sys.stderr.buffer, encoding=sys.stderr.encoding,
    errors=sys.stderr.errors, line_buffering=True
)

# ─────────────────────────────────────────────────────────────────────────────
# Extensions considered for duplicate detection
# ─────────────────────────────────────────────────────────────────────────────

CONFLICT_EXTENSIONS: frozenset = frozenset({
    ".zip", ".7z", ".gz", ".bz2", ".xz", ".rar",
    ".chd", ".rvz", ".wbfs",
    ".bin", ".rom",
    ".a26", ".a52", ".a78",
    ".nes", ".fds",
    ".sfc", ".smc",
    ".n64", ".z64", ".v64",
    ".gb", ".gbc", ".gba",
    ".nds", ".3ds",
    ".md", ".smd", ".gen", ".32x", ".gg", ".sms",
    ".pce", ".ws", ".wsc",
    ".lnx", ".ngp", ".ngc", ".ngpc",
    ".iso", ".img", ".cue", ".gdi",
})

# ─────────────────────────────────────────────────────────────────────────────
# Scanning
# ─────────────────────────────────────────────────────────────────────────────

def find_conflicts(base, recursive):
    pattern = "**/*" if recursive else "*"
    by_dir_stem = defaultdict(lambda: defaultdict(list))

    for p in sorted(base.glob(pattern)):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in CONFLICT_EXTENSIONS:
            continue
        by_dir_stem[p.parent][p.stem].append(p)

    result = {}
    for directory, stems in by_dir_stem.items():
        conflicts = {
            stem: files
            for stem, files in stems.items()
            if len(files) > 1
        }
        if conflicts:
            result[directory] = conflicts
    return result

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _format_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "{:.1f} {}".format(n, unit)
        n /= 1024
    return "{:.1f} TB".format(n)


def _file_info(p):
    try:
        size = p.stat().st_size
        return "{}  ({})".format(p.name, _format_size(size))
    except OSError:
        return p.name

# ─────────────────────────────────────────────────────────────────────────────
# Decision
# ─────────────────────────────────────────────────────────────────────────────

def decide_keep(files, keep_ext, dry_run, auto_all):
    """Return (keep_list, delete_list)."""
    ext_map = {}
    for f in files:
        ext_map[f.suffix.lower().lstrip(".")] = f

    # --keep flag
    if keep_ext:
        bare = keep_ext.lower().lstrip(".")
        if bare in ext_map:
            keep   = [ext_map[bare]]
            delete = [f for f in files if f != keep[0]]
            return keep, delete
        return files, []

    # Apply-to-all from earlier interactive choice
    if "ext" in auto_all:
        preferred = auto_all["ext"]
        if preferred in ext_map:
            keep   = [ext_map[preferred]]
            delete = [f for f in files if f != keep[0]]
            return keep, delete

    # Interactive
    print()
    print("  Conflict:")
    sorted_files = sorted(files)
    for i, f in enumerate(sorted_files, 1):
        print("    [{}] {}".format(i, _file_info(f)))

    exts = sorted(ext_map.keys())

    while True:
        opts = "  ".join("[{}]".format(e) for e in exts)
        sys.stdout.write("  Keep which?  {}  [a]ll  [s]kip  or number: ".format(opts))
        sys.stdout.flush()
        try:
            choice = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if choice in ("s", "skip"):
            return files, []

        if choice in ("a", "all"):
            return files, []

        # Number
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(sorted_files):
                keep       = [sorted_files[idx]]
                delete     = [f for f in sorted_files if f != keep[0]]
                kept_ext   = keep[0].suffix.lower().lstrip(".")
                sys.stdout.write(
                    "  Apply '.{}' to all remaining conflicts? [y/n]: ".format(kept_ext)
                )
                sys.stdout.flush()
                try:
                    ans = input().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    sys.exit(0)
                if ans == "y":
                    auto_all["ext"] = kept_ext
                return keep, delete

        # Extension name
        bare = choice.lstrip(".")
        if bare in ext_map:
            keep     = [ext_map[bare]]
            delete   = [f for f in files if f != keep[0]]
            kept_ext = bare
            sys.stdout.write(
                "  Apply '.{}' to all remaining conflicts? [y/n]: ".format(kept_ext)
            )
            sys.stdout.flush()
            try:
                ans = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(0)
            if ans == "y":
                auto_all["ext"] = kept_ext
            return keep, delete

        print("  Invalid choice, try again.")

# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Remove duplicate ROM files where the same stem exists in\n"
            "multiple formats (e.g. Zaxxon.7z and Zaxxon.zip).\n\n"
            "Without --keep the script prompts interactively for each conflict.\n"
            "The first interactive choice can be applied to all remaining conflicts."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive - prompts for each conflict:
  python3 romclean.py /Volumes/RetroGaming/roms/mame

  # Keep all .7z, delete .zip duplicates:
  python3 romclean.py /Volumes/RetroGaming/roms/mame --keep 7z

  # Keep all .zip, delete .7z duplicates:
  python3 romclean.py /Volumes/RetroGaming/roms/mame --keep zip

  # Preview without deleting:
  python3 romclean.py /Volumes/RetroGaming/roms/mame --keep 7z --dry-run

  # Recurse into all subdirectories:
  python3 romclean.py /Volumes/RetroGaming/roms --keep 7z --recursive

  # Save full report to file:
  python3 romclean.py /path/to/roms --keep 7z --dry-run > report.txt 2>&1
        """
    )

    parser.add_argument("path",
                        help="Directory to scan for duplicate ROM files")
    parser.add_argument("--keep", metavar="EXT",
                        help="Extension to keep (e.g. '7z' or 'zip'). "
                             "Omit to be prompted interactively.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only - no files are deleted")
    parser.add_argument("--recursive", action="store_true",
                        help="Recurse into subdirectories")

    args     = parser.parse_args()
    base     = Path(args.path)
    dry_run  = args.dry_run
    keep_ext = args.keep.lower().lstrip(".") if args.keep else None

    if not base.is_dir():
        print("ERROR: '{}' is not a directory or does not exist.".format(base))
        sys.exit(1)

    print("\nromclean - scanning: {}".format(base))
    if args.recursive:
        print("  Mode     : recursive")
    if keep_ext:
        print("  Keep     : .{}".format(keep_ext))
    if dry_run:
        print("  Dry run  : yes (no files will be deleted)")
    print()

    conflicts = find_conflicts(base, args.recursive)

    if not conflicts:
        print("No duplicate stems found. Nothing to do.")
        return

    total_conflicts = sum(len(stems) for stems in conflicts.values())
    print("Found {} conflicting stem(s) across {} director{}.".format(
        total_conflicts,
        len(conflicts),
        "y" if len(conflicts) == 1 else "ies"
    ))
    print()

    auto_all      = {}
    total_deleted = 0
    total_kept    = 0
    total_skipped = 0
    deleted_files = []

    for directory in sorted(conflicts.keys()):
        try:
            rel = directory.relative_to(base)
        except ValueError:
            rel = directory
        rel_str = str(rel) if str(rel) != "." else "(root)"
        print("-- {} {}".format(rel_str, "-" * max(1, 50 - len(rel_str))))

        for stem in sorted(conflicts[directory].keys()):
            files = sorted(conflicts[directory][stem])
            print("  {}".format(stem))

            keep, delete = decide_keep(files, keep_ext, dry_run, auto_all)

            for f in keep:
                print("    KEEP    {}".format(_file_info(f)))
                total_kept += 1

            if not delete:
                print("    SKIPPED (no files removed)")
                total_skipped += 1
            else:
                for f in delete:
                    if dry_run:
                        print("    [DRY-RUN] Would delete: {}".format(_file_info(f)))
                        total_deleted += 1
                    else:
                        try:
                            f.unlink()
                            print("    DELETED {}".format(_file_info(f)))
                            deleted_files.append(f)
                            total_deleted += 1
                        except OSError as exc:
                            print("    ERROR deleting {}: {}".format(f.name, exc))

    tag = "[DRY-RUN] " if dry_run else ""
    print("\n" + "=" * 54)
    print("{}Complete.".format(tag))
    print("  Conflicts found   : {}".format(total_conflicts))
    print("  Files kept        : {}".format(total_kept))
    print("  Files deleted     : {}".format(total_deleted))
    print("  Conflicts skipped : {}".format(total_skipped))

    if deleted_files:
        print("\nDeleted files:")
        for f in deleted_files:
            print("  {}".format(f))


if __name__ == "__main__":
    main()
