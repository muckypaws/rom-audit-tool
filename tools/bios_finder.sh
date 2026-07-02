#!/usr/bin/env bash
# =============================================================================
# bios_finder.sh
#
# Scans a directory tree for BIOS files, validates MD5 checksums, copies
# matches to a staging directory, and generates a ready-to-run deployment
# script for transfer to Batocera/Recalbox/RetroArch.
#
# Compatible with bash 3.2+ (macOS default) and bash 4/5 (Linux/Homebrew).
# No associative arrays used.
#
# Manifest sources (pick one):
#   1. Live from Batocera via SSH:     ./bios_finder.sh --from-batocera [host]
#   2. Fetch readme.txt via SCP:       ./bios_finder.sh --fetch-readme [host]
#   3. Pipe batocera-systems output:   batocera-systems | ./bios_finder.sh -
#   4. Saved manifest file:            ./bios_finder.sh [path/to/manifest.txt]
#   5. Default file in script dir:     ./bios_finder.sh
#                                      (looks for bios_manifest.txt)
#
# Requirements: bash 3.2+, find, md5sum (Linux) or md5 (macOS), ssh/scp (optional)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
colour_red()    { printf '\033[0;31m%s\033[0m' "$*"; }
colour_yellow() { printf '\033[0;33m%s\033[0m' "$*"; }
colour_green()  { printf '\033[0;32m%s\033[0m' "$*"; }
colour_cyan()   { printf '\033[0;36m%s\033[0m' "$*"; }
colour_bold()   { printf '\033[1m%s\033[0m'    "$*"; }

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
expand_path() {
    local p="$1"
    if   [[ "$p" == "~/"* ]]; then echo "${HOME}/${p:2}"
    elif [[ "$p" == "~"   ]]; then echo "$HOME"
    else echo "$p"
    fi
}

# ---------------------------------------------------------------------------
# Defaults file
# ---------------------------------------------------------------------------
DEFAULTS_FILE="$(dirname "$0")/.bios_finder_defaults"

SEARCH_ROOT=""
DEST_DIR=""
DEPLOY_SCRIPT=""
BATOCERA_HOST=""

load_defaults() {
    [[ -f "$DEFAULTS_FILE" ]] || return 1
    # shellcheck source=/dev/null
    source "$DEFAULTS_FILE"
}

