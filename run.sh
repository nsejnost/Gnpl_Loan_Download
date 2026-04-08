#!/bin/bash
# GNMA MF Downloader - Run Script for Replit
# Handles nix library path discovery for Playwright Firefox, then runs main.py
#
# Usage:
#   bash run.sh --email you@email.com --answer "YourAnswer"
#   bash run.sh --email you@email.com --answer "YourAnswer" --months 12
#   bash run.sh --skip-download

set -euo pipefail

# ─── FIREFOX SETUP ─────────────────────────────────────────
# Install Firefox for Playwright if not already present
if ! ls "$HOME/workspace/.cache/ms-playwright/firefox-"*/firefox/firefox &>/dev/null; then
    echo "[setup] Installing Firefox for Playwright..."
    python3 -m playwright install firefox
fi

# ─── NIX LIBRARY PATH DISCOVERY ───────────────────────────
# Replit uses Nix, which puts libraries in /nix/store/<hash>-<pkg>/lib/
# instead of standard /usr/lib/. Playwright's Firefox can't find them
# without LD_LIBRARY_PATH pointing to the right directories.
#
# Strategy:
#   1. Find the LATEST user-environment/lib (contains most nix-env installed libs)
#   2. Add fontconfig separately (often lives outside user-environment)
#   3. Add gtk3 lib path separately (critical for Firefox)

echo "[setup] Discovering nix library paths..."

# 1. Latest user-environment (sorted by modification time, newest first)
USER_ENV_LIB=$(ls -dt /nix/store/*-user-environment/lib 2>/dev/null | head -1)
if [ -n "$USER_ENV_LIB" ]; then
    echo "  user-environment: $USER_ENV_LIB"
    LIB_PATH="$USER_ENV_LIB"
else
    echo "  WARNING: No user-environment found"
    LIB_PATH=""
fi

# 2. Fontconfig (often in a separate -lib derivation)
FC_LIB=$(dirname "$(ls /nix/store/*fontconfig*-lib/lib/libfontconfig.so.1 2>/dev/null | head -1)" 2>/dev/null)
if [ -n "$FC_LIB" ] && [ "$FC_LIB" != "." ]; then
    echo "  fontconfig: $FC_LIB"
    LIB_PATH="${FC_LIB}:${LIB_PATH}"
fi

# 3. GTK3 (Firefox specifically needs libgtk-3.so.0)
GTK_LIB=$(dirname "$(ls /nix/store/*gtk+3*/lib/libgtk-3.so.0 2>/dev/null | head -1)" 2>/dev/null)
if [ -n "$GTK_LIB" ] && [ "$GTK_LIB" != "." ]; then
    echo "  gtk3: $GTK_LIB"
    LIB_PATH="${GTK_LIB}:${LIB_PATH}"
fi

# 4. GDK-Pixbuf (needed by GTK)
GDK_LIB=$(dirname "$(ls /nix/store/*gdk-pixbuf*/lib/libgdk_pixbuf-2.0.so.0 2>/dev/null | head -1)" 2>/dev/null)
if [ -n "$GDK_LIB" ] && [ "$GDK_LIB" != "." ]; then
    echo "  gdk-pixbuf: $GDK_LIB"
    LIB_PATH="${GDK_LIB}:${LIB_PATH}"
fi

# 5. Pango/Cairo (text rendering)
PANGO_LIB=$(dirname "$(ls /nix/store/*pango*/lib/libpango-1.0.so.0 2>/dev/null | head -1)" 2>/dev/null)
if [ -n "$PANGO_LIB" ] && [ "$PANGO_LIB" != "." ]; then
    LIB_PATH="${PANGO_LIB}:${LIB_PATH}"
fi

# 6. Additional libs that might be in separate derivations
for lib_pattern in "libX11.so.6" "libXrender.so.1" "libfreetype.so.6" "libdbus-1.so.3" "libatk-1.0.so.0"; do
    LIB_DIR=$(dirname "$(ls /nix/store/*/${lib_pattern} 2>/dev/null | grep -v user-environment | head -1)" 2>/dev/null)
    if [ -n "$LIB_DIR" ] && [ "$LIB_DIR" != "." ]; then
        LIB_PATH="${LIB_DIR}:${LIB_PATH}"
    fi
done

export LD_LIBRARY_PATH="${LIB_PATH}"
echo "[setup] LD_LIBRARY_PATH configured (${#LIB_PATH} chars)"

# ─── RUN ───────────────────────────────────────────────────
python3 main.py "$@"
