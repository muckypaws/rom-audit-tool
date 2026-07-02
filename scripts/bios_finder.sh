#!/usr/bin/env bash

set -euo pipefail

##############################################################################
# Configuration
##############################################################################

CONFIG_FILE="bios_finder.conf"
REPORT_FILE="bios_report.txt"

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
NC="\033[0m"

##############################################################################
# Path Expansion
##############################################################################

expand_path() {
    local path="$1"

    case "$path" in
        "~")
            printf "%s\n" "$HOME"
            ;;
        "~/"*)
            printf "%s/%s\n" "$HOME" "${path#~/}"
            ;;
        *)
            printf "%s\n" "$path"
            ;;
    esac
}

##############################################################################
# Configuration Handling
##############################################################################

configure() {

    echo
    echo "Configuration"
    echo "============="

    read -rp "Search Root [$HOME]: " SEARCH_ROOT
    SEARCH_ROOT="${SEARCH_ROOT:-$HOME}"

    read -rp "Stage Root [$HOME/BIOS_Stage]: " STAGE_ROOT
    STAGE_ROOT="${STAGE_ROOT:-$HOME/BIOS_Stage}"

    read -rp "Copy Script [copy_bios.sh]: " COPY_SCRIPT
    COPY_SCRIPT="${COPY_SCRIPT:-copy_bios.sh}"

    SEARCH_ROOT=$(expand_path "$SEARCH_ROOT")
    STAGE_ROOT=$(expand_path "$STAGE_ROOT")

    cat > "$CONFIG_FILE" <<EOF
SEARCH_ROOT="$SEARCH_ROOT"
STAGE_ROOT="$STAGE_ROOT"
COPY_SCRIPT="$COPY_SCRIPT"
EOF

    echo
    echo "Configuration saved."
}

load_config() {

    if [ ! -f "$CONFIG_FILE" ]; then
        configure
        return
    fi

    source "$CONFIG_FILE"

    SEARCH_ROOT=$(expand_path "$SEARCH_ROOT")
    STAGE_ROOT=$(expand_path "$STAGE_ROOT")

    echo
    echo "Current Configuration"
    echo "====================="
    echo "Search Root : $SEARCH_ROOT"
    echo "Stage Root  : $STAGE_ROOT"
    echo "Copy Script : $COPY_SCRIPT"
    echo

    read -rp "Use these settings? [Y/n]: " reply

    case "${reply:-Y}" in
        [Nn]*)
            configure
            ;;
    esac
}

##############################################################################
# Main
##############################################################################

INPUT_FILE="${1:-}"

if [ -z "$INPUT_FILE" ]; then
    echo
    echo "Usage:"
    echo "  $0 missing_bios.txt"
    echo
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    echo "Input file not found: $INPUT_FILE"
    exit 1
fi

load_config

echo
echo "Building file index..."
echo "Search Root: $SEARCH_ROOT"
echo

INDEX_FILE=$(mktemp)

if ! find "$SEARCH_ROOT" -type f 2>/dev/null > "$INDEX_FILE"
then

    echo

    echo "ERROR: Search root does not exist:"

    echo "  $SEARCH_ROOT"

    exit 1

fi
echo "Expanded Search Root: $SEARCH_ROOT"
echo "Indexed $(wc -l < "$INDEX_FILE") files"
echo

##############################################################################
# Initialise Outputs
##############################################################################

: > "$REPORT_FILE"

cat > "$COPY_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail

EOF

chmod +x "$COPY_SCRIPT"

##############################################################################
# Statistics
##############################################################################

TOTAL=0
MATCHED=0
MISMATCH=0
NOTFOUND=0
UNKNOWN=0

##############################################################################
# Process BIOS List
##############################################################################

