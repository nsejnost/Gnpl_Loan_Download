"""
GNMA Multifamily Data Downloader & Raw Data Exporter
=====================================================
Authenticates with GNMA Disclosure Data Download using Firefox (Playwright),
downloads mfplmon3 monthly portfolio files, parses the V3.3 pipe-delimited
format, and outputs all raw loan data as a single CSV.

Usage:
  bash run.sh --email you@email.com --answer "YourAnswer"
  bash run.sh --email you@email.com --answer "YourAnswer" --months 12
  bash run.sh --skip-download
  python3 main.py --skip-download --data-dir ./my_files
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta

try:
    import requests
    import pandas as pd
    import numpy as np
except ImportError as e:
    print(f"Missing: {e}\nInstall: pip install requests pandas numpy")
    sys.exit(1)

# ─── CONFIGURATION ────────────────────────────────────────
BASE_URL = "https://www.ginniemae.gov"
BULK_URL = "https://bulk.ginniemae.gov"
DOWNLOAD_PAGE = f"{BASE_URL}/data_and_reports/disclosure_data/Pages/datadownload_bulk.aspx"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "gnma_mf_data")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

POOL_TYPE_NAMES = {
    'PL': 'Project Loan', 'PN': 'Non-Level PL', 'LM': 'Mature/Modified',
    'LS': 'Small PL', 'RX': 'Mark-to-Market',
    'CL': 'Construction (Same)', 'CS': 'Construction (Diff)',
}
P = {
    'cusip': 0, 'pool_number': 1, 'pool_indicator': 2, 'pool_type': 3,
    'security_rate': 4, 'issue_date': 5, 'maturity_date': 6, 'orig_agg_amount': 7,
    'issuer_number': 8, 'issuer_name': 9,
    'pool_upb': 16, 'num_loans': 17,
    'num_30dq': 18, 'upb_30dq': 19, 'pct_30dq': 20,
    'num_60dq': 21, 'upb_60dq': 22, 'pct_60dq': 23,
    'num_90dq': 24, 'upb_90dq': 25, 'pct_90dq': 26,
    'security_rpb': 27, 'rpb_factor': 28,
    'proj_loan_sec_rate': 29, 'est_mtg_amount': 30,
}
L = {
    'disclosure_seq': 31, 'case_number': 32, 'agency_type': 33, 'loan_type': 34,
    'loan_term': 35, 'first_pay_date': 36, 'loan_maturity_date': 37, 'loan_rate': 38,
    'modified_ind': 39, 'non_level_ind': 40, 'mature_loan_flag': 41,
    'origination_date': 42, 'initial_endorsement': 43, 'final_endorsement': 44,
    'lockout_term': 45, 'lockout_end_date': 46,
    'prepay_premium_period': 47, 'prepay_end_date': 48,
    'interest_approval_date': 49, 'prepay_penalty_flag': 50,
    'orig_prin_bal': 51, 'upb_at_issuance': 52, 'upb': 53,
    'draw_number': 54, 'approved_draw_amt': 55,
    'months_dq': 56, 'liquidation_flag': 57, 'removal_reason': 58,
    'seller_issuer_id': 59,
    'property_name': 60, 'property_street': 61, 'property_city': 62,
    'property_state': 63, 'property_zip': 64, 'msa': 65,
    'num_units': 66, 'pi_amount': 67,
    'prepay_desc': 68, 'non_level_desc': 69,
    'fha_program_code': 70, 'insurance_type': 71,
    'as_of_date': 72, 'green_status': 73, 'affordable_status': 74,
}

GNMA_EMAIL_SELECTOR = 'input[name*="tbemailaddress" i]'
GNMA_ANSWER_ID = '#ctl00_ctl45_g_174dfd7c_a193_4313_a2ed_0005c00273fc_ctl00_tbAnswer'
GNMA_ANSWER_SUBMIT_ID = '#ctl00_ctl45_g_174dfd7c_a193_4313_a2ed_0005c00273fc_ctl00_btnAnswerSecret'

OUTPUT_CSV = os.path.join(SCRIPT_DIR, "gnma_mf_raw_data.csv")


# ═══════════════════════════════════════════════════════════
#  NIX LIBRARY DISCOVERY (replaces run.sh glob-based approach)
# ═══════════════════════════════════════════════════════════

def discover_nix_libs():
    """Set LD_LIBRARY_PATH for Playwright Firefox on NixOS/Replit.

    Uses os.listdir('/nix/store') + substring filtering instead of shell globs.
    Shell globs like `ls /nix/store/*/${lib}` expand against every nix store
    entry (tens of thousands) and hang. This scans the directory listing once
    and does fast string matching in Python.
    """
    nix_store = '/nix/store'
    if not os.path.isdir(nix_store):
        return

    print("[setup] Discovering nix library paths...")
    try:
        entries = os.listdir(nix_store)
    except OSError:
        print("[setup] WARNING: Cannot read /nix/store")
        return

    lib_dirs = []

    # 1. Latest user-environment/lib (has most nix-env installed libs via symlinks)
    user_envs = [e for e in entries if '-user-environment' in e]
    if user_envs:
        user_envs.sort(
            key=lambda e: os.path.getmtime(os.path.join(nix_store, e)),
            reverse=True
        )
        ue_lib = os.path.join(nix_store, user_envs[0], 'lib')
        if os.path.isdir(ue_lib):
            lib_dirs.append(ue_lib)
            print(f"  user-environment: {ue_lib}")

    # 2. Libraries that often live in separate nix derivations outside user-environment.
    #    Each tuple: (substring to match in store entry name, .so file to verify)
    targets = [
        ('fontconfig-2', 'libfontconfig.so.1'),
        ('gtk+3-3', 'libgtk-3.so.0'),
        ('gdk-pixbuf-2', 'libgdk_pixbuf-2.0.so.0'),
        ('pango-1', 'libpango-1.0.so.0'),
        ('libX11-1', 'libX11.so.6'),
        ('libXrender-0', 'libXrender.so.1'),
        ('freetype-2', 'libfreetype.so.6'),
        ('dbus-1', 'libdbus-1.so.3'),
        ('atk-2', 'libatk-1.0.so.0'),
        ('alsa-lib-1', 'libasound.so.2'),
    ]

    for pkg_substr, lib_file in targets:
        matches = [e for e in entries if pkg_substr in e]
        for entry in matches:
            lib_path = os.path.join(nix_store, entry, 'lib', lib_file)
            if os.path.exists(lib_path):
                lib_dir = os.path.join(nix_store, entry, 'lib')
                if lib_dir not in lib_dirs:
                    lib_dirs.append(lib_dir)
                    print(f"  {pkg_substr}: {lib_dir}")
                break

    if lib_dirs:
        os.environ['LD_LIBRARY_PATH'] = ':'.join(lib_dirs)
        print(f"[setup] LD_LIBRARY_PATH set ({len(lib_dirs)} dirs)")
    else:
        print("[setup] WARNING: No nix library paths found")


# ═══════════════════════════════════════════════════════════
#  GNMA AUTHENTICATION & DOWNLOAD
# ═══════════════════════════════════════════════════════════

def ensure_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return True
    except ImportError:
        print("\n[setup] Installing playwright...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "firefox"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            from playwright.sync_api import sync_playwright
            print("[setup] ok Installed")
            return True
        except Exception as e:
            print(f"[setup] x Failed: {e}")
            return False


def authenticate_gnma(email, answer):
    from playwright.sync_api import sync_playwright

    print("\n[auth] Launching Firefox...")
    pw = sync_playwright().start()

    # Replit containers don't support user namespaces, so disable Firefox sandbox
    os.environ['MOZ_DISABLE_CONTENT_SANDBOX'] = '1'
    browser = pw.firefox.launch(
        headless=True,
        firefox_user_prefs={
            'security.sandbox.content.level': 0,
        },
    )
    ctx = browser.new_context(user_agent=UA, accept_downloads=True)
    page = ctx.new_page()

    try:
        # Step 1: Navigate
        print("[auth] Step 1: Loading GNMA profile page...")
        page.goto(DOWNLOAD_PAGE, wait_until="networkidle", timeout=60000)
        time.sleep(3)

        if "profile" not in page.url.lower():
            print("[auth] ok Already authenticated")
        else:
            # Step 2: Email
            print("[auth] Step 2: Submitting email...")
            try:
                page.fill(GNMA_EMAIL_SELECTOR, email)
            except Exception:
                inputs = page.query_selector_all('input[type="text"]')
                for inp in inputs:
                    if inp.is_visible():
                        inp.fill(email)
                        break
            for sel in ['input[type="submit"]', 'button[type="submit"]']:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    break
            page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(3)

            # Step 3: Security answer
            print("[auth] Step 3: Submitting security answer...")
            try:
                page.fill(GNMA_ANSWER_ID, answer)
                page.click(GNMA_ANSWER_SUBMIT_ID)
            except Exception:
                answer_el = page.query_selector('input[name*="Answer" i]') or \
                            page.query_selector('input[name*="answer" i]')
                if answer_el:
                    answer_el.fill(answer)
                submit_el = page.query_selector('input[name*="btnAnswer" i]') or \
                            page.query_selector('input[type="submit"]')
                if submit_el and submit_el.is_visible():
                    submit_el.click()
            page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(5)

        if "profile" in page.url.lower():
            page.goto(DOWNLOAD_PAGE, wait_until="networkidle", timeout=30000)
            time.sleep(3)

        if "profile" in page.url.lower():
            print("[auth] x Authentication failed")
            page.screenshot(path=os.path.join(SCRIPT_DIR, "gnma_debug.png"), full_page=True)
            browser.close(); pw.stop()
            return None

        print("[auth] ok Authentication successful!")

        # Transfer cookies to requests
        session = requests.Session()
        for cookie in ctx.cookies():
            session.cookies.set(cookie['name'], cookie['value'],
                                domain=cookie.get('domain', ''), path=cookie.get('path', '/'))
        session.headers.update({"User-Agent": UA})
        browser.close(); pw.stop()
        return session

    except Exception as e:
        print(f"[auth] x Error: {e}")
        try: page.screenshot(path=os.path.join(SCRIPT_DIR, "gnma_debug.png"), full_page=True)
        except: pass
        browser.close(); pw.stop()
        return None


def get_file_list(months=6):
    now = datetime.now()
    files = []
    for i in range(months + 2):
        dt = now - timedelta(days=30 * (i + 1))
        period = dt.strftime("%Y%m")
        fn = f"mfplmon3_{period}.zip"
        url = f"{BULK_URL}/protectedfiledownload.aspx?dlfile=data_bulk/{fn}"
        files.append({"period": period, "filename": fn, "url": url})
    return files[:months]


def download_files(session, months=6):
    os.makedirs(DATA_DIR, exist_ok=True)
    files = get_file_list(months)
    print(f"\n[download] Downloading {len(files)} files to {DATA_DIR}\n")
    downloaded = 0
    for f in files:
        dest = os.path.join(DATA_DIR, f["filename"])
        if os.path.exists(dest) and os.path.getsize(dest) > 1000:
            print(f"  ok {f['filename']} (cached, {os.path.getsize(dest)//1024} KB)")
            downloaded += 1; continue
        try:
            r = session.get(f["url"], timeout=120, stream=True, allow_redirects=True)
            if "profile" in r.url.lower():
                print(f"  x {f['filename']} — session expired"); continue
            ct = r.headers.get("Content-Type", "")
            if "text/html" in ct and "profile" in (r.text[:500] if hasattr(r,'text') else "").lower():
                print(f"  x {f['filename']} — login redirect"); continue
            if r.status_code != 200:
                print(f"  x {f['filename']} — HTTP {r.status_code}"); continue
            total = int(r.headers.get("Content-Length", 0))
            dl = 0
            with open(dest, "wb") as out:
                for chunk in r.iter_content(65536):
                    out.write(chunk); dl += len(chunk)
                    if total:
                        print(f"\r  > {f['filename']} {dl//1024}/{total//1024} KB ({dl*100//total}%)",
                              end="", flush=True)
            sz = os.path.getsize(dest)
            if sz < 1000:
                with open(dest, 'r', errors='replace') as chk:
                    if 'html' in chk.read(300).lower():
                        os.remove(dest)
                        print(f"\r  x {f['filename']} — was HTML" + " "*20); continue
            print(f"\r  ok {f['filename']} — {sz//1024} KB" + " "*20)
            downloaded += 1
        except requests.RequestException as e:
            print(f"  x {f['filename']} — {e}")
        time.sleep(1)
    print(f"\n[download] {downloaded}/{len(files)} files downloaded")
    return downloaded


# ═══════════════════════════════════════════════════════════
#  FILE PARSING
# ═══════════════════════════════════════════════════════════

def sf(s):
    try: return float(s.strip()) if s and s.strip() else np.nan
    except: return np.nan

def si(s):
    try: return int(float(s.strip())) if s and s.strip() else 0
    except: return 0

def read_mfplmon3(filepath):
    text = None
    if filepath.endswith('.zip'):
        try:
            with zipfile.ZipFile(filepath, 'r') as zf:
                for name in zf.namelist():
                    if name.endswith('.txt') or 'mfplmon' in name.lower():
                        with zf.open(name) as f:
                            text = f.read().decode('utf-8', errors='replace')
                        break
                if text is None and zf.namelist():
                    with zf.open(zf.namelist()[0]) as f:
                        text = f.read().decode('utf-8', errors='replace')
        except zipfile.BadZipFile: return []
    else:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    if not text: return []

    records = []
    for line in text.split('\n'):
        flds = line.rstrip('\r').split('|')
        if len(flds) < 32: continue
        cusip = flds[0].strip()
        if not cusip or len(cusip) != 9 or not cusip[0].isdigit(): continue
        pt = flds[P['pool_type']].strip()
        def g(idx): return flds[idx].strip() if len(flds) > idx else ''
        rec = {
            'pool_cusip': cusip, 'pool_number': g(P['pool_number']),
            'pool_type': pt, 'pool_type_name': POOL_TYPE_NAMES.get(pt, pt),
            'security_rate': sf(g(P['security_rate'])),
            'issue_date': g(P['issue_date']), 'pool_maturity_date': g(P['maturity_date']),
            'orig_agg_amount': sf(g(P['orig_agg_amount'])),
            'issuer_number': g(P['issuer_number']), 'issuer_name': g(P['issuer_name']),
            'pool_upb': sf(g(P['pool_upb'])),
            'security_rpb': sf(g(P['security_rpb'])),
            'rpb_factor': sf(g(P['rpb_factor'])),
        }
        if len(flds) > L['case_number']:
            case = g(L['case_number'])
            rec.update({
                'case_number': case, 'loan_id': cusip + '_' + case,
                'agency_type': g(L['agency_type']), 'loan_type': g(L['loan_type']),
                'loan_term': si(g(L['loan_term'])),
                'first_pay_date': g(L['first_pay_date']),
                'loan_maturity_date': g(L['loan_maturity_date']),
                'loan_rate': sf(g(L['loan_rate'])),
                'modified_ind': g(L['modified_ind']),
                'non_level_ind': g(L['non_level_ind']),
                'mature_loan_flag': g(L['mature_loan_flag']),
                'origination_date': g(L['origination_date']),
                'lockout_term_yrs': si(g(L['lockout_term'])),
                'lockout_end_date': g(L['lockout_end_date']),
                'prepay_premium_period_yrs': si(g(L['prepay_premium_period'])),
                'prepay_end_date': g(L['prepay_end_date']),
                'prepay_penalty_flag': g(L['prepay_penalty_flag']),
                'orig_prin_bal': sf(g(L['orig_prin_bal'])),
                'upb_at_issuance': sf(g(L['upb_at_issuance'])),
                'upb': sf(g(L['upb'])),
                'months_dq': si(g(L['months_dq'])),
                'liquidation_flag': g(L['liquidation_flag']),
                'removal_reason': g(L['removal_reason']),
                'property_name': g(L['property_name']),
                'property_city': g(L['property_city']),
                'property_state': g(L['property_state']),
                'msa': g(L['msa']),
                'num_units': si(g(L['num_units'])),
                'pi_amount': sf(g(L['pi_amount'])),
                'prepay_desc': g(L['prepay_desc']),
                'fha_program_code': g(L['fha_program_code']),
                'insurance_type': g(L['insurance_type']),
                'as_of_date': g(L['as_of_date']),
                'green_status': g(L['green_status']),
                'affordable_status': g(L['affordable_status']),
            })
        else:
            rec.update({'loan_id': cusip + '_POOL', 'case_number': '',
                        'loan_rate': np.nan, 'upb': np.nan})
        records.append(rec)
    return records



# ═══════════════════════════════════════════════════════════
#  ANALYTICS: PREPAYMENT FLAGS, LOCKOUT, REFI INCENTIVE
# ═══════════════════════════════════════════════════════════

PLC_SPREAD_BPS = 70  # Assumed spread over 10yr Treasury for PLC rate

def load_treasury_rates():
    """Load 10yrTsyRates.csv, return monthly average rates keyed by YYYYMM."""
    tsy_path = os.path.join(SCRIPT_DIR, '10yrTsyRates.csv')
    if not os.path.exists(tsy_path):
        print("[analytics] WARNING: 10yrTsyRates.csv not found, skipping refi incentive")
        return {}
    tsy = pd.read_csv(tsy_path)
    tsy.columns = [c.strip() for c in tsy.columns]
    tsy['DGS10'] = pd.to_numeric(tsy['DGS10'], errors='coerce')
    tsy = tsy.dropna(subset=['DGS10'])
    tsy['observation_date'] = pd.to_datetime(tsy['observation_date'])
    tsy['yyyymm'] = tsy['observation_date'].dt.strftime('%Y%m')
    monthly = tsy.groupby('yyyymm')['DGS10'].mean()
    return monthly.to_dict()


def parse_date_yyyymmdd(s):
    """Parse YYYYMMDD or YYYYMM string to a comparable YYYYMM string."""
    s = (s or '').strip()
    if len(s) >= 6:
        return s[:6]
    return ''


def build_analytics(monthly_data):
    """Enrich raw records with prepayment flags, lockout status, and refi incentive.

    - Excludes lockout-period loans from prepayment calculations
    - Computes refi incentive per the formula:
      Refi Incentive (bps) = Net Coupon (bps) - (PLC_bps + (1 + prepay_penalty_points) * 12.5)
      where PLC = 10yr Treasury + 70bps spread
    """
    periods = sorted(monthly_data.keys())
    print(f"\n[analytics] Enriching {len(periods)} periods with prepayment & refi incentive data...")

    # Load treasury rates for PLC calculation
    tsy_rates = load_treasury_rates()

    # Build lookup: period -> {loan_id -> record}
    pmap = {p: {r['loan_id']: r for r in recs} for p, recs in monthly_data.items()}

    all_records = []
    for i, period in enumerate(periods):
        nxt = periods[i + 1] if i + 1 < len(periods) else None
        nl = pmap.get(nxt, {}) if nxt else {}

        # PLC rate for this period: 10yr Treasury + spread
        tsy_rate = tsy_rates.get(period, np.nan)
        plc_rate = tsy_rate + PLC_SPREAD_BPS / 100.0 if pd.notna(tsy_rate) else np.nan

        for r in monthly_data[period]:
            r['period'] = period

            # Lockout status: compare period to lockout_end_date
            lockout_end_yyyymm = parse_date_yyyymmdd(r.get('lockout_end_date', ''))
            in_lockout = 1 if lockout_end_yyyymm and period < lockout_end_yyyymm else 0
            r['in_lockout'] = in_lockout

            # Prepay penalty status
            prepay_end_yyyymm = parse_date_yyyymmdd(r.get('prepay_end_date', ''))
            in_penalty = 1 if prepay_end_yyyymm and period < prepay_end_yyyymm else 0
            r['in_prepay_penalty'] = in_penalty
            r['past_all_restrictions'] = 1 if not in_lockout and not in_penalty else 0

            # Current prepay penalty points (years remaining * 1 point/year, floored at 0)
            if prepay_end_yyyymm and period < prepay_end_yyyymm:
                try:
                    pe_yr = int(prepay_end_yyyymm[:4])
                    pe_mo = int(prepay_end_yyyymm[4:6])
                    p_yr = int(period[:4])
                    p_mo = int(period[4:6])
                    months_remaining = (pe_yr - p_yr) * 12 + (pe_mo - p_mo)
                    prepay_penalty_points = max(months_remaining / 12.0, 0)
                except (ValueError, IndexError):
                    prepay_penalty_points = 0.0
            else:
                prepay_penalty_points = 0.0
            r['prepay_penalty_points'] = round(prepay_penalty_points, 2)

            # Refi incentive calculation
            # Refi Incentive (bps) = Net Coupon (bps) - (PLC_bps + (1 + penalty_points) * 12.5)
            loan_rate = r.get('loan_rate', np.nan)
            if pd.notna(loan_rate) and pd.notna(plc_rate):
                net_coupon_bps = loan_rate * 100  # e.g., 5.0 -> 500 bps
                plc_bps = plc_rate * 100  # e.g., 4.7 -> 470 bps
                refi_incentive = net_coupon_bps - (plc_bps + (1 + prepay_penalty_points) * 12.5)
                r['plc_rate'] = round(plc_rate, 4)
                r['refi_incentive_bps'] = round(refi_incentive, 2)
            else:
                r['plc_rate'] = np.nan
                r['refi_incentive_bps'] = np.nan

            # Prepayment flags (only for loans NOT in lockout)
            lid = r.get('loan_id', '')
            rm = r.get('removal_reason', '').strip()
            mdq = r.get('months_dq', 0) or 0

            if in_lockout:
                # Loans in lockout are excluded from prepayment calculations
                r['prepaid_voluntary'] = 0
                r['prepaid_involuntary'] = 0
                r['prepay_eligible'] = 0
            else:
                r['prepay_eligible'] = 1
                vol = rm == '1'
                invol = rm in ('2', '3', '4', '6')
                # Disappearance tracking (loan in T but not in T+1)
                disappeared = nxt is not None and lid not in nl
                if disappeared and not vol and not invol:
                    vol = mdq == 0
                    invol = mdq > 0
                r['prepaid_voluntary'] = 1 if vol else 0
                r['prepaid_involuntary'] = 1 if invol else 0

            all_records.append(r)

    df = pd.DataFrame(all_records)

    # Summary stats
    eligible = df[df['prepay_eligible'] == 1]
    vol = eligible['prepaid_voluntary'].sum()
    invol = eligible['prepaid_involuntary'].sum()
    locked = (df['in_lockout'] == 1).sum()
    print(f"  Total records: {len(df)}")
    print(f"  Prepay-eligible (not in lockout): {len(eligible)}")
    print(f"  In lockout (excluded): {locked}")
    print(f"  Voluntary prepays: {vol:.0f}")
    print(f"  Involuntary removals: {invol:.0f}")
    if df['refi_incentive_bps'].notna().any():
        print(f"  Refi incentive range: {df['refi_incentive_bps'].min():.0f} to {df['refi_incentive_bps'].max():.0f} bps")

    return df


# ═══════════════════════════════════════════════════════════
#  OUTPUT
# ═══════════════════════════════════════════════════════════

def write_csv(df, monthly_data):
    """Write enriched DataFrame to CSV."""
    df.to_csv(OUTPUT_CSV, index=False)

    print(f"\n{'='*60}")
    print(f"  ok {OUTPUT_CSV}")
    print(f"     {len(df)} records, {df['loan_id'].nunique()} unique loans")
    print(f"     Periods: {', '.join(sorted(monthly_data.keys()))}")
    print(f"     Columns: {len(df.columns)}")
    print(f"{'='*60}")
    return df


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="GNMA MF Downloader & Raw Data Exporter")
    ap.add_argument("--email", help="GNMA Disclosure email")
    ap.add_argument("--answer", help="Security question answer")
    ap.add_argument("--months", type=int, default=12, help="Months to download (default: 12)")
    ap.add_argument("--skip-download", action="store_true", help="Skip download, parse existing files")
    ap.add_argument("--data-dir", help="Override data directory")
    args = ap.parse_args()

    global DATA_DIR, OUTPUT_CSV
    if args.data_dir:
        DATA_DIR = args.data_dir
        OUTPUT_CSV = os.path.join(args.data_dir, "gnma_mf_raw_data.csv")

    print("""
