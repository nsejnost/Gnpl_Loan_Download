# GNMA Multifamily Loan Data Downloader & Prepayment Analyzer

Automated tool that authenticates with Ginnie Mae's gated Disclosure Data Download site, bulk-downloads monthly multifamily loan-level portfolio files (`mfplmon3`), produces a single enriched CSV with prepayment flags, lockout/penalty status, and refinance incentive calculations, and runs an XGBoost + SHAP machine learning pipeline that estimates prepayment S-curves and attributes prepayment probability to individual loan characteristics.

## Quick Start

### On Replit (recommended)
1. Open the project in Replit
2. Hit the **Run** button — it handles authentication, download, and parsing automatically
3. Output: `gnma_mf_raw_data_YYYYMMDD_HHMMSS.csv.gz` (~15,000 loans × 120 months ≈ 1.8M rows)
4. Optionally run the ML analysis:
   ```bash
   python3 prepayment_analysis.py    # trains XGBoost, computes SHAP, saves analysis_results.json
   python3 generate_report.py        # builds interactive prepayment_report.html
   ```

### On any machine
```bash
pip install -r requirements.txt
python3 -m playwright install firefox
bash run.sh --email you@email.com --answer "YourSecurityAnswer"
```

### Parse already-downloaded files (no browser needed)
```bash
bash run.sh --skip-download
# or directly:
python3 main.py --skip-download --data-dir ./my_files
```

### CLI options
| Flag | Default | Description |
|------|---------|-------------|
| `--email` | (prompted) | GNMA Disclosure site email |
| `--answer` | (prompted) | Security question answer |
| `--months` | 12 | Number of monthly files to download |
| `--skip-download` | false | Skip auth/download, parse existing files only |
| `--data-dir` | `./gnma_mf_data` | Directory for downloaded zip files |

Note: The `.replit` Run button is configured to pass `--months 120` for the maximum available history (roughly May 2022 onward, when the V3.1 layout was introduced).

## What This Tool Does

Ginnie Mae publishes monthly loan-level data for all multifamily (project loan) MBS pools, but there is **no public API** — the data sits behind a gated website that requires email + security question authentication, with forms rendered by SharePoint JavaScript. This tool automates the entire pipeline:

```
┌─────────────────────────────────────────────────────────────┐
│  Phase 1: Authentication & Download                         │
│                                                             │
│  Playwright Firefox (headless)                              │
│    → Navigate to GNMA profile page                          │
│    → Fill email → ASP.NET postback                          │
│    → Fill security answer → postback                        │
│    → Extract session cookies                                │
│                                                             │
│  requests (with session cookies)                            │
│    → Bulk download mfplmon3_YYYYMM.zip files                │
│    → Try data_bulk/ first, fall back to data_history_cons/  │
│    → Validate each zip as a real mfplmon file               │
│    → Cached locally — skips already-downloaded files        │
├─────────────────────────────────────────────────────────────┤
│  Phase 2: Parse & Enrich                                    │
│                                                             │
│  Parse mfplmon3 V3.1 / V3.2 / V3.3 (detected at runtime)    │
│    → V3.3: 31 pool + 44 loan = 75 fields per record         │
│    → V3.1/V3.2: 29 pool + 44 loan = 73 fields per record    │
│                                                             │
│  Analytics (build_analytics)                                │
│    → Lockout & penalty period status per loan-month         │
│    → Prepayment flags (lockout + construction excluded)     │
│    → Refi incentive using GNMA PLC rates                    │
│    → Declining penalty schedule parsed from prepay_desc     │
│                                                             │
│  Output: timestamped gzipped CSV                            │
├─────────────────────────────────────────────────────────────┤
│  Phase 3: ML Prepayment Analysis (optional)                 │
│                                                             │
│  prepayment_analysis.py                                     │
│    → Empirical S-curves (refi, penalty, age, UPB, state)    │
│    → XGBoost binary classifier with time-based split        │
│    → SHAP TreeExplainer: global + per-loan attribution      │
│    → Model-implied S-curves from median loan profile        │
│    → Saves analysis_results.json                            │
│                                                             │
│  generate_report.py                                         │
│    → Self-contained prepayment_report.html with Chart.js    │
│    → S-curves, feature importance, sample loan waterfalls   │
└─────────────────────────────────────────────────────────────┘
```

