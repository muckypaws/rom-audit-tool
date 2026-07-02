#!/usr/bin/env bash
# =============================================================================
# bios_finder_recalbox.sh
#
# Parses a Recalbox missing_bios_report.txt, searches a local directory tree
# for the missing BIOS files, verifies MD5 signatures (multiple valid MD5s
# per file are supported), stages matches into a destination directory with
# the correct Recalbox folder structure, and generates a deploy script to
# copy them into place on the Recalbox itself.
#
# HOW TO OBTAIN THE REPORT:
#   Recalbox writes /recalbox/share/bios/missing_bios_report.txt when its
#   BIOS checker runs. Fetch it via the network share (\\RECALBOX\share\bios)
#   or SFTP, then point this script at it.
#
# COMPATIBILITY:
#   bash 3.2+ (stock macOS) and modern Linux. No associative arrays,
#   no mapfile, no ${var,,} lowercasing. Uses md5 (macOS) or md5sum (Linux).
#
# USAGE:
#   ./bios_finder_recalbox.sh <missing_bios_report.txt> [options]
#
# OPTIONS:
#   --required-only     Ignore OPTIONAL BIOS entries
#   --hash-scan         Second pass: hash unmatched-by-name candidates so
#                       misnamed files can be found purely by MD5
#   --max-size <MB>     Size cap for hash-scan candidates (default 16 MB)
#   --help              This text
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colour helpers (degrade gracefully if not a TTY)
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    colour_red()    { printf '\033[31m%s\033[0m' "$1"; }
    colour_green()  { printf '\033[32m%s\033[0m' "$1"; }
    colour_yellow() { printf '\033[33m%s\033[0m' "$1"; }
    colour_bold()   { printf '\033[1m%s\033[0m'  "$1"; }
else
    colour_red()    { printf '%s' "$1"; }
    colour_green()  { printf '%s' "$1"; }
    colour_yellow() { printf '%s' "$1"; }
    colour_bold()   { printf '%s' "$1"; }
fi

# ---------------------------------------------------------------------------
# Portable MD5 (md5sum on Linux, md5 -q on macOS)
# ---------------------------------------------------------------------------
if command -v md5sum >/dev/null 2>&1; then
    md5_of() { md5sum "$1" 2>/dev/null | awk '{print $1}'; }
elif command -v md5 >/dev/null 2>&1; then
    md5_of() { md5 -q "$1" 2>/dev/null; }
else
    echo "ERROR: neither md5sum nor md5 found on this system." >&2
    exit 1
fi

lc() { tr 'A-Z' 'a-z'; }

# Portable file size in bytes (GNU stat vs BSD stat)
file_size() {
    if stat --version >/dev/null 2>&1; then
        stat -c '%s' "$1"
    else
        stat -f '%z' "$1"
    fi
}

# ---------------------------------------------------------------------------
# Globals. Parallel indexed arrays, one index per BIOS entry
# (bash 3.2 has no associative arrays).
# ---------------------------------------------------------------------------
ENTRY_COUNT=0
E_NAME=()       # filename only, e.g. IPLROM.X1
E_REL=()        # path relative to the bios root, e.g. xmil/IPLROM.X1
E_REQ=()        # REQUIRED or OPTIONAL
E_MD5S=()       # space-separated list of acceptable MD5s (lowercase), may be empty
E_STATUS=()     # NOTFOUND / OK / MISMATCH

COUNT_OK=0
COUNT_MISMATCH=0
COUNT_NOTFOUND=0

MISMATCH_WARNINGS=()
NOT_FOUND_LIST=()
DEPLOY_LINES=()

REQUIRED_ONLY=0
HASH_SCAN=0
MAX_SIZE_MB=16

DEFAULTS_FILE=".bios_finder_recalbox_defaults"
SEARCH_ROOT=""
DEST_DIR=""
DEPLOY_SCRIPT=""
FILE_INDEX=""