+==============================================================+
|  GNMA Multifamily Data Downloader & Raw Data Exporter        |
|  Auth: Firefox (Playwright) -> cookie transfer -> downloads  |
+==============================================================+
""")

    if not args.skip_download:
        discover_nix_libs()
        email = args.email or input("GNMA Disclosure email: ").strip()
        answer = args.answer or input("Security question answer: ").strip()
        if not ensure_playwright():
            print("Playwright installation failed.")
            return
        session = authenticate_gnma(email, answer)
        if session:
            download_files(session, args.months)
        else:
            print(f"\n  Manual fallback: download mfplmon3 files to {DATA_DIR}")
            print(f"  Then: python3 main.py --skip-download")

    # Clear LD_LIBRARY_PATH (not strictly needed for CSV, but clean)
    os.environ.pop('LD_LIBRARY_PATH', None)

    os.makedirs(DATA_DIR, exist_ok=True)
    allf = sorted(glob.glob(os.path.join(DATA_DIR,"*.zip")) +
                  glob.glob(os.path.join(DATA_DIR,"*.txt")))
    if not allf:
        print(f"No files in {DATA_DIR}"); return

    print(f"\n[parse] Parsing {len(allf)} files...")
    md = {}
    for fp in allf:
        m = re.search(r'(\d{6})', os.path.basename(fp))
        if not m: continue
        per = m.group(1)
        recs = read_mfplmon3(fp)
        if recs:
            md[per] = recs
            pt = defaultdict(int)
            for r in recs: pt[r['pool_type']] += 1
            print(f"  {os.path.basename(fp)} -> {per}: {len(recs)} records {dict(pt)}")

    if not md:
        print("\nNo valid records parsed."); return

    df = build_analytics(md)
    write_csv(df, md)


if __name__ == "__main__":
    main()
