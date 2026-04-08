#!/bin/bash
# GNMA MF Downloader - Run Script
# Sets up library paths for Firefox/Playwright on Replit, then runs main.py
#
# Usage:
#   bash run.sh --email you@email.com --answer "YourAnswer"
#   bash run.sh --email you@email.com --answer "YourAnswer" --months 12
#   bash run.sh --skip-download

# First-time setup: install Firefox for Playwright if not present
if [ ! -d "$HOME/workspace/.cache/ms-playwright/firefox-"* ] 2>/dev/null; then
    echo "[setup] Installing Firefox for Playwright..."
    python3 -m playwright install firefox 2>/dev/null
fi

# Set LD_LIBRARY_PATH for nix-installed libraries
# Find the user-environment lib directory
USER_ENV_LIB=$(ls -d /nix/store/*-user-environment/lib 2>/dev/null | tail -1)

# Find fontconfig lib (often in a separate path)
FONTCONFIG_LIB=$(dirname "$(ls /nix/store/*fontconfig*-lib/lib/libfontconfig.so.1 2>/dev/null | head -1)" 2>/dev/null)

# Build LD_LIBRARY_PATH
export LD_LIBRARY_PATH="${FONTCONFIG_LIB:+$FONTCONFIG_LIB:}${USER_ENV_LIB:+$USER_ENV_LIB:}${LD_LIBRARY_PATH}"

echo "[setup] LD_LIBRARY_PATH set"

# Run the main script with all arguments passed through
python3 main.py "$@"
