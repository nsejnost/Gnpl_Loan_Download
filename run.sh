#!/bin/bash
# GNMA MF Downloader - Run Script for Replit
# Nix library discovery and LD_LIBRARY_PATH are now handled in main.py
#
# Usage:
#   bash run.sh --email you@email.com --answer "YourAnswer"
#   bash run.sh --email you@email.com --answer "YourAnswer" --months 12
#   bash run.sh --skip-download

set -euo pipefail

# Install Python dependencies
pip install -q -r requirements.txt

# Install Firefox for Playwright if not already present
if ! ls "$HOME/workspace/.cache/ms-playwright/firefox-"*/firefox/firefox &>/dev/null; then
    echo "[setup] Installing Firefox for Playwright..."
    python3 -m playwright install firefox
fi

python3 main.py "$@"

# Show output summary if gzipped CSV was created
if [ -f gnma_mf_raw_data.csv.gz ]; then
    echo ""
    echo "[verify] File size: $(ls -lh gnma_mf_raw_data.csv.gz | awk '{print $5}')"
    echo "[verify] To open in pandas: pd.read_csv('gnma_mf_raw_data.csv.gz')"
fi