## Output CSV: Column Reference

The output file `gnma_mf_raw_data_YYYYMMDD_HHMMSS.csv.gz` contains one row per loan per month. With 120 months of history and ~15,000 loans/month, expect roughly 1.8M rows. The file is gzip-compressed and can be read directly by pandas: `pd.read_csv('gnma_mf_raw_data_*.csv.gz')`.

### Raw Fields (parsed from mfplmon3)

**Pool-level fields:**
| Column | Description |
|--------|-------------|
| `pool_cusip` | 9-character CUSIP identifier for the pool |
| `pool_number` | GNMA pool number |
| `pool_type` | Pool type code: PL, PN, LM, LS, RX, CL, CS |
| `pool_type_name` | Human-readable pool type (e.g., "Project Loan") |
| `security_rate` | Pass-through coupon rate on the security (%) |
| `issue_date` | Pool issuance date (YYYYMMDD) |
| `pool_maturity_date` | Pool maturity date (YYYYMMDD) |
| `orig_agg_amount` | Original aggregate pool amount ($) |
| `issuer_number` | GNMA issuer/servicer number |
| `issuer_name` | Issuer/servicer name |
| `pool_upb` | Current pool unpaid principal balance ($) |
| `security_rpb` | Remaining principal balance of the security ($) |
| `rpb_factor` | RPB factor (current RPB / original face) |
| `proj_loan_sec_rate` | Project Loan Security Interest Rate (V3.3-only, populated only for CS pool types) |
| `est_mtg_amount` | Estimated Mortgage Amount at construction-loan conversion (V3.3-only, populated only for CL/CS pool types) |

**Loan-level fields:**
| Column | Description |
|--------|-------------|
| `loan_id` | Unique identifier: `pool_cusip` + `_` + `case_number` |
| `case_number` | FHA/RD case number |
| `agency_type` | Insuring agency (FHA, RD, etc.) |
| `loan_type` | Loan type code |
| `loan_term` | Original loan term (months) |
| `loan_rate` | Current note rate (%) — this is the "net coupon" for refi incentive |
| `first_pay_date` | First payment date (YYYYMMDD) |
| `loan_maturity_date` | Loan maturity date (YYYYMMDD) |
| `origination_date` | Loan origination date (YYYYMMDD) |
| `orig_prin_bal` | Original principal balance ($) |
| `upb_at_issuance` | UPB at pool issuance ($) |
| `upb` | Current unpaid principal balance ($) |
| `lockout_term_yrs` | Lockout period length (years) |
| `lockout_end_date` | Date lockout period ends (YYYYMMDD) |
| `prepay_premium_period_yrs` | Prepayment penalty period length (years) |
| `prepay_end_date` | Date penalty period ends (YYYYMMDD) |
| `prepay_penalty_flag` | Penalty flag from GNMA |
| `prepay_desc` | Prepayment protection description text (declining schedule) |
| `months_dq` | Months delinquent (0 = current) |
| `removal_reason` | Reason for removal: 1=voluntary payoff, 2/3/4/6=involuntary |
| `liquidation_flag` | Liquidation indicator |
| `property_name` | Property name |
| `property_city` | Property city |
| `property_state` | Property state (2-letter) |
| `msa` | Metropolitan Statistical Area code |
| `num_units` | Number of housing units |
| `pi_amount` | Monthly principal & interest payment ($) |
| `fha_program_code` | FHA program (e.g., 223f, 221d4) |
| `insurance_type` | Insurance type |
| `green_status` | Green MBS indicator |
| `affordable_status` | Affordable housing indicator |
| `as_of_date` | Data as-of date from GNMA |
| `modified_ind` | Modified loan indicator |
| `non_level_ind` | Non-level payment indicator |
| `mature_loan_flag` | Mature loan indicator |
| `period` | Observation period (YYYYMM) |

### Computed Fields (added by `build_analytics()`)

| Column | Description |
|--------|-------------|
| `in_lockout` | 1 if the loan is currently in its lockout period (period < lockout_end_date) |
| `in_prepay_penalty` | 1 if the loan is past lockout but still in its penalty period |
| `past_all_restrictions` | 1 if the loan is past both lockout and penalty periods |
| `prepay_penalty_points` | Current penalty % from declining schedule in `prepay_desc` (capped at 10) |
| `plc_rate_bps` | GNMA PLC rate for this period (basis points, from GnmaPlcRatesHistorical.csv) |
| `refi_incentive_bps` | Refinance incentive in basis points (see formula below) |
| `prepay_eligible` | 1 if eligible for prepayment analysis (not in lockout, not CL/CS construction) |
| `prepaid_voluntary` | 1 if the loan voluntarily prepaid this period |
| `prepaid_involuntary` | 1 if the loan was involuntarily removed this period |

## ML Prepayment Analysis

Once `main.py` has produced a `gnma_mf_raw_data_*.csv.gz`, two additional scripts turn it into a full prepayment model with an interactive report:

```bash
python3 prepayment_analysis.py    # auto-detects the most recent raw data file
python3 generate_report.py        # writes prepayment_report.html
```

### What `prepayment_analysis.py` does

1. **Loads the most recent `gnma_mf_raw_data_*.csv.gz`** (via `glob` + mtime — no hardcoded filename).
2. **Feature engineering:** loan age, remaining term, log UPB, refi incentive bucket (25 bp bins), age bucket, UPB bucket, penalty points bucket, penalty status (`in_lockout` / `in_prepay_penalty` / `past_all`), top-15 state group, pool type group, green/affordable flags, delinquency flag.
3. **Empirical S-curves:** CPR by refi incentive, by penalty status × refi incentive, by loan age, by UPB, by penalty points, and by state. Each bucket requires a minimum observation count for statistical reliability.
4. **XGBoost binary classifier** predicting `prepaid_voluntary` per loan-month:
   - 500 trees, max depth 4, learning rate 0.03
   - Moderate class weighting (`scale_pos_weight=3.0`) to keep probabilities calibrated for the rare ~0.26% positive rate
   - L1/L2 regularization and `min_child_weight=20` to prevent overfitting on the sparse positive class
   - **Time-based train/test split:** the last 6 monthly periods are held out as an out-of-time test set
5. **SHAP TreeExplainer:**
   - Global feature importance via mean |SHAP| across a 3,000-row test sample
   - Per-loan attribution for 7 sample loans (3 actual prepays, 2 high-risk survivors, 2 low-risk) — decomposes each prediction into additive feature contributions relative to the model baseline
6. **Model-implied S-curves:** synthetic median-loan profiles varied across the refi-incentive grid (-500 to +675 bps), evaluated separately for "past all restrictions" and "in penalty" profiles.

Results are saved to `analysis_results.json` for the report generator to consume.

### What `generate_report.py` produces

`prepayment_report.html` is a self-contained HTML report (loads Chart.js from CDN, all data embedded inline). Sections:

1. **Dataset Overview** — row counts, baseline SMM/CPR, model AUC
2. **Empirical Refi Incentive S-Curve** — the classic S-curve shape, with observation counts per bucket
3. **S-Curves by Penalty Status** — separate curves for loans in lockout, in penalty, and past all restrictions
4. **Model-Implied S-Curves** — smooth curves from the XGBoost model at the median loan profile
5. **CPR by Loan Age, UPB, Penalty Points, State** — categorical bar charts
6. **Feature Importance** — XGBoost gain and mean |SHAP|
7. **Sample Loan Attribution** — 7 loan cards with SHAP waterfalls showing which features drive each loan's prediction relative to the baseline

### Typical results (with 24 months of training data)

| Metric | Value |
|--------|-------|
| Eligible loan-months | ~352,000 |
| Voluntary prepayment events | ~900 |
| Baseline SMM (unconditional) | 0.26% |
| Baseline CPR (annualized) | 3.0% |
| XGBoost Test AUC (out-of-time) | ~0.72 |
| XGBoost Test Brier score | ~0.0036 |

**Top SHAP features:** remaining term, log UPB, loan age, refi incentive, num units, security rate, prepay penalty points. (Remaining term and log UPB dominate because near-maturity loans prepay more often and larger loans have more sophisticated borrowers.)

With 120 months of history the dataset is ~5× larger, so expect smoother S-curves and better out-of-time AUC.

## Refinance Incentive Calculation

The refi incentive measures how attractive it is for a borrower to refinance, accounting for the current prepayment penalty cost:

```
Refi Incentive (bps) = Net Coupon (bps) - [ PLC Rate (bps) + (1 + Prepay Penalty Points) * 12.5 ]
```

Where:
- **Net Coupon** = `loan_rate * 100` (the borrower's current note rate, in bps)
- **PLC Rate** = actual GNMA PLC rate for the period (from `GnmaPlcRatesHistorical.csv`, already in bps)
- **Prepay Penalty Points** = current penalty percentage from the declining schedule in `prepay_desc` (e.g., 10 in year 1, 9 in year 2, etc.)
- **12.5** = cost multiplier per penalty point (in bps)

**Example:** A loan with a 5.00% net coupon, 8 penalty points remaining, and a PLC rate of 400 bps:
```
500 - (400 + (1 + 8) * 12.5) = 500 - 512.5 = -12.5 bps
```
A negative refi incentive means refinancing is not economically attractive after accounting for the penalty cost.

### Penalty Schedule Parsing

The `prepay_desc` field contains the declining penalty schedule in various formats:
- `10,9,8,7,6,5,4,3,2,1,0` (comma-separated)
- `10/9/8/7/6/5/4/3/2/1% THRU 9/1/2034` (slash-separated with date)
- `0 LOCK, THEN 10,9,8,7,6,5,4,3,2,1,0` (lockout prefix)

The code parses the schedule, determines which year of the penalty period the loan is in, and looks up the corresponding penalty percentage. Values are capped at 10 (the maximum in any standard GNMA MF schedule) to handle the ~14 loans (0.08%) with garbled `prepay_desc` formats (typos, period-delimited, free-text narratives).

The PLC rates come from `GnmaPlcRatesHistorical.csv`, which contains actual GNMA-published monthly PLC rates. To update with new months, add rows to this CSV.

## Prepayment Identification Logic

Loans are tracked by `loan_id` across consecutive monthly periods. The following loans are **excluded** from prepayment calculations (`prepay_eligible = 0`):
- Loans in their **lockout period** (period < lockout_end_date)
- **Construction loans** (pool_type CL or CS) — these convert to PN on completion, and their disappearance is a pool type conversion, not a prepay

For eligible loans:

| Scenario | Flag |
|----------|------|
| `removal_reason = "1"` | `prepaid_voluntary = 1` |
| `removal_reason` in `("2","3","4","6")` | `prepaid_involuntary = 1` |
| Loan in period T but not T+1, and `months_dq = 0` | `prepaid_voluntary = 1` |
| Loan in period T but not T+1, and `months_dq > 0` | `prepaid_involuntary = 1` |
| Loan in final period (no T+1 data) | Only explicit `removal_reason` flags apply |

## Pool Types

| Code | Name | Prepay Eligible |
|------|------|----------------|
| PL | Level Payment Project Loan | Yes |
| PN | Non-Level Payment Project Loan | Yes |
| LM | Mature/Modified Loan | Yes |
| LS | Small Project Loan | Yes |
| RX | Mark-to-Market | Yes |
| CL | Construction Loan (Same Issuer) | No — converts to PN on completion |
| CS | Construction Loan (Diff Issuer) | No — converts to PN on completion |

## Source Data: mfplmon3 (V3.1 / V3.2 / V3.3)

GNMA publishes monthly `mfplmon3_YYYYMM.zip` files containing pipe-delimited text with one record per loan. The file has existed under three layout versions, all of which this project parses with automatic runtime detection by field count.

| Version | Effective | Pool Fields | Loan Fields | Total | Key Differences |
|---------|-----------|-------------|-------------|-------|-----------------|
| V3.1 | May 2022 – Feb 2023 | P1–P29 | L1–L44 | 73 | Affordable Status uses `NAF` for "not affordable" |
| V3.2 | Mar 2023 – May 2023 | P1–P29 | L1–L44 | 73 | Non-format change: `NAF` renamed to `MKT` in L44 |
| V3.3 | Jun 2023 – Present | P1–P31 | L1–L44 | 75 | Added P30 (Project Loan Security Rate) and P31 (Estimated Mortgage Amount) for CL/CS construction pools |

The parser detects the layout by counting fields in the first loan-level record of each file. `L_V31V32` is derived from the V3.3 dict by shifting all loan indices down by 2 (since V3.1/V3.2 don't have P30/P31), so the two stay in lockstep if V3.3 is ever edited. V3.1's `NAF` values are normalized to `MKT` at parse time so downstream code doesn't need to special-case them.

The layout specification PDFs are included in the repo for reference:
- `mfplmon3_layout_v31.pdf`
- `mfplmon3_layout_v32.pdf`
- `mfplmon3_layout_v33.pdf`

### URL routing (recent vs historical)

Recent months live in GNMA's `data_bulk/` directory; older months are archived to `data_history_cons/`. The downloader tries `data_bulk/` first and automatically falls back to `data_history_cons/` (with URL-encoded backslash, since GNMA uses Windows path conventions internally). Each downloaded zip is validated by opening it and checking for a real mfplmon entry, so the ~4 KB HTML error pages GNMA returns when a file doesn't exist can no longer slip through as apparent successes.

Downloaded files are cached in `gnma_mf_data/` and reused on subsequent runs. Only new months are downloaded.

| Source | URL | Auth |
|--------|-----|------|
| Disclosure Data Download | `ginniemae.gov/.../datadownload_bulk.aspx` | Email + security Q |
| Bulk File Server (recent) | `bulk.ginniemae.gov/protectedfiledownload.aspx?dlfile=data_bulk/...` | Session cookie |
| Bulk File Server (archive) | `bulk.ginniemae.gov/protectedfiledownload.aspx?dlfile=data_history_cons%5C...` | Session cookie |

## GNMA Authentication Details

The GNMA site uses SharePoint/ASP.NET with JavaScript-rendered forms. A plain `requests.get()` returns HTML with no visible input fields — they're injected by SharePoint's JS framework. This is why Playwright with a real browser (Firefox) is required.

The auth is a two-step form submission:
1. **Email** — field name contains `tbemailaddress`
2. **Security answer** — field ID contains a SharePoint web part GUID (`174dfd7c-a193-4313-a2ed-0005c00273fc`). The code has fallback CSS selectors in case this GUID changes.

After auth, cookies are transferred to a `requests.Session` for fast bulk downloads.

## Architecture & File Inventory

| File | Purpose |
|------|---------|
| `main.py` | Main downloader/parser: auth, download, parse, analytics, CSV output |
| `prepayment_analysis.py` | ML pipeline: empirical S-curves, XGBoost model, SHAP attribution |
| `generate_report.py` | HTML report generator — reads `analysis_results.json`, writes `prepayment_report.html` |
| `analysis_results.json` | Intermediate output of `prepayment_analysis.py` consumed by the report generator |
| `prepayment_report.html` | Self-contained interactive report with S-curves, feature importance, and sample loan attributions |
| `run.sh` | Shell wrapper: installs pip deps, checks Firefox, calls main.py |
| `GnmaPlcRatesHistorical.csv` | Monthly GNMA PLC rates (bps) for refi incentive calculation |
| `gnma_mf_raw_data_*.csv.gz` | Output: timestamped gzip-compressed enriched loan-month panel |
| `mfplmon3_layout_v31.pdf` | GNMA layout specification for V3.1 (May 2022 – Feb 2023) |
| `mfplmon3_layout_v32.pdf` | GNMA layout specification for V3.2 (Mar 2023 – May 2023) |
| `mfplmon3_layout_v33.pdf` | GNMA layout specification for V3.3 (Jun 2023 – Present) |
| `requirements.txt` | Python dependencies: requests, pandas, numpy, playwright, scikit-learn, xgboost, shap |
| `.replit` | Replit Run button configuration (120 months) |
| `replit.nix` | Nix system dependencies for Replit |
| `.gitignore` | Excludes downloaded zip files, xlsx, debug screenshots, and Replit caches |

### Key functions in `main.py`

| Function | What it does |
|----------|-------------|
| `discover_nix_libs()` | Finds NixOS library paths for Firefox (Replit-specific) |
| `authenticate_gnma()` | Playwright Firefox auth flow, returns `requests.Session` with cookies |
| `_ensure_data_dir()` | Creates `gnma_mf_data/` if missing; emits a clear actionable error if the path exists as a file instead of a directory |
| `_validate_mfplmon_zip()` | Opens a downloaded zip and verifies it contains a real mfplmon entry (catches the ~4 KB HTML error pages GNMA returns for missing files) |
| `_attempt_download()` | Tries a single download URL and validates the result; shared by the primary + fallback URL attempts |
| `download_files()` | Downloads mfplmon3 zips using the authenticated session, with `data_bulk/` → `data_history_cons/` fallback and a missing-period summary at the end |
| `read_mfplmon3()` | Parses pipe-delimited file into list of dicts with V3.1 / V3.2 / V3.3 runtime dispatch |
| `load_plc_rates()` | Reads `GnmaPlcRatesHistorical.csv`, returns monthly PLC rates in bps |
| `parse_penalty_schedule()` | Parses declining penalty schedule from `prepay_desc` string |
| `get_current_penalty_points()` | Looks up current penalty % based on year within penalty period |
| `build_analytics()` | Adds lockout/penalty status, prepayment flags, refi incentive |
| `write_csv()` | Writes enriched DataFrame to timestamped gzipped CSV |

## Replit-Specific Notes

**Why Firefox instead of Chromium?** Playwright's Chromium requires `GLIBC_PRIVATE` symbols not present in Replit's NixOS libc. Firefox works.

**Nix library discovery:** Replit installs packages into `/nix/store/<hash>-<pkg>/lib/` instead of `/usr/lib/`. The `discover_nix_libs()` function scans `/nix/store` entries with Python's `os.listdir()` + substring filtering (instant) rather than shell globs (which hang on the tens of thousands of nix store entries).

**Firefox sandbox:** Replit containers don't support `CanCreateUserNamespace()`. Firefox sandbox is disabled via `MOZ_DISABLE_CONTENT_SANDBOX=1` and `security.sandbox.content.level: 0`.

### First-time Replit setup
```bash
nix-env -iA nixpkgs.nspr nixpkgs.nss nixpkgs.atk nixpkgs.cups \
  nixpkgs.libdrm nixpkgs.xorg.libX11 nixpkgs.gtk3 nixpkgs.pango \
  nixpkgs.cairo nixpkgs.mesa.drivers nixpkgs.alsa-lib nixpkgs.dbus \
  nixpkgs.glib nixpkgs.expat nixpkgs.libxkbcommon \
  nixpkgs.xorg.libXcomposite nixpkgs.xorg.libXext \
  nixpkgs.xorg.libXfixes nixpkgs.xorg.libXrandr nixpkgs.xorg.libxcb \
  nixpkgs.fontconfig nixpkgs.freetype nixpkgs.gdk-pixbuf \
  nixpkgs.xorg.libXrender
python3 -m playwright install firefox
```

## Downstream Analysis

The simplest path is the built-in ML pipeline described in [ML Prepayment Analysis](#ml-prepayment-analysis):

```bash
python3 prepayment_analysis.py   # trains XGBoost + SHAP
python3 generate_report.py       # builds prepayment_report.html
```

These two scripts take the CSV output of `main.py` and produce a full set of S-curves, a calibrated XGBoost prepayment model, and an interactive HTML report with per-loan attribution. No additional code needed.

### Rolling your own S-curves from the CSV

If you'd rather build analytics yourself, the CSV has everything you need:

1. **Filter** to `prepay_eligible = 1` (excludes lockout and construction loans)
2. **Group** by `refi_incentive_bps` buckets (e.g., 25bps or 50bps bins)
3. **Compute CPR** per bucket: `CPR = 1 - (1 - prepaid_voluntary/n)^12`
4. **Plot** CPR vs refi incentive — this is the S-curve
5. **Segment** by `in_prepay_penalty` vs `past_all_restrictions`, `property_state`, `fha_program_code`, `green_status`, loan age, etc.
6. **Fit** a logistic model: `P(prepay) = f(refi_incentive, age, penalty_status, loan_size, ...)`

### Computing SMM from the raw data
For loans appearing in consecutive periods:
```
SMM = 1 - (upb_T+1 / upb_T)
CPR = 1 - (1 - SMM)^12
```

### Loan age
```
age_months = (period_year - first_pay_year) * 12 + (period_month - first_pay_month)
```

## Known Limitations

- **History goes back to May 2022 (V3.1)** — the parser supports V3.1, V3.2, and V3.3 layouts, all under the `mfplmon3_*.zip` filename. Earlier periods use pre-V3.1 layouts (V2.0, V3.0) that this project does not parse. If you need data before May 2022, you'd need to add layout dicts for those older versions.
- **GNMA form field IDs are hardcoded** — the SharePoint web part GUID could change if GNMA redesigns their site. Fallback CSS selectors exist but haven't been tested against a redesign. The auth flow also has an occasional cold-start race where Playwright's `fill()` runs before SharePoint JS has wired up the form; a plain retry almost always succeeds.
- **Terminated pools not tracked** — pools where all loans prepay and the pool is removed entirely from mfplmon3 could be captured via the separate `mftermpools` file.
- **PLC rate file needs periodic updates** — `GnmaPlcRatesHistorical.csv` must be updated with new monthly PLC rates as they are published.
- **~14 loans (0.08%) have garbled `prepay_desc`** — penalty points are capped at 10 for these. Affected formats include period-delimited schedules (`10.9.8...`), typos creating concatenated numbers (`76` instead of `7,6`), and free-text narratives with embedded dates.
- **CL/CS construction loan conversions** — when a CL pool converts to PN, the same case_number gets a new CUSIP. The CL disappearance is correctly excluded from prepayment counts, but the PN loan appears as a "new" loan with no prior history in the panel.
- **`proj_loan_sec_rate` and `est_mtg_amount` are V3.3-only** — these CL/CS-specific pool fields don't exist in V3.1/V3.2, so rows from older months will have NaN in these columns. The prepayment analysis doesn't use them anyway (construction loans are excluded from `prepay_eligible`), so this is not a functional limitation.

## Changelog

| Date | Change |
|------|--------|
| 2026-04-09 | Initial version: auth, download, parse, raw CSV output |
| 2026-04-09 | Fix nix library discovery (moved to Python, eliminated shell hang) |
| 2026-04-09 | Fix Firefox launch (alsa-lib, sandbox disabled for Replit) |
| 2026-04-09 | Add refi incentive calculation (originally 10yr Treasury + 70bps) |
| 2026-04-09 | Switch to direct GNMA PLC rates (GnmaPlcRatesHistorical.csv) |
| 2026-04-09 | Exclude lockout-period loans from prepayment calculations |
| 2026-04-09 | Exclude CL/CS construction loans from prepayment calculations |
| 2026-04-09 | Parse declining penalty schedule from prepay_desc |
| 2026-04-09 | Cap prepay_penalty_points at 10 for garbled formats |
| 2026-04-09 | Gzipped output (.csv.gz) to stay under GitHub 100MB limit |
| 2026-04-09 | Timestamped output filenames for run history |
| 2026-04-09 | Expand to 24 months of history |
| 2026-04-10 | Add ML prepayment analysis pipeline (`prepayment_analysis.py`, `generate_report.py`) — XGBoost + SHAP with interactive HTML report |
| 2026-04-10 | `prepayment_analysis.py` auto-detects the most recent `gnma_mf_raw_data_*.csv.gz` (no hardcoded filename) |
| 2026-04-10 | Add `_validate_mfplmon_zip()` to catch silent download failures (4 KB HTML error pages from GNMA) |
| 2026-04-10 | Add `data_bulk/` → `data_history_cons/` URL fallback for historical months |
| 2026-04-10 | Add V3.1 / V3.2 / V3.3 runtime-detected parsing with `NAF` → `MKT` normalization |
| 2026-04-10 | Add `proj_loan_sec_rate` and `est_mtg_amount` to output CSV (NaN for V3.1/V3.2) |
| 2026-04-10 | Add `_ensure_data_dir()` helper with clear error when `gnma_mf_data` exists as a file instead of a directory |
| 2026-04-10 | Remove orphaned `download` and `gitignore` (missing-dot) files; add Replit cache dirs to `.gitignore` |
| 2026-04-10 | Expand default to 120 months of history (maximum available back to V3.1 introduction) |