cleanup() { [[ -n "${FILE_INDEX:-}" && -f "$FILE_INDEX" ]] && rm -f "$FILE_INDEX" "$FILE_INDEX.md5" 2>/dev/null || true; }
trap cleanup EXIT

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

# ---------------------------------------------------------------------------
# Manifest parser for missing_bios_report.txt
#
# Expected block shape (blocks separated by new "... BIOS:" lines):
#   MISSING REQUIRED BIOS: IPLROM.X1
#   Path: /recalbox/share/bios/xmil/IPLROM.X1
#   For: xmil
#   Possible MD5 List:
#   EEEEA1CD29C6E0E8B094790AE969BFA7
#   59074727A953FE965109B7DBE3298E30
# ---------------------------------------------------------------------------
CUR_NAME=""
CUR_REL=""
CUR_REQ=""
CUR_MD5S=""
IN_MD5_LIST=0

finish_entry() {
    if [[ -n "$CUR_NAME" ]]; then
        if [[ "$REQUIRED_ONLY" -eq 1 && "$CUR_REQ" != "REQUIRED" ]]; then
            : # skip optional entry
        else
            # Fall back to bare filename if no Path: line was present
            [[ -z "$CUR_REL" ]] && CUR_REL="$CUR_NAME"
            E_NAME[$ENTRY_COUNT]="$CUR_NAME"
            E_REL[$ENTRY_COUNT]="$CUR_REL"
            E_REQ[$ENTRY_COUNT]="$CUR_REQ"
            E_MD5S[$ENTRY_COUNT]="$CUR_MD5S"
            E_STATUS[$ENTRY_COUNT]="NOTFOUND"
            ENTRY_COUNT=$(( ENTRY_COUNT + 1 ))
        fi
    fi
    CUR_NAME=""; CUR_REL=""; CUR_REQ=""; CUR_MD5S=""; IN_MD5_LIST=0
}

parse_manifest() {
    local manifest="$1" raw line p m
    while IFS= read -r raw || [[ -n "$raw" ]]; do
        line="${raw%$'\r'}"                      # tolerate CRLF
        case "$line" in
            *"BIOS: "*)
                finish_entry
                CUR_REQ="OPTIONAL"
                case "$line" in *REQUIRED*) CUR_REQ="REQUIRED" ;; esac
                CUR_NAME="${line##*BIOS: }"
                ;;
            "Path: "*)
                p="${line#Path: }"
                CUR_REL="${p#/recalbox/share/bios/}"
                IN_MD5_LIST=0
                ;;
            "Possible MD5 List:"*)
                IN_MD5_LIST=1
                ;;
            "For: "*|"Notes: "*|"SYSTEM:"*|----*|"")
                IN_MD5_LIST=0
                ;;
            *)
                if [[ "$IN_MD5_LIST" -eq 1 ]]; then
                    m=$(printf '%s' "$line" | tr -d '[:space:]' | lc)
                    if [[ "$m" =~ ^[0-9a-f]{32}$ ]]; then
                        CUR_MD5S="$CUR_MD5S $m"
                    else
                        IN_MD5_LIST=0
                    fi
                fi
                ;;
        esac
    done < "$manifest"
    finish_entry

    echo "  Format: Recalbox missing_bios_report.txt"
    echo "  Parsed $ENTRY_COUNT entries (required-only: $REQUIRED_ONLY)"
}

# ---------------------------------------------------------------------------
# Helpers for matching
# ---------------------------------------------------------------------------
md5_in_list() {
    # $1 = candidate md5, $2 = space-separated accepted list (may be empty)
    local cand="$1" list="$2" m
    [[ -z "$list" ]] && return 1
    for m in $list; do
        [[ "$cand" == "$m" ]] && return 0
    done
    return 1
}

stage_file() {
    # $1 = source file, $2 = relative dest path
    local src="$1" rel="$2" dest
    dest="$DEST_DIR/$rel"
    mkdir -p "$(dirname "$dest")"
    if [[ -e "$dest" ]]; then
        return 0   # already staged (duplicate manifest entry), skip silently
    fi
    cp "$src" "$dest"
    DEPLOY_LINES+=( "mkdir -p \"\$BIOS_ROOT/$(dirname "$rel")\" && cp -v \"./$rel\" \"\$BIOS_ROOT/$rel\"" )
}

