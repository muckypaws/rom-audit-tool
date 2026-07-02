#!/usr/bin/env python3

import ast
import argparse
from collections import defaultdict
from pathlib import Path


def check_file(path: Path, quiet: bool = False) -> tuple[bool, int]:
    """
    Returns:
        has_issue: True if duplicates or parse/read errors were found
        duplicate_count: number of duplicate function names found
    """

    if path.name == "__init__.py":
        return False, 0

    if not quiet:
        print(f"\nChecking: {path}")

    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        print(f"\nChecking: {path}")
        print(f"  ERROR: Syntax error on line {exc.lineno}: {exc.msg}")
        return True, 0
    except UnicodeDecodeError:
        print(f"\nChecking: {path}")
        print("  ERROR: Could not decode as UTF-8")
        return True, 0
    except OSError as exc:
        print(f"\nChecking: {path}")
        print(f"  ERROR: Could not read file: {exc}")
        return True, 0

    defs = defaultdict(list)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[node.name].append(node.lineno)

    duplicates = {
        name: lines
        for name, lines in defs.items()
        if len(lines) > 1
    }

    if duplicates:
        if quiet:
            print(f"\nChecking: {path}")

        for name, lines in sorted(duplicates.items()):
            line_list = ", ".join(str(line) for line in lines)
            print(f"  DUPLICATE: {name} lines {line_list}")

        return True, len(duplicates)

    if not quiet:
        print("  OK")

    return False, 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Python files for duplicate function definitions."
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Python files to check",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only show files with duplicates or errors",
    )

    args = parser.parse_args()

    files_checked = 0
    files_with_issues = 0
    duplicate_defs = 0

    for filename in args.files:
        path = Path(filename)

        if path.name == "__init__.py":
            continue

        files_checked += 1

        has_issue, dup_count = check_file(path, quiet=args.quiet)

        if has_issue:
            files_with_issues += 1

        duplicate_defs += dup_count

    print("\n" + "-" * 40)
    print(f"Files checked     : {files_checked}")
    print(f"Files with issues : {files_with_issues}")
    print(f"Duplicate defs    : {duplicate_defs}")

    return 1 if files_with_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())