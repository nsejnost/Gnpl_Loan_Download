#!/bin/bash
# GNMA MF Downloader - Run Script
# Usage:
#   bash run.sh --email you@email.com --answer "YourAnswer"
#   bash run.sh --email you@email.com --answer "YourAnswer" --months 12
#   bash run.sh --skip-download

# Install Firefox if not present
if ! ls "$HOME/workspace/.cache/ms-playwright/firefox-"* &>/dev/null; then
    echo "[setup] Installing Firefox for Playwright..."
    python3 -m playwright install firefox 2>/dev/null
fi

# Set library paths for Firefox (nix store)
USER_ENV_LIB=$(ls -d /nix/store/*-user-environment/lib 2>/dev/null | tail -1)
FONTCONFIG_LIB=$(dirname "$(ls /nix/store/*fontconfig*-lib/lib/libfontconfig.so.1 2>/dev/null | head -1)" 2>/dev/null)
export LD_LIBRARY_PATH="${FONTCONFIG_LIB:+$FONTCONFIG_LIB:}${USER_ENV_LIB:+$USER_ENV_LIB:}"
echo "[setup] LD_LIBRARY_PATH set"

python3 main.py "$@"