# ---------------------------------------------------------------------------
# Pass 1: search by filename (case-insensitive), verify MD5
# ---------------------------------------------------------------------------
search_and_copy() {
    local i name rel md5s esc cand cmd5 matched
    echo ""
    echo "$(colour_bold 'Indexing files…') (this may take a while for large drives)"
    FILE_INDEX=$(mktemp)
    find "$SEARCH_ROOT" -type f 2>/dev/null | grep -v -F "$DEST_DIR" > "$FILE_INDEX" || true
    echo "  Indexed $(wc -l < "$FILE_INDEX" | tr -d ' ') files under $SEARCH_ROOT"
    echo ""
    echo "$(colour_bold 'Scanning…')"

    i=0
    while [[ $i -lt $ENTRY_COUNT ]]; do
        name="${E_NAME[$i]}"
        rel="${E_REL[$i]}"
        md5s="${E_MD5S[$i]}"
        matched=0

        # Escape regex metacharacters in the filename, anchor to end of path
        esc=$(printf '%s' "$name" | sed 's/[].[^$\\*+?(){}|]/\\&/g')

        # First pass over candidates: exact MD5 match wins
        while IFS= read -r cand; do
            [[ -z "$cand" ]] && continue
            cmd5=$(md5_of "$cand" | lc)
            if md5_in_list "$cmd5" "$md5s"; then
                stage_file "$cand" "$rel"
                E_STATUS[$i]="OK"
                COUNT_OK=$(( COUNT_OK + 1 ))
                printf '  [%-8s] %-44s %s\n' "${E_REQ[$i]}" "$name" "$(colour_green 'FOUND  ✓ MD5 OK')"
                matched=1
                break
            fi
        done < <(grep -iE "/${esc}\$" "$FILE_INDEX" || true)

        # Second pass: name matches but no MD5 matched, copy best-effort
        if [[ $matched -eq 0 ]]; then
            cand=$(grep -iE "/${esc}\$" "$FILE_INDEX" | head -n 1 || true)
            if [[ -n "$cand" ]]; then
                stage_file "$cand" "$rel"
                E_STATUS[$i]="MISMATCH"
                COUNT_MISMATCH=$(( COUNT_MISMATCH + 1 ))
                MISMATCH_WARNINGS+=( "$rel  (from: $cand)" )
                printf '  [%-8s] %-44s %s\n' "${E_REQ[$i]}" "$name" "$(colour_yellow 'FOUND  ⚠ MD5 MISMATCH – copied anyway')"
                matched=1
            fi
        fi

        if [[ $matched -eq 0 ]]; then
            E_STATUS[$i]="NOTFOUND"
        fi
        i=$(( i + 1 ))
    done
}

# ---------------------------------------------------------------------------
# Pass 2 (optional, --hash-scan): find misnamed files purely by MD5.
# Hashes every indexed file under the size cap once, then matches against
# the wanted-MD5 list of any entry still NOTFOUND.
# ---------------------------------------------------------------------------
hash_scan() {
    local i f sz cmd5 md5s rel name found_any
    echo ""
    echo "$(colour_bold 'Hash scan…') (files ≤ ${MAX_SIZE_MB} MB, misnamed BIOS detection)"

    local cap=$(( MAX_SIZE_MB * 1024 * 1024 ))
    : > "$FILE_INDEX.md5"
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        sz=$(file_size "$f" 2>/dev/null || echo "$(( cap + 1 ))")
        [[ "$sz" -gt "$cap" ]] && continue
        cmd5=$(md5_of "$f" | lc)
        [[ -n "$cmd5" ]] && printf '%s %s\n' "$cmd5" "$f" >> "$FILE_INDEX.md5"
    done < "$FILE_INDEX"

    i=0
    while [[ $i -lt $ENTRY_COUNT ]]; do
        if [[ "${E_STATUS[$i]}" == "NOTFOUND" && -n "${E_MD5S[$i]}" ]]; then
            name="${E_NAME[$i]}"
            rel="${E_REL[$i]}"
            found_any=""
            for m in ${E_MD5S[$i]}; do
                found_any=$(grep -m1 "^$m " "$FILE_INDEX.md5" | cut -d' ' -f2- || true)
                [[ -n "$found_any" ]] && break
            done
            if [[ -n "$found_any" ]]; then
                stage_file "$found_any" "$rel"
                E_STATUS[$i]="OK"
                COUNT_OK=$(( COUNT_OK + 1 ))
                printf '  [%-8s] %-44s %s\n' "${E_REQ[$i]}" "$name" "$(colour_green "FOUND BY HASH ✓ (was: $(basename "$found_any"))")"
            fi
        fi
        i=$(( i + 1 ))
    done
}

