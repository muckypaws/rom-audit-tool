# Check Duplicate Python Function Definitions

This utility checks Python source files for duplicate function or method
names.

It is useful for catching accidental duplicate `def` blocks, for
example:

``` python
def pre_audit(self) -> None:
    ...

def pre_audit(self) -> None:
    ...
```

In Python, the later definition silently replaces the earlier one in the
same scope. This can make bugs difficult to spot.

## Files

``` text
check_duplicate_defs.py
README_CheckDuplicateDefs.md
```

## Usage

Check a single file:

``` bash
python3 check_duplicate_defs.py path/to/file.py
```

Check all Python files in the current project, excluding `__init__.py`:

``` bash
find . -name "*.py" ! -name "__init__.py" -exec python3 check_duplicate_defs.py {} +
```

Quiet mode:

``` bash
find . -name "*.py" ! -name "__init__.py" -exec python3 check_duplicate_defs.py --quiet {} +
```

## Example Output

``` text
Checking: ./scripts/rom_audit.py
  DUPLICATE: pre_audit lines 102, 487

Checking: ./scripts/helpers.py
  OK

----------------------------------------
Files checked     : 38
Files with issues : 1
Duplicate defs    : 1
```

## Exit Codes

    Exit code Meaning
  ----------- ---------------------------------------------------
          `0` No duplicate definitions found
          `1` Duplicate definitions or read/syntax errors found

## Notes

The script uses Python's built-in `ast` module rather than regular
expressions, so it correctly detects:

-   Normal functions
-   Class methods
-   `async def` functions
-   Multi-line function signatures
-   Decorated functions

It ignores comments and strings, making it much more reliable than text
matching.

When used with the recommended `find` command, `__init__.py` files are
excluded automatically.

## Summary

This utility is intended as a lightweight sanity check for Python
projects to help identify accidental duplicate function definitions
before they become difficult-to-find bugs.
