# GNMA Multifamily S-Curve Dataset Builder

Authenticates with the Ginnie Mae Disclosure Data Download site, downloads multifamily monthly portfolio files (`mfplmon3`), tracks loans across months, identifies prepayments, and outputs a panel dataset in Excel for prepayment S-curve estimation.

## Quick Start (Replit)

### One-time setup (in Shell):
```bash
# Install Firefox for Playwright
python3 -m playwright install firefox

# Install nix dependencies (if Firefox complains about missing libs)
nix-env -iA nixpkgs.fontconfig nixpkgs.freetype nixpkgs.gdk-pixbuf nixpkgs.xorg.libXrender
```

### Run:
```bash
bash run.sh --email your@email.com --answer "YourSecurityAnswer"
```

Or with more months:
```bash
bash run.sh --email your@email.com --answer "YourSecurityAnswer" --months 12
```

### Parse existing files only:
```bash
python3 main.py --skip-download
```

## Files

| File | Purpose |
|------|---------|
| `main.py` | Main script — auth, download, parse, build S-curve dataset |
| `run.sh` | Helper that sets `LD_LIBRARY_PATH` for Replit and runs `main.py` |
| `requirements.txt` | Python dependencies |
| `.replit` | Replit run configuration |
| `replit.nix` | Nix system dependencies for Firefox |

## How It Works

1. **Authentication**: Uses Playwright (Firefox) to complete GNMA's two-step auth flow (email → security question)
2. **Cookie Transfer**: Extracts session cookies from Firefox and transfers them to a `requests` session for fast bulk downloads
3. **Download**: Downloads `mfplmon3_YYYYMM.zip` files from `bulk.ginniemae.gov`
4. **Parse**: Reads pipe-delimited mfplmon3 V3.3 format (P1-P31 pool + L1-L44 loan fields)
5. **Panel Construction**: Tracks each loan across months, identifies prepayments by:
   - `removal_reason = 1` (Mortgagor Payoff)
   - Loan disappears between months while current → voluntary prepay
   - Loan disappears while delinquent → involuntary removal
6. **Output**: Excel workbook with Summary, S-Curve Buckets, Lockout Analysis, Loan Panel, and Full Detail tabs

## Output Tabs

- **Summary**: Monthly aggregates — loan counts, CPR, WA rate, WA age, lockout/penalty %
- **S-Curve Buckets**: Prepayment rates by 25bp rate buckets (raw S-curve shape)
- **Lockout Analysis**: CPR segmented by in-lockout vs in-penalty vs open
- **Loan Panel**: Full loan-month panel with all S-curve variables
- **Full Detail**: Every field from the source data

## Completing the S-Curve

Fill in the `benchmark_rate` column in the Loan Panel tab with the prevailing GNMA MF rate for each period, then compute:
```
refi_incentive_bps = (loan_rate - benchmark_rate) × 10000
```

Re-bucket by incentive and fit a logistic model.