# ---------------------------------------------------------------------------
# Deploy script generation (run ON the Recalbox after SFTP transfer)
# ---------------------------------------------------------------------------
generate_deploy_script() {
    cat > "$DEPLOY_SCRIPT" <<'DEPLOY_HEADER'
#!/bin/bash
# =============================================================================
# deploy_bios.sh, generated by bios_finder_recalbox.sh
#
# USAGE (on the Recalbox, from inside the transferred staging directory):
#   BIOS_ROOT=/recalbox/share/bios ./deploy_bios.sh
#
# The default BIOS_ROOT is /recalbox/share/bios which is correct for Recalbox.
# Only override if your setup is non-standard.
# =============================================================================

set -euo pipefail
BIOS_ROOT="${BIOS_ROOT:-/recalbox/share/bios}"

echo "Deploying BIOS files to: $BIOS_ROOT"
echo ""

DEPLOY_HEADER

    local line
    for line in "${DEPLOY_LINES[@]:-}"; do
        [[ -n "$line" ]] && echo "$line" >> "$DEPLOY_SCRIPT"
    done

    cat >> "$DEPLOY_SCRIPT" <<'DEPLOY_FOOTER'

echo ""
echo "Done. Re-run the BIOS checker (START → BIOS CHECKING) to confirm."
DEPLOY_FOOTER

    chmod +x "$DEPLOY_SCRIPT"
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print_summary() {
    local i
    # Collate not-found entries, required first
    i=0
    while [[ $i -lt $ENTRY_COUNT ]]; do
        if [[ "${E_STATUS[$i]}" == "NOTFOUND" ]]; then
            COUNT_NOTFOUND=$(( COUNT_NOTFOUND + 1 ))
            NOT_FOUND_LIST+=( "[${E_REQ[$i]}] ${E_REL[$i]}" )
        fi
        i=$(( i + 1 ))
    done

    echo ""
    echo "$(colour_bold '══════════════════════════════════════════════════')"
    echo "$(colour_bold '  Summary')"
    echo "$(colour_bold '══════════════════════════════════════════════════')"
    printf '  Total manifest entries   : %d\n' "$ENTRY_COUNT"
    printf '  %s\n' "$(colour_green  "Found & MD5 OK           : $COUNT_OK")"
    if [[ $COUNT_MISMATCH -gt 0 ]]; then
        printf '  %s\n' "$(colour_yellow "Found but MD5 MISMATCH   : $COUNT_MISMATCH")"
    fi
    printf '  %s\n' "$(colour_red "Not found on this machine: $COUNT_NOTFOUND")"
    echo ""
    echo "  Staged files : $DEST_DIR"
    echo "  Deploy script: $DEPLOY_SCRIPT"
    echo ""

    if [[ $COUNT_MISMATCH -gt 0 ]]; then
        echo "$(colour_yellow '⚠  MD5 MISMATCHES (copied but may be wrong region/revision):')"
        local w
        for w in "${MISMATCH_WARNINGS[@]}"; do
            echo "     $w"
        done
        echo ""
    fi

    if [[ $COUNT_NOTFOUND -gt 0 ]]; then
        echo "$(colour_red "✗  Files not found (${COUNT_NOTFOUND}):")"
        local f
        for f in "${NOT_FOUND_LIST[@]}"; do
            echo "     $f"
        done
        echo ""
        echo "  These will need to be sourced separately."
        echo ""
    fi

    echo "$(colour_bold 'Next steps:')"
    echo "  1. Copy deploy_bios.sh into the staging directory if it isn't already."
    echo "  2. SFTP the staging directory to the Recalbox:"
    echo "       sftp root@recalbox.local"
    echo "       put -r $DEST_DIR /recalbox/share/bios_staging"
    echo "  3. SSH in and deploy:"
    echo "       cd /recalbox/share/bios_staging && ./deploy_bios.sh"
    echo "  4. Restart EmulationStation and re-check START → BIOS CHECKING."
}

# ---------------------------------------------------------------------------
# Interactive prompts with persisted defaults
# ---------------------------------------------------------------------------
load_defaults() {
    DEF_SEARCH="$HOME"
    DEF_DEST="$HOME/bios_staging_recalbox"
    DEF_DEPLOY="./deploy_bios.sh"
    if [[ -f "$DEFAULTS_FILE" ]]; then
        # shellcheck disable=SC1090
        . "$DEFAULTS_FILE"
    fi
}

save_defaults() {
    {
        printf 'DEF_SEARCH=%q\n'  "$SEARCH_ROOT"
        printf 'DEF_DEST=%q\n'    "$DEST_DIR"
        printf 'DEF_DEPLOY=%q\n'  "$DEPLOY_SCRIPT"
    } > "$DEFAULTS_FILE"
    echo ""
    echo "  Defaults saved → $DEFAULTS_FILE"
}

prompt_paths() {
    load_defaults
    printf 'Search root directory [%s]: ' "$DEF_SEARCH"
    read -r SEARCH_ROOT
    [[ -z "$SEARCH_ROOT" ]] && SEARCH_ROOT="$DEF_SEARCH"

    printf 'Destination directory [%s]: ' "$DEF_DEST"
    read -r DEST_DIR
    [[ -z "$DEST_DIR" ]] && DEST_DIR="$DEF_DEST"

    printf 'Deploy script filename [%s]: ' "$DEF_DEPLOY"
    read -r DEPLOY_SCRIPT
    [[ -z "$DEPLOY_SCRIPT" ]] && DEPLOY_SCRIPT="$DEF_DEPLOY"

    if [[ ! -d "$SEARCH_ROOT" ]]; then
        echo "$(colour_red "ERROR: search root does not exist: $SEARCH_ROOT")"
        exit 1
    fi
    mkdir -p "$DEST_DIR"
    save_defaults
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    local manifest_file=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help|-h)        usage ;;
            --required-only)  REQUIRED_ONLY=1 ;;
            --hash-scan)      HASH_SCAN=1 ;;
            --max-size)       shift; MAX_SIZE_MB="${1:-16}" ;;
            *)                manifest_file="$1" ;;
        esac
        shift
    done

    if [[ -z "$manifest_file" || ! -f "$manifest_file" ]]; then
        echo "$(colour_red "ERROR: Manifest file not found: ${manifest_file:-<none>}")"
        echo "Expected: a Recalbox missing_bios_report.txt"
        echo "Run with --help for usage."
        exit 1
    fi

    prompt_paths

    echo ""
    echo "$(colour_bold 'Parsing manifest…')"
    parse_manifest "$manifest_file"

    if [[ $ENTRY_COUNT -eq 0 ]]; then
        echo "$(colour_yellow 'Nothing to do: no entries parsed from the report.')"
        exit 0
    fi

    search_and_copy
    if [[ $HASH_SCAN -eq 1 ]]; then
        hash_scan
    fi

    echo ""
    echo "$(colour_bold 'Generating deploy script…')"
    generate_deploy_script
    echo "  Written → $DEPLOY_SCRIPT"

    print_summary
}

main "$@"