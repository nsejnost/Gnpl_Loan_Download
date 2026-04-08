# GNMA Multifamily Data Downloader & S-Curve Builder

## Project Overview

A Python CLI tool that automates downloading Ginnie Mae (GNMA) multifamily financial disclosure data (`mfplmon3` files), parses the data, and produces an Excel dataset for mortgage-backed securities (MBS) analysis.

## Tech Stack

- **Language:** Python 3.12
- **Data Processing:** pandas, numpy
- **Web Scraping:** requests + BeautifulSoup4 (primary), Playwright/Chromium (fallback)
- **File Output:** openpyxl (Excel), lxml

## Project Layout

```
main.py            # Core application — authentication, download, parse, Excel output
requirements.txt   # Python dependencies
```

### Runtime-generated:
- `gnma_mf_data/`             — downloaded ZIP and TXT disclosure files
- `gnma_mf_scurve_dataset.xlsx` — final output dataset
- `gnma_debug.png`            — Playwright debug screenshot (on auth failure)

## Running the App

```bash
python main.py --email <your@email.com> --answer <security_answer> [--months N]
```

### CLI Options
- `--email EMAIL`       GNMA account email
- `--answer ANSWER`     Security question answer
- `--months N`          Number of months to retrieve
- `--skip-download`     Skip download, use existing files
- `--data-dir DIR`      Override data directory
- `--browser`           Force Playwright browser mode
- `--headed`            Show browser window (debug mode)

## Workflow

Configured as a console workflow running `python main.py --help` to verify setup.
Deployment target: **vm** (always-running process).

## Dependencies

All packages from `requirements.txt` are installed:
- requests, beautifulsoup4, pandas, openpyxl, lxml, numpy, playwright
