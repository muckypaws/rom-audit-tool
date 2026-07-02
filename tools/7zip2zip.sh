#!/usr/bin/env bash

set -euo pipefail

DELETE_ORIGINAL=true

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 file.7z|directory [more files/directories...]"
    exit 1
fi

if command -v 7z >/dev/null 2>&1; then
    SEVENZIP="7z"
elif command -v 7zr >/dev/null 2>&1; then
    SEVENZIP="7zr"
else
    echo "Error: neither 7z nor 7zr is installed."
    exit 1
fi

command -v zip >/dev/null 2>&1 || { echo "Error: zip not installed"; exit 1; }
command -v unzip >/dev/null 2>&1 || { echo "Error: unzip not installed"; exit 1; }

echo "Using: $SEVENZIP"

convert_one() {
    local sevenzfile="$1"

    case "$sevenzfile" in
        *.7z|*.7Z) ;;
        *) return ;;
    esac

    local sevenz_abs dir base name output tmpdir

    sevenz_abs="$(cd "$(dirname "$sevenzfile")" && pwd)/$(basename "$sevenzfile")"
    dir="$(dirname "$sevenz_abs")"
    base="$(basename "$sevenz_abs")"
    name="${base%.*}"
    output="$dir/$name.zip"

    if [ -f "$output" ]; then
        echo "Skipping, ZIP already exists: $output"
        return
    fi

    tmpdir="$(mktemp -d)"

    echo "Converting: $sevenz_abs"
    echo "Output:     $output"

    "$SEVENZIP" x "$sevenz_abs" -o"$tmpdir" >/dev/null

    (
        cd "$tmpdir"
        zip -qr "$output" .
    )

    if unzip -tq "$output" >/dev/null 2>&1; then
        echo "Verified:   $output"

        if [ "$DELETE_ORIGINAL" = true ]; then
            rm -f "$sevenz_abs"
            echo "Deleted:    $sevenz_abs"
        fi
    else
        echo "FAILED verification: $output"
        rm -f "$output"
    fi

    rm -rf "$tmpdir"
    echo
}

for input in "$@"; do
    if [ -d "$input" ]; then
        find "$input" -type f -iname "*.7z" -print0 |
        while IFS= read -r -d '' file; do
            convert_one "$file"
        done
    elif [ -f "$input" ]; then
        convert_one "$input"
    else
        echo "Skipping: $input does not exist"
    fi
done