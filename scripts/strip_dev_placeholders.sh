#!/bin/sh
# ==============================================================================
# Hardcoded Production Release Gate: Strips all development placeholder forms
# Run this script before merging/promoting dev branch into production master!
# ==============================================================================
set -e

echo "[RELEASE GATE] Scanning for development placeholder forms..."
REMOVED_COUNT=0

for f in data/forms/placeholder_*.json data_dev/forms/placeholder_*.json; do
    if [ -f "$f" ]; then
        echo "  - Removing: $f"
        rm -f "$f"
        REMOVED_COUNT=$((REMOVED_COUNT + 1))
    fi
done

echo "[RELEASE GATE] Stripped $REMOVED_COUNT placeholder form files."
echo "[RELEASE GATE] Verification: Zero placeholder forms remaining."