while IFS= read -r line
do

    [[ -z "$line" ]] && continue

    [[ "$line" =~ ^\> ]] && continue

    if [[ "$line" =~ ^(MISSING|UNTESTED) ]]
    then

        TOTAL=$((TOTAL + 1))

        STATUS=$(echo "$line" | awk '{print $1}')
        EXPECTED_MD5=$(echo "$line" | awk '{print $2}')
        BIOS_PATH=$(echo "$line" | awk '{print $3}')

        [ -z "$BIOS_PATH" ] && continue

        FILENAME=$(basename "$BIOS_PATH")

        FOUND=$(grep -i "/${FILENAME}$" "$INDEX_FILE" | head -1 || true)

        if [ -z "$FOUND" ]
        then

            echo -e "${RED}[MISSING]${NC} $FILENAME"

            {
                echo "[MISSING]"
                echo "BIOS : $BIOS_PATH"
                echo
            } >> "$REPORT_FILE"

            NOTFOUND=$((NOTFOUND + 1))
            continue
        fi

        DEST="$STAGE_ROOT/$BIOS_PATH"
        DESTDIR=$(dirname "$DEST")

        ACTUAL_MD5=$(md5sum "$FOUND" | awk '{print $1}')

        ######################################################################
        # No checksum supplied
        ######################################################################

        if [ "$EXPECTED_MD5" = "-" ]
        then

            echo -e "${YELLOW}[UNKNOWN]${NC} $FILENAME"

            cat >> "$COPY_SCRIPT" <<EOF

mkdir -p "$DESTDIR"
cp -p "$FOUND" "$DEST"

EOF

            {
                echo "[UNKNOWN]"
                echo "BIOS  : $BIOS_PATH"
                echo "FOUND : $FOUND"
                echo
            } >> "$REPORT_FILE"

            UNKNOWN=$((UNKNOWN + 1))
            continue
        fi

        ######################################################################
        # MD5 Match
        ######################################################################

        if [ "$ACTUAL_MD5" = "$EXPECTED_MD5" ]
        then

            echo -e "${GREEN}[OK]${NC} $FILENAME"

            cat >> "$COPY_SCRIPT" <<EOF

mkdir -p "$DESTDIR"
cp -p "$FOUND" "$DEST"

EOF

            {
                echo "[OK]"
                echo "BIOS     : $BIOS_PATH"
                echo "FOUND    : $FOUND"
                echo "EXPECTED : $EXPECTED_MD5"
                echo "ACTUAL   : $ACTUAL_MD5"
                echo
            } >> "$REPORT_FILE"

            MATCHED=$((MATCHED + 1))

        ######################################################################
        # MD5 Mismatch
        ######################################################################

        else

            echo -e "${YELLOW}[WARN]${NC} $FILENAME"

            cat >> "$COPY_SCRIPT" <<EOF

# WARNING: MD5 MISMATCH
# BIOS     : $BIOS_PATH
# EXPECTED : $EXPECTED_MD5
# ACTUAL   : $ACTUAL_MD5

mkdir -p "$DESTDIR"
cp -p "$FOUND" "$DEST"

EOF

            {
                echo "[WARN]"
                echo "BIOS     : $BIOS_PATH"
                echo "FOUND    : $FOUND"
                echo "EXPECTED : $EXPECTED_MD5"
                echo "ACTUAL   : $ACTUAL_MD5"
                echo
            } >> "$REPORT_FILE"

            MISMATCH=$((MISMATCH + 1))
        fi

    fi

done < "$INPUT_FILE"

rm -f "$INDEX_FILE"

##############################################################################
# Summary
##############################################################################

echo
echo "=================================================="
echo "Summary"
echo "=================================================="
echo "Total Entries      : $TOTAL"
echo "Checksum Matches   : $MATCHED"
echo "Checksum Warnings  : $MISMATCH"
echo "Unknown Checksums  : $UNKNOWN"
echo "Not Found          : $NOTFOUND"
echo
echo "Generated Files"
echo "---------------"
echo "$COPY_SCRIPT"
echo "$REPORT_FILE"
echo
echo "Review then execute:"
echo
echo "  ./$COPY_SCRIPT"
echo