save_defaults() {
    cat > "$DEFAULTS_FILE" <<EOF
# bios_finder defaults – saved $(date '+%Y-%m-%d %H:%M:%S')
SEARCH_ROOT="$(sed 's/"/\\"/g' <<< "$SEARCH_ROOT")"
DEST_DIR="$(sed 's/"/\\"/g' <<< "$DEST_DIR")"
DEPLOY_SCRIPT="$(sed 's/"/\\"/g' <<< "$DEPLOY_SCRIPT")"
BATOCERA_HOST="$(sed 's/"/\\"/g' <<< "$BATOCERA_HOST")"
EOF
    echo "  Defaults saved → $DEFAULTS_FILE"
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
print_banner() {
    echo ""
    echo "$(colour_bold '╔══════════════════════════════════════════════════════════╗')"
    echo "$(colour_bold '║          BIOS Finder & Batocera Deploy Tool              ║')"
    echo "$(colour_bold '╚══════════════════════════════════════════════════════════╝')"
    echo ""
}

# ---------------------------------------------------------------------------
# Interactive config
# ---------------------------------------------------------------------------
prompt_config() {
    local defaults_loaded=false ans candidate

    if load_defaults 2>/dev/null; then
        defaults_loaded=true
        echo "$(colour_cyan 'Saved defaults found:')"
        echo "  Search root  : ${SEARCH_ROOT:-<not set>}"
        echo "  Destination  : ${DEST_DIR:-<not set>}"
        echo "  Deploy script: ${DEPLOY_SCRIPT:-<not set>}"
        [[ -n "${BATOCERA_HOST:-}" ]] && echo "  Batocera host: $BATOCERA_HOST"
        echo ""
        read -rp "$(colour_bold 'Use these defaults? [Y/n]: ')" ans
        ans="${ans:-Y}"
        if [[ "$ans" =~ ^[Yy] ]]; then
            SEARCH_ROOT="$(expand_path "$SEARCH_ROOT")"
            DEST_DIR="$(expand_path "$DEST_DIR")"
            return
        fi
        echo ""
    fi

    # --- Search root ---
    while true; do
        local sr_prompt="Search root directory"
        $defaults_loaded && [[ -n "${SEARCH_ROOT:-}" ]] \
            && sr_prompt+=" [${SEARCH_ROOT}]"
        sr_prompt+=": "
        read -rp "$(colour_bold "$sr_prompt")" ans
        candidate="$(expand_path "${ans:-${SEARCH_ROOT:-$HOME}}")"
        if [[ -d "$candidate" ]]; then
            SEARCH_ROOT="$candidate"
            break
        else
            echo "  $(colour_red "Not found: $candidate – please try again.")"
        fi
    done

    # --- Destination ---
    local dest_default
    dest_default="$(expand_path "${DEST_DIR:-$HOME/bios_staging}")"
    read -rp "$(colour_bold "Destination directory [$dest_default]: ")" ans
    DEST_DIR="$(expand_path "${ans:-$dest_default}")"

    # --- Deploy script ---
    local ds_default="${DEPLOY_SCRIPT:-$(dirname "$0")/deploy_bios.sh}"
    read -rp "$(colour_bold "Deploy script filename [$ds_default]: ")" ans
    DEPLOY_SCRIPT="${ans:-$ds_default}"
    [[ "$DEPLOY_SCRIPT" != */* ]] && DEPLOY_SCRIPT="$(dirname "$0")/$DEPLOY_SCRIPT"
    DEPLOY_SCRIPT="$(expand_path "$DEPLOY_SCRIPT")"

    echo ""
    save_defaults
    echo ""
}

# ---------------------------------------------------------------------------
# Manifest sourcing
# ---------------------------------------------------------------------------
MANIFEST_TMP=""

read_stdin_manifest() {
    MANIFEST_TMP="$(mktemp /tmp/bios_manifest_XXXXXX.txt)"
    cat /dev/stdin > "$MANIFEST_TMP"
    echo "$MANIFEST_TMP"
}

fetch_from_batocera() {
    local host="$1"
    echo "  $(colour_cyan "Connecting to Batocera at $host …")"
    MANIFEST_TMP="$(mktemp /tmp/bios_manifest_XXXXXX.txt)"
    if ssh -o ConnectTimeout=10 \
           -o StrictHostKeyChecking=accept-new \
           "root@${host}" \
           "batocera-systems" > "$MANIFEST_TMP" 2>/dev/null; then
        local count
        count=$(grep -cE '^(MISSING|UNTESTED)' "$MANIFEST_TMP" || true)
        echo "  $(colour_green "Fetched manifest: $count BIOS entries")"
    else
        echo "  $(colour_red "SSH failed. Is Batocera running and reachable?")"
        rm -f "$MANIFEST_TMP"
        exit 1
    fi
    echo "$MANIFEST_TMP"
}

fetch_readme_from_batocera() {
    local host="$1"
    local readme_path="/usr/share/batocera/datainit/bios/readme.txt"
    echo "  $(colour_cyan "Fetching $readme_path from $host …")"
    MANIFEST_TMP="$(mktemp /tmp/bios_manifest_XXXXXX.txt)"
    if scp -o ConnectTimeout=10 \
           -o StrictHostKeyChecking=accept-new \
           "root@${host}:${readme_path}" "$MANIFEST_TMP" 2>/dev/null; then
        local count
        count=$(grep -cE '^[0-9a-fA-F]{32}[[:space:]]' "$MANIFEST_TMP" || true)
        echo "  $(colour_green "Fetched readme.txt: $count BIOS entries")"
    else
        echo "  $(colour_red "SCP failed. Check host and that Batocera is running.")"
        rm -f "$MANIFEST_TMP"
        exit 1
    fi
    echo "$MANIFEST_TMP"
}

# ---------------------------------------------------------------------------
# Manifest parsing  –  bash 3.2 compatible, deduplicates by path.
#
# Because readme.txt lists multiple valid MD5s for the same file (different
# regional/revision dumps), we deduplicate on rel_path and store ALL valid
# hashes for a given file as a space-separated string in BIOS_MD5S[].
#
# Parallel indexed arrays (same index = same file):
#   BIOS_PATHS[]    relative path          e.g. bios/dc/naomi.zip
#   BIOS_MD5S[]     space-separated MD5s   e.g. "abc123 def456"  or "-"
#   BIOS_STATUSES[] status label           MISSING | UNTESTED | REFERENCE
#
# BIOS_PATH_INDEX[] is a flat lookup list ("path:index") used to find
# whether a path is already registered — a bash 3.2 substitute for an
# associative array keyed on path.
# ---------------------------------------------------------------------------
BIOS_PATHS=()
BIOS_MD5S=()
BIOS_STATUSES=()
BIOS_PATH_INDEX=()   # entries like "bios/adam.zip:0"
BIOS_COUNT=0

detect_manifest_format() {
    local manifest="$1"
    if grep -qE '^(MISSING|UNTESTED)[[:space:]]' "$manifest" 2>/dev/null; then
        echo "batocera-systems"
    else
        echo "readme"
    fi
}

# Return the array index for a given rel_path, or -1 if not found
find_path_index() {
    local needle="$1" entry idx
    for entry in "${BIOS_PATH_INDEX[@]+"${BIOS_PATH_INDEX[@]}"}"; do
        idx="${entry##*:}"
        if [[ "${entry%:*}" == "$needle" ]]; then
            echo "$idx"
            return
        fi
    done
    echo "-1"
}

register_entry() {
    local rel_path="$1" md5="$2" status="$3"
    local existing
    existing="$(find_path_index "$rel_path")"

    if [[ "$existing" == "-1" ]]; then
        # New path – add a fresh entry
        BIOS_PATHS[$BIOS_COUNT]="$rel_path"
        BIOS_MD5S[$BIOS_COUNT]="$md5"
        BIOS_STATUSES[$BIOS_COUNT]="$status"
        BIOS_PATH_INDEX+=("${rel_path}:${BIOS_COUNT}")
        BIOS_COUNT=$(( BIOS_COUNT + 1 ))
    else
        # Already seen – append this MD5 to the existing entry (if not "-")
        local current_md5s="${BIOS_MD5S[$existing]}"
        if [[ "$md5" != "-" && "$current_md5s" != "-" ]]; then
            BIOS_MD5S[$existing]="$current_md5s $md5"
        fi
        # Upgrade status: MISSING > UNTESTED > REFERENCE
        local current_status="${BIOS_STATUSES[$existing]}"
        if [[ "$current_status" == "REFERENCE" && "$status" != "REFERENCE" ]]; then
            BIOS_STATUSES[$existing]="$status"
        fi
    fi
}

parse_manifest() {
    local manifest="$1"
    local line status md5 rel_path fmt
    local raw_count=0 count_missing=0 count_untested=0

    fmt="$(detect_manifest_format "$manifest")"

    case "$fmt" in

        batocera-systems)
            echo "  $(colour_cyan 'Format: batocera-systems output (MISSING/UNTESTED)')"
            while IFS= read -r line || [[ -n "$line" ]]; do
                [[ "$line" =~ ^[[:space:]]*$  ]] && continue
                [[ "$line" =~ ^[[:space:]]*\> ]] && continue
                if [[ "$line" =~ ^(MISSING|UNTESTED)[[:space:]]+([^[:space:]]+)[[:space:]]+(.+)$ ]]; then
                    status="${BASH_REMATCH[1]}"
                    md5="${BASH_REMATCH[2]}"
                    rel_path="${BASH_REMATCH[3]}"
                    register_entry "$rel_path" "$md5" "$status"
                    raw_count=$(( raw_count + 1 ))
                    [[ "$status" == "MISSING"  ]] && count_missing=$(( count_missing + 1 ))
                    [[ "$status" == "UNTESTED" ]] && count_untested=$(( count_untested + 1 ))
                fi
            done < "$manifest"
            echo "  Parsed $(colour_green "$BIOS_COUNT") unique files  ($count_missing MISSING, $count_untested UNTESTED)"
            ;;

        readme)
            echo "  $(colour_cyan 'Format: readme.txt / plain reference list (<md5> <path>)')"
            echo "  $(colour_yellow 'Note: all entries treated as search targets')"
            while IFS= read -r line || [[ -n "$line" ]]; do
                [[ "$line" =~ ^[[:space:]]*$ ]] && continue
                [[ "$line" =~ ^#             ]] && continue
                if [[ "$line" =~ ^([0-9a-fA-F]{32})[[:space:]]+(.+)$ ]]; then
                    md5="${BASH_REMATCH[1]}"
                    rel_path="${BASH_REMATCH[2]}"
                    rel_path="${rel_path#"${rel_path%%[! ]*}"}"
                    register_entry "$rel_path" "$md5" "REFERENCE"
                    raw_count=$(( raw_count + 1 ))
                fi
            done < "$manifest"
            local dupes=$(( raw_count - BIOS_COUNT ))
            echo "  Parsed $(colour_green "$BIOS_COUNT") unique files  ($dupes duplicate MD5 variants merged)"
            ;;
    esac
}

# ---------------------------------------------------------------------------
# MD5 – cross-platform (macOS md5 or Linux md5sum)
# ---------------------------------------------------------------------------
md5_of_file() {
    if   command -v md5sum &>/dev/null; then md5sum "$1" | awk '{print $1}'
    elif command -v md5    &>/dev/null; then md5 -q  "$1"
    else echo "UNAVAILABLE"
    fi
}

# ---------------------------------------------------------------------------
# Search & copy
# ---------------------------------------------------------------------------
COUNT_FOUND=0
COUNT_MD5_OK=0
COUNT_MD5_MISMATCH=0
COUNT_NOT_FOUND=0
COUNT_RENAMED=0

# Plain indexed arrays for results (bash 3.2 safe)
DEPLOY_LINES=()
MISMATCH_WARNINGS=()
NOT_FOUND_LIST=()      # rel_paths only — for display in summary
NOT_FOUND_INDICES=()   # BIOS array indices — for MD5 hunt lookup
RENAMED_LIST=()
FIND_EXCLUDE_ARGS=()   # populated by find wrappers

# ---------------------------------------------------------------------------
# Directories excluded from all find operations (noisy / irrelevant)
# ---------------------------------------------------------------------------
EXCLUDE_DIRS=(
    ".git"
    "node_modules"
    ".npm"
    ".gradle"
    "Library/Caches"
    "Library/Application Support"
    ".Trash"
    "Xcode"
    ".venv"
    "__pycache__"
    ".cache"
    "snap"
    "proc"
    "sys"
    "dev"
)

# Build exclude args directly into a named array (avoids word-splitting on spaces)
# Usage: build_find_excludes myarray
build_find_excludes() {
    local -n _arr="$1"   # nameref — bash 4.3+; we use alternate below for 3.2
    # Note: bash 3.2 doesn't support namerefs. We write directly to a global instead.
    # Caller must declare FIND_EXCLUDE_ARGS=() before calling.
    local dir
    for dir in "${EXCLUDE_DIRS[@]}"; do
        FIND_EXCLUDE_ARGS+=( -not -path "*/${dir}/*" -not -path "*/${dir}" )
    done
}

# Wrapper: find with exclusions, filtered by filename
find_files_named() {
    local root="$1" name="$2"
    FIND_EXCLUDE_ARGS=()
    local dir
    for dir in "${EXCLUDE_DIRS[@]}"; do
        FIND_EXCLUDE_ARGS+=( -not -path "*/${dir}/*" -not -path "*/${dir}" )
    done
    find -L "$root" -type f -name "$name" "${FIND_EXCLUDE_ARGS[@]+"${FIND_EXCLUDE_ARGS[@]}"}" 2>/dev/null
}

# Wrapper: find ALL regular files (for MD5 hunt)
find_all_files() {
    local root="$1"
    FIND_EXCLUDE_ARGS=()
    local dir
    for dir in "${EXCLUDE_DIRS[@]}"; do
        FIND_EXCLUDE_ARGS+=( -not -path "*/${dir}/*" -not -path "*/${dir}" )
    done
    find -L "$root" -type f "${FIND_EXCLUDE_ARGS[@]+"${FIND_EXCLUDE_ARGS[@]}"}" 2>/dev/null
}

search_and_copy() {
    mkdir -p "$DEST_DIR"

    # Resolve staging dir to absolute path so we can exclude it from find,
    # regardless of whether SEARCH_ROOT was given as . or a relative path
    local abs_dest
    abs_dest="$(cd "$DEST_DIR" && pwd)"

    echo ""
    echo "$(colour_bold 'Pass 1 — searching by filename…')"
    echo ""

    local i rel_path valid_md5s status filename dest_subdir dest_full
    local best_file best_md5 best_matched candidate abs_candidate actual_md5
    local md5_matched known

    for (( i=0; i<BIOS_COUNT; i++ )); do
        rel_path="${BIOS_PATHS[$i]}"
        valid_md5s="${BIOS_MD5S[$i]}"
        status="${BIOS_STATUSES[$i]}"
        filename="$(basename "$rel_path")"
        dest_subdir="$(dirname "$rel_path")"
        dest_full="$DEST_DIR/$rel_path"

        mkdir -p "$DEST_DIR/$dest_subdir"

        printf '  [%-9s] %-44s' "$status" "$filename"

        best_file=""
        best_md5=""
        best_matched=false

        while IFS= read -r candidate; do
            abs_candidate="$(cd "$(dirname "$candidate")" 2>/dev/null && pwd)/$(basename "$candidate")"
            [[ "$abs_candidate" == "$abs_dest"/* ]] && continue
            [[ "$abs_candidate" == "$abs_dest"    ]] && continue

            if [[ "$valid_md5s" == "-" ]]; then
                best_file="$candidate"
                best_matched=true
                break
            fi

            actual_md5="$(md5_of_file "$candidate")"
            md5_matched=false
            for known in $valid_md5s; do
                if [[ "$actual_md5" == "$known" ]]; then
                    md5_matched=true
                    break
                fi
            done

            if $md5_matched; then
                best_file="$candidate"
                best_md5="$actual_md5"
                best_matched=true
                break
            elif [[ -z "$best_file" ]]; then
                best_file="$candidate"
                best_md5="$actual_md5"
            fi
        done < <(find_files_named "$SEARCH_ROOT" "$filename")

        if [[ -z "$best_file" ]]; then
            echo "$(colour_red 'NOT FOUND')"
            COUNT_NOT_FOUND=$(( COUNT_NOT_FOUND + 1 ))
            NOT_FOUND_LIST+=("$rel_path")
            NOT_FOUND_INDICES+=("$i")
            continue
        fi

        COUNT_FOUND=$(( COUNT_FOUND + 1 ))

        if [[ "$valid_md5s" == "-" ]]; then
            echo "$(colour_yellow 'FOUND  (no checksum – copied)')"
            cp -p "$best_file" "$dest_full"
            DEPLOY_LINES+=("cp -p \"$dest_full\" \"\$BIOS_ROOT/$rel_path\"")
            COUNT_MD5_OK=$(( COUNT_MD5_OK + 1 ))
        elif $best_matched; then
            echo "$(colour_green 'FOUND  ✓ MD5 OK')"
            cp -p "$best_file" "$dest_full"
            DEPLOY_LINES+=("cp -p \"$dest_full\" \"\$BIOS_ROOT/$rel_path\"")
            COUNT_MD5_OK=$(( COUNT_MD5_OK + 1 ))
        else
            echo "$(colour_yellow 'FOUND  ⚠ MD5 MISMATCH – copied anyway')"
            cp -p "$best_file" "$dest_full"
            DEPLOY_LINES+=("# ⚠ MD5 MISMATCH: $rel_path")
            DEPLOY_LINES+=("#   known good : $valid_md5s")
            DEPLOY_LINES+=("#   actual     : $best_md5")
            DEPLOY_LINES+=("#   source     : $best_file")
            DEPLOY_LINES+=("cp -p \"$dest_full\" \"\$BIOS_ROOT/$rel_path\"")
            MISMATCH_WARNINGS+=("$filename  |  actual=$best_md5  (known good: $valid_md5s)")
            COUNT_MD5_MISMATCH=$(( COUNT_MD5_MISMATCH + 1 ))
        fi
    done
}

# ---------------------------------------------------------------------------
# Pass 2: MD5 hunt
# For each file still not found, hash every file on the drive and check
# whether its MD5 matches any known-good hash for a missing target.
# If so, copy it to staging under the expected filename (rename in place).
# Only runs when --md5-hunt flag is set.
# ---------------------------------------------------------------------------
md5_hunt() {
    # Build huntable list from NOT_FOUND_INDICES — only entries with real MD5s
    local huntable_indices=()
    local idx valid_md5s
    for idx in "${NOT_FOUND_INDICES[@]+"${NOT_FOUND_INDICES[@]}"}"; do
        valid_md5s="${BIOS_MD5S[$idx]}"
        [[ "$valid_md5s" == "-" ]] && continue
        huntable_indices+=("$idx")
    done

    if [[ ${#huntable_indices[@]} -eq 0 ]]; then
        echo ""
        echo "  $(colour_green 'No files with known MD5s remain — MD5 hunt skipped.')"
        return
    fi

    local abs_dest
    abs_dest="$(cd "$DEST_DIR" && pwd)"

    echo ""
    echo "$(colour_bold 'Pass 2 — MD5 hunt (hashing all files, this will take longer)…')"
    echo "  Targets: ${#huntable_indices[@]} file(s) still missing"
    echo "  Excluding: ${EXCLUDE_DIRS[*]}"
    echo ""

    # Build flat lookup: "md5:bios_index" for every known-good hash of every target
    local md5_map=()
    for idx in "${huntable_indices[@]}"; do
        valid_md5s="${BIOS_MD5S[$idx]}"
        local known
        for known in $valid_md5s; do
            md5_map+=("${known}:${idx}")
        done
    done

    local files_checked=0
    local candidate
    local abs_candidate
    local actual_md5
    local map_entry
    local map_md5
    local map_idx
    local rel_path
    local filename
    local dest_subdir
    local dest_full
    local source_name
    local already
    local r

    # Track resolved indices to avoid double-copy
    local resolved=()

    while IFS= read -r candidate; do
        abs_candidate="$(cd "$(dirname "$candidate")" 2>/dev/null && pwd)/$(basename "$candidate")"
        [[ "$abs_candidate" == "$abs_dest"/* ]] && continue
        [[ "$abs_candidate" == "$abs_dest"    ]] && continue

        files_checked=$(( files_checked + 1 ))
        if (( files_checked % 500 == 0 )); then
            printf '\r  Checked %d files…' "$files_checked"
        fi

        actual_md5="$(md5_of_file "$candidate")"

        for map_entry in "${md5_map[@]+"${md5_map[@]}"}"; do
            map_md5="${map_entry%%:*}"
            map_idx="${map_entry#*:}"

            [[ "$actual_md5" != "$map_md5" ]] && continue

            # Skip if already resolved
            already=false
            for r in "${resolved[@]+"${resolved[@]}"}"; do
                [[ "$r" == "$map_idx" ]] && already=true && break
            done
            $already && continue

            # Match found — copy under expected name
            rel_path="${BIOS_PATHS[$map_idx]}"
            filename="$(basename "$rel_path")"
            dest_subdir="$(dirname "$rel_path")"
            dest_full="$DEST_DIR/$rel_path"
            source_name="$(basename "$candidate")"

            printf '\r'
            echo "  $(colour_green 'FOUND via MD5') $(colour_bold "$filename") ← renamed from $(colour_cyan "$source_name")"
            echo "    source: $candidate"

            mkdir -p "$DEST_DIR/$dest_subdir"
            cp -p "$candidate" "$dest_full"

            DEPLOY_LINES+=("# ✓ MD5 matched – renamed from $source_name")
            DEPLOY_LINES+=("cp -p \"$dest_full\" \"\$BIOS_ROOT/$rel_path\"")
            RENAMED_LIST+=("$filename  ←  $source_name  ($candidate)")
            resolved+=("$map_idx")

            COUNT_FOUND=$(( COUNT_FOUND + 1 ))
            COUNT_MD5_OK=$(( COUNT_MD5_OK + 1 ))
            COUNT_RENAMED=$(( COUNT_RENAMED + 1 ))
            COUNT_NOT_FOUND=$(( COUNT_NOT_FOUND - 1 ))
        done
    done < <(find_all_files "$SEARCH_ROOT")

    printf '\r  Checked %d files total.                    \n' "$files_checked"

    # Rebuild NOT_FOUND_LIST and NOT_FOUND_INDICES removing resolved entries.
    local new_not_found=()
    local new_indices=()
    local pos=0
    local total_nf=0
    local still_missing
    total_nf=${#NOT_FOUND_INDICES[@]}
    for (( pos=0; pos<total_nf; pos++ )); do
        idx="${NOT_FOUND_INDICES[$pos]}"
        still_missing=true
        for r in "${resolved[@]+"${resolved[@]}"}"; do
            [[ "$r" == "$idx" ]] && still_missing=false && break
        done
        if $still_missing; then
            new_not_found+=("${NOT_FOUND_LIST[$pos]}")
            new_indices+=("$idx")
        fi
    done
    NOT_FOUND_LIST=("${new_not_found[@]+"${new_not_found[@]}"}")
    NOT_FOUND_INDICES=("${new_indices[@]+"${new_indices[@]}"}")
}

# ---------------------------------------------------------------------------
# Generate deploy script
# ---------------------------------------------------------------------------
generate_deploy_script() {
    local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"

    cat > "$DEPLOY_SCRIPT" <<DEPLOY_HEADER
#!/usr/bin/env bash
# =============================================================================
# deploy_bios.sh  –  generated by bios_finder.sh on $ts
#
# Copies staged BIOS files to their correct locations on the target system.
#
# USAGE:
#   BIOS_ROOT=/userdata/bios ./deploy_bios.sh
#
# Default BIOS_ROOT is /userdata/bios (correct for Batocera).
# Recalbox uses /recalbox/share/bios  — set BIOS_ROOT accordingly.
#
# Staged files: $DEST_DIR
# =============================================================================

set -euo pipefail
BIOS_ROOT="\${BIOS_ROOT:-/userdata/bios}"

echo "Deploying BIOS files to: \$BIOS_ROOT"
echo ""

DEPLOY_HEADER

    local line
    for line in "${DEPLOY_LINES[@]+"${DEPLOY_LINES[@]}"}"; do
        echo "$line" >> "$DEPLOY_SCRIPT"
    done

    cat >> "$DEPLOY_SCRIPT" <<'DEPLOY_FOOTER'

echo ""
echo "Done."
DEPLOY_FOOTER

    chmod +x "$DEPLOY_SCRIPT"
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print_summary() {
    local total=$(( COUNT_FOUND + COUNT_NOT_FOUND ))
    echo ""
    echo "$(colour_bold '══════════════════════════════════════════════════')"
    echo "$(colour_bold '  Summary')"
    echo "$(colour_bold '══════════════════════════════════════════════════')"
    printf '  Total manifest entries : %d\n' "$total"
    printf '  %s\n' "$(colour_green "Found & MD5 OK           : $COUNT_MD5_OK")"
    (( COUNT_RENAMED > 0 )) && \
        printf '  %s\n' "$(colour_green "  of which found by MD5   : $COUNT_RENAMED (renamed to expected filename)")"
    (( COUNT_MD5_MISMATCH > 0 )) && \
        printf '  %s\n' "$(colour_yellow "Found, MD5 mismatch      : $COUNT_MD5_MISMATCH")"
    printf '  %s\n' "$(colour_red "Not found on this machine: $COUNT_NOT_FOUND")"

    echo ""
    echo "  Staged files : $DEST_DIR"
    echo "  Deploy script: $DEPLOY_SCRIPT"
    echo ""

    if (( COUNT_RENAMED > 0 )); then
        echo "$(colour_green '✓  Files found by MD5 and renamed:')"
        local r
        for r in "${RENAMED_LIST[@]+"${RENAMED_LIST[@]}"}"; do
            echo "     $r"
        done
        echo ""
    fi

    if (( COUNT_MD5_MISMATCH > 0 )); then
        echo "$(colour_yellow '⚠  MD5 MISMATCHES (copied but may be wrong region/revision):')"
        local w
        for w in "${MISMATCH_WARNINGS[@]+"${MISMATCH_WARNINGS[@]}"}"; do
            echo "     $w"
        done
        echo ""
    fi

    if (( COUNT_NOT_FOUND > 0 )); then
        echo "$(colour_red "✗  Files not found (${COUNT_NOT_FOUND}):")"
        local f
        for f in "${NOT_FOUND_LIST[@]+"${NOT_FOUND_LIST[@]}"}"; do
            echo "     $f"
        done
        echo ""
        echo "  These will need to be sourced separately."
        echo ""
    fi

    echo "$(colour_bold 'Next steps:')"
    echo "  1. SFTP the staging directory to the target:"
    echo "       sftp root@<host>"
    echo "       put -r $DEST_DIR /userdata/bios_staging"
    echo "  2. SSH in and run the deploy script:"
    echo "       ssh root@<host>"
    echo "       BIOS_ROOT=/userdata/bios bash /userdata/bios_staging/deploy_bios.sh"
    echo ""
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
cleanup() {
    [[ -n "${MANIFEST_TMP:-}" && -f "${MANIFEST_TMP:-}" ]] && rm -f "$MANIFEST_TMP"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat <<USAGE
Usage:
  $(basename "$0") [--md5-hunt] [manifest|option]

Manifest sources:
  $(basename "$0") manifest.txt             # saved file (auto-detects format)
  $(basename "$0") -                        # read from stdin
  batocera-systems | $(basename "$0") -     # pipe live output
  $(basename "$0") --from-batocera [host]   # SSH fetch via batocera-systems
  $(basename "$0") --fetch-readme [host]    # SCP fetch readme.txt from Batocera
  $(basename "$0")                          # use bios_manifest.txt in script dir

Flags:
  --md5-hunt    After the filename scan, hash every file in the search root
                and try to match remaining missing files by MD5 — even if
                they are stored under a completely different filename.
                Matched files are copied and renamed to the expected name.
                Slower than Pass 1 but catches renamed/mislabelled dumps.

Supported manifest formats (auto-detected):
  batocera-systems output   Lines beginning MISSING or UNTESTED
  readme.txt / plain list   Lines formatted as: <md5hash> bios/path/file.bin

Requires: bash 3.2+, find, md5 (macOS) or md5sum (Linux)
USAGE
    exit 0
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
main() {
    print_banner

    local manifest_file=""
    local fetch_mode="false"
    local do_md5_hunt=false
    local batocera_host="${BATOCERA_HOST:-batocera.local}"

    # Parse flags — allow --md5-hunt anywhere in args
    local args=()
    local arg
    for arg in "$@"; do
        case "$arg" in
            --md5-hunt) do_md5_hunt=true ;;
            *)          args+=("$arg") ;;
        esac
    done
    set -- "${args[@]+"${args[@]}"}"

    case "${1:-}" in
        -h|--help)         usage ;;
        --from-batocera)   fetch_mode="batocera"; [[ -n "${2:-}" ]] && batocera_host="$2" ;;
        --fetch-readme)    fetch_mode="readme";   [[ -n "${2:-}" ]] && batocera_host="$2" ;;
        -)
            echo "  $(colour_cyan 'Reading manifest from stdin…')"
            manifest_file="$(read_stdin_manifest)"
            ;;
        "")
            manifest_file="$(expand_path "$(dirname "$0")/bios_manifest.txt")"
            ;;
        *)
            manifest_file="$(expand_path "$1")"
            ;;
    esac

    prompt_config

    case "$fetch_mode" in
        batocera)
            BATOCERA_HOST="$batocera_host"; save_defaults >/dev/null
            manifest_file="$(fetch_from_batocera "$batocera_host")"
            ;;
        readme)
            BATOCERA_HOST="$batocera_host"; save_defaults >/dev/null
            manifest_file="$(fetch_readme_from_batocera "$batocera_host")"
            ;;
    esac

    if [[ -z "$manifest_file" || ! -f "$manifest_file" ]]; then
        echo "$(colour_red "ERROR: Manifest file not found: ${manifest_file:-<none>}")"
        echo "Run with --help for usage."
        exit 1
    fi

    echo ""
    echo "$(colour_bold 'Parsing manifest…')"
    parse_manifest "$manifest_file"

    search_and_copy

    if $do_md5_hunt && (( COUNT_NOT_FOUND > 0 )); then
        md5_hunt
    elif $do_md5_hunt && (( COUNT_NOT_FOUND == 0 )); then
        echo ""
        echo "  $(colour_green 'All files found in Pass 1 — MD5 hunt not needed.')"
    fi

    echo ""
    echo "$(colour_bold 'Generating deploy script…')"
    generate_deploy_script
    echo "  Written → $DEPLOY_SCRIPT"

    print_summary
}

main "$@"