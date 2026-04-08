# GNMA Multifamily Loan Data Downloader & Prepayment S-Curve Builder

## Goal

Build a historical loan-level panel dataset of all Ginnie Mae multifamily (project loan) MBS pools, suitable for estimating a prepayment S-curve. The tool should:

1. **Authenticate** with the GNMA Disclosure Data Download website (which requires email + security question — no public API exists)
2. **Download** monthly multifamily portfolio files (`mfplmon3_YYYYMM.zip`) in bulk
3. **Parse** the pipe-delimited V3.3 format (31 pool-level fields + 44 loan-level fields per record)
4. **Track loans across months** to identify which loans prepaid (voluntary payoff vs involuntary removal)
5. **Output** a structured Excel workbook with summary stats, S-curve rate buckets, lockout analysis, and a full loan-month panel

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      run.sh                              │
│  - Discovers nix library paths for Firefox               │
│  - Sets LD_LIBRARY_PATH                                  │
│  - Calls main.py with all arguments                      │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                     main.py                              │
│                                                          │
│  Phase 1: Auth + Download                                │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Playwright (Firefox headless)                      │  │
│  │  Step 1: Navigate to GNMA download page            │  │
│  │  Step 2: Fill email → submit (ASP.NET postback)    │  │
│  │  Step 3: Fill security answer → submit             │  │
│  │  Step 4: Extract cookies → transfer to requests    │  │
│  └────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────┐  │
│  │ requests (with stolen cookies)                     │  │
│  │  Download mfplmon3_YYYYMM.zip files from           │  │
│  │  bulk.ginniemae.gov/protectedfiledownload.aspx     │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  Phase 2: Parse + Build (LD_LIBRARY_PATH cleared)        │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Parse mfplmon3 V3.3 pipe-delimited files           │  │
│  │ Build loan-month panel                             │  │
│  │ Identify prepayments (month-over-month tracking)   │  │
│  │ Write Excel (pandas ExcelWriter only, no XML)      │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## Key Data Sources

| Source | URL | Auth Required | What It Provides |
|--------|-----|---------------|------------------|
| Disclosure Data Download | `ginniemae.gov/data_and_reports/disclosure_data/Pages/datadownload_bulk.aspx` | Yes (email + security Q) | Monthly `mfplmon3` pool/loan files |
| Bulk File Server | `bulk.ginniemae.gov/protectedfiledownload.aspx?dlfile=data_bulk/mfplmon3_YYYYMM.zip` | Yes (session cookie) | Direct zip download |
| MF Database Search | `structuredginniemaes.ginniemae.gov/multifam/` | No | Individual pool lookup (no bulk/API) |
| Disclosure Data Search | `ginniemae.gov/investors/investor_search_tools/Pages/default.aspx` | No | REMIC CUSIP → collateral mapping |

**All bulk download endpoints require authentication.** There is no public API. The download page uses ASP.NET with a two-step auth flow (email, then security question on a second page load).

## GNMA Authentication Flow (Discovered via Playwright Inspection)

The GNMA Disclosure site uses a SharePoint/ASP.NET profile page at `ginniemae.gov/Pages/profile.aspx`. The auth is a **two-step form submission**:

1. **Step 1 — Email**: The page loads with an email text input. The field name contains `tbemailaddress` (case-insensitive). Submit via the first visible `input[type="submit"]`. This triggers an ASP.NET postback.

2. **Step 2 — Security Answer**: After the email postback, the page reloads with a new text input for the security answer. The exact field ID discovered:
   - Answer input: `#ctl00_ctl45_g_174dfd7c_a193_4313_a2ed_0005c00273fc_ctl00_tbAnswer`
   - Submit button: `#ctl00_ctl45_g_174dfd7c_a193_4313_a2ed_0005c00273fc_ctl00_btnAnswerSecret`
   
   **Note**: These IDs contain a SharePoint web part GUID (`174dfd7c-a193-4313-a2ed-0005c00273fc`). If GNMA redesigns the page, these IDs will change. The code has fallback selectors (`input[name*="Answer" i]`).

3. **After auth**: The browser is redirected to the original download page. Cookies are extracted and transferred to a `requests.Session` for fast bulk downloads.

**Why not just use `requests`?** The profile page requires JavaScript to render the form fields. A plain `requests.get()` returns HTML with no visible input fields — they're injected by SharePoint's JS framework. This is why Playwright (real browser) is required for auth.

## File Format: mfplmon3 (V3.3)

Pipe-delimited text file, one record per line. Each record contains pool-level data followed by loan-level data.

**Pool fields (P1–P31, indices 0–30):**
- CUSIP, pool number, pool type (PL/PN/LM/LS/RX/CL/CS), security rate, issue date, maturity date, original amount, issuer info, UPB, delinquency counts (30/60/90 day), security RPB, factor

**Loan fields (L1–L44, indices 31–74):**
- Case number, agency type, loan rate, term, maturity, origination date, lockout term/end date, prepay penalty period/end date, original balance, current UPB, delinquency months, removal reason, property info (name, city, state, MSA, units), FHA program code, green/affordable status

**Pool types included in S-curve (excludes CL/CS construction):**
- PL: Level Payment Project Loan
- PN: Non-Level Payment Project Loan  
- LM: Mature/Modified Loan
- LS: Small Project Loan
- RX: Mark-to-Market

## Prepayment Identification Logic

Loans are tracked by unique ID (`pool_cusip + "_" + case_number`) across consecutive months:

| Scenario | Flag |
|----------|------|
| `removal_reason = "1"` in current month | `prepaid_voluntary = 1` |
| `removal_reason` in `("2","3","4","6")` | `prepaid_involuntary = 1` |
| Loan exists in month T but not T+1, and `months_dq = 0` | `prepaid_voluntary = 1` |
| Loan exists in month T but not T+1, and `months_dq > 0` | `prepaid_involuntary = 1` |
| Loan in final period (no T+1 data) | Only explicit flags apply |

## Output Excel Tabs

| Tab | Content |
|-----|---------|
| Instructions | Generation metadata, how to complete the S-curve |
| Summary | Monthly aggregates: loan count, UPB, vol/invol prepays, CPR, WA rate, WA age, % lockout/penalty/open |
| S-Curve Buckets | Prepayment rates by 25bp loan rate buckets (raw S-curve shape) |
| Lockout Analysis | CPR segmented by in-lockout vs in-penalty vs open (key MF dimension) |
| Loan Panel | Full loan-month panel with all S-curve variables (key columns) |
| Full Detail | Every parsed field |

## S-Curve Completion (User Action Required)

The dataset includes `benchmark_rate` and `refi_incentive_bps` columns left blank. To complete:

1. Fill `benchmark_rate` with the prevailing GNMA MF coupon rate for each period (e.g., FHA 223(f) refinance rate, or 10yr Treasury + typical MF spread)
2. Compute `refi_incentive_bps = (loan_rate - benchmark_rate) × 10000`
3. Re-bucket by incentive instead of absolute rate
4. Fit logistic: `P(prepay) = f(incentive, age, lockout_status, penalty_status, loan_size, ...)`

## Problems Encountered & Solutions

### Problem 1: No Public API
**Issue**: GNMA has no REST API for bulk data. All download URLs (`bulk.ginniemae.gov/protectedfiledownload.aspx`) redirect to a login/profile page. Even `data.gov` links route back to the gated GNMA site.

**Solution**: Browser automation (Playwright) to complete the auth flow, then cookie transfer to `requests` for fast bulk downloads.

### Problem 2: ASP.NET JavaScript-Rendered Forms  
**Issue**: `requests + BeautifulSoup` cannot find the form fields on the GNMA profile page because they're injected by SharePoint's JavaScript framework. The raw HTML contains no `<input>` elements for email/answer.

**Solution**: Playwright with a real browser engine (Firefox) renders the JavaScript, making the form fields visible and fillable.

### Problem 3: Two-Step Auth Flow
**Issue**: The GNMA profile page has a two-step flow (email → postback → security answer → postback). The initial script assumed a single-page form and tried to fill both fields at once, but after the email submit, the page reloads with completely different fields.

**Solution**: Discovered via Playwright inspection that step 1 shows email fields and step 2 shows the answer field with specific ASP.NET control IDs. The script now waits for each postback before proceeding.

### Problem 4: Chromium Won't Launch on Replit (GLIBC Mismatch)
**Issue**: Playwright's bundled Chromium requires `GLIBC_PRIVATE` symbols not present in Replit's system libc. Error: `version 'GLIBC_PRIVATE' not found`. This is a fundamental binary incompatibility — no amount of library installation fixes it.

**Solution**: Switched from Chromium to **Firefox**, which has fewer system dependencies and doesn't hit the GLIBC mismatch.

### Problem 5: Missing Shared Libraries on Replit (Nix Store)
**Issue**: Replit uses NixOS, which installs packages into `/nix/store/<hash>-<pkg>/lib/` instead of standard `/usr/lib/`. Firefox can't find `libnspr4.so`, `libgbm.so.1`, `libfontconfig.so.1`, `libgtk-3.so.0`, etc.

**Solution**: 
- Installed libs via `nix-env -iA nixpkgs.<pkg>` (nspr, nss, atk, cups, libdrm, gtk3, pango, cairo, fontconfig, freetype, gdk-pixbuf, etc.)
- Created `run.sh` that discovers nix library paths and sets `LD_LIBRARY_PATH` before launching the script
- The path discovery uses `ls -dt` (sorted by modification time) to find the latest `user-environment/lib`, plus targeted lookups for fontconfig, gtk3, gdk-pixbuf, and other libs that live in separate nix derivations

### Problem 6: Expat/XML Conflict During Excel Write
**Issue**: The `LD_LIBRARY_PATH` set for Firefox pulls in a nix version of `libexpat` that conflicts with Python's built-in `pyexpat` module. When `openpyxl` calls `load_workbook()` (which uses XML parsing internally), it crashes with `ImportError: No module named expat`.

**Solution**: Two-part fix:
1. `main.py` calls `os.environ.pop('LD_LIBRARY_PATH', None)` after the download phase completes but before the Excel writing phase
2. `write_output()` was rewritten to use only `pandas.ExcelWriter` (write-only) instead of `openpyxl.load_workbook()` (read-back), eliminating all XML parsing from the output path

### Problem 7: `find /nix/store` Is Extremely Slow
**Issue**: Replit's `/nix/store` contains thousands of packages. Running `find /nix/store -name "libfoo.so"` takes minutes, making the `LD_LIBRARY_PATH` setup painfully slow.

**Solution**: `run.sh` uses targeted `ls` globs (e.g., `ls /nix/store/*fontconfig*-lib/lib/libfontconfig.so.1`) instead of recursive `find`. This is near-instant because it uses filename pattern matching rather than directory traversal.

## What Is Working

- ✅ Firefox launches successfully on Replit
- ✅ Two-step GNMA authentication (email → security answer)
- ✅ Cookie transfer from Playwright to requests session
- ✅ Bulk download of mfplmon3 zip files from bulk.ginniemae.gov
- ✅ Parsing of mfplmon3 V3.3 pipe-delimited format (all 75 fields)
- ✅ Loan tracking across months and prepayment identification
- ✅ Excel output with Summary, S-Curve Buckets, Lockout Analysis, Loan Panel tabs
- ✅ Tested with real data: 5 periods, ~15,400 records/month, 75,071 loan-month observations, 248 voluntary prepays, 10 involuntary removals

## What May Need Attention

- ⚠️ **GNMA form field IDs are hardcoded**: The SharePoint web part GUID in the answer field ID (`174dfd7c-a193-4313-a2ed-0005c00273fc`) could change if GNMA updates their site. Fallback selectors exist but haven't been tested against a redesign.
- ⚠️ **Nix library paths are fragile**: Every `nix-env -iA` install creates a new `user-environment` hash. `run.sh` handles this by finding the latest one, but a complete Replit environment rebuild could require re-running `nix-env -iA` for all packages.
- ⚠️ **`mfplmon3_202603.zip` was only 4 KB**: This suggests the March 2026 file may not have been fully released yet at download time, or the period doesn't exist. The parser silently skips files with no valid records.
- ⚠️ **Benchmark rate not auto-populated**: The S-curve dataset requires the user to manually fill in the prevailing GNMA MF rate for each period. A future enhancement could pull Treasury rates automatically.
- ⚠️ **No terminated pool tracking yet**: Pools that fully pay off (all loans prepay) and are removed from mfplmon3 entirely could be captured via the separate `mftermpools` file.

## Replit One-Time Setup

```bash
# Install system dependencies via nix
nix-env -iA nixpkgs.nspr nixpkgs.nss nixpkgs.atk nixpkgs.cups \
  nixpkgs.libdrm nixpkgs.xorg.libX11 nixpkgs.gtk3 nixpkgs.pango \
  nixpkgs.cairo nixpkgs.mesa.drivers nixpkgs.alsa-lib nixpkgs.dbus \
  nixpkgs.glib nixpkgs.expat nixpkgs.libxkbcommon \
  nixpkgs.xorg.libXcomposite nixpkgs.xorg.libXext \
  nixpkgs.xorg.libXfixes nixpkgs.xorg.libXrandr nixpkgs.xorg.libxcb \
  nixpkgs.fontconfig nixpkgs.freetype nixpkgs.gdk-pixbuf \
  nixpkgs.xorg.libXrender

# Install Firefox for Playwright
python3 -m playwright install firefox
```

## Usage

```bash
# Download 6 months + build dataset
bash run.sh --email nsejnost@gmail.com --answer "Red"

# Download 12 months
bash run.sh --email nsejnost@gmail.com --answer "Red" --months 12

# Parse already-downloaded files (no browser needed)
bash run.sh --skip-download

# Use custom data directory
python3 main.py --skip-download --data-dir /path/to/files
```

## File Inventory

| File | Purpose |
|------|---------|
| `main.py` | Main script: auth, download, parse, panel construction, Excel output |
| `run.sh` | Shell wrapper: nix library discovery, LD_LIBRARY_PATH setup, runs main.py |
| `requirements.txt` | Python dependencies (requests, pandas, openpyxl, playwright, etc.) |
| `.replit` | Replit run button configuration |
| `replit.nix` | Nix system dependencies declaration (may not take effect on all Replit plans) |
| `.gitignore` | Excludes downloaded data files, xlsx output, debug screenshots from git |
| `README.md` | This file |
