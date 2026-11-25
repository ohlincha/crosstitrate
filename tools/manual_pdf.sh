#!/bin/sh
# Build docs/MANUAL.html (self-contained, shipped with the installers for Help > Manual)
# and docs/CrossTitrate-Manual.pdf from docs/MANUAL.md. Needs pandoc and Google Chrome/Chromium.
# Commit both outputs after editing the manual.
set -eu
cd "$(dirname "$0")/../docs"
pandoc MANUAL.md -s --embed-resources --metadata title="CrossTitrate manual" -c manual.css -o MANUAL.html
CHROME=$(command -v google-chrome || command -v chromium || command -v chromium-browser)
PROFILE=$(mktemp -d)   # own profile: don't touch a running browser
trap 'rm -rf "$PROFILE"' EXIT
"$CHROME" --headless --disable-gpu --no-pdf-header-footer --user-data-dir="$PROFILE" \
    --print-to-pdf="$PWD/CrossTitrate-Manual.pdf" "file://$PWD/MANUAL.html" 2>/dev/null
ls -l MANUAL.html CrossTitrate-Manual.pdf
