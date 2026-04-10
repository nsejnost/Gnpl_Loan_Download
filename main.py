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
# V3.3 pool-level field indices (Jun 2023 - Present). Pool section has
# 31 fields (indices 0-30). V3.1/V3.2 (May 2022 - May 2023) have only
# 29 pool fields (0-28) — no P30/P31. All P1-P29 positions are identical
# across V3.1, V3.2, and V3.3.
P = {
    'cusip': 0, 'pool_number': 1, 'pool_indicator': 2, 'pool_type': 3,
    'security_rate': 4, 'issue_date': 5, 'maturity_date': 6, 'orig_agg_amount': 7,
    'issuer_number': 8, 'issuer_name': 9,
    'pool_upb': 16, 'num_loans': 17,
    'num_30dq': 18, 'upb_30dq': 19, 'pct_30dq': 20,
    'num_60dq': 21, 'upb_60dq': 22, 'pct_60dq': 23,
    'num_90dq': 24, 'upb_90dq': 25, 'pct_90dq': 26,
    'security_rpb': 27, 'rpb_factor': 28,
    # V3.3-only fields below — only populated for CL/CS (construction) pool
    # types. Do NOT read these without first checking the detected layout
    # version; V3.1/V3.2 files do not have them at all.
    'proj_loan_sec_rate': 29,  # P30, V3.3-only, CS pool types only
    'est_mtg_amount': 30,      # P31, V3.3-only, CL/CS pool types only
}

# V3.3 loan-level field indices (pool fields 0-30 precede).
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

# V3.1 / V3.2 loan-level field indices (May 2022 - May 2023). Identical
# loan fields L1-L44, but pool section has only 29 fields (no P30/P31),
# so every loan index is shifted down by 2. Derived from L so the two
# stay in lockstep if V3.3's dict is ever edited.
L_V31V32 = {k: v - 2 for k, v in L.items()}

GNMA_EMAIL_SELECTOR = 'input[name*="tbemailaddress" i]'
GNMA_ANSWER_ID = '#ctl00_ctl45_g_174dfd7c_a193_4313_a2ed_0005c00273fc_ctl00_tbAnswer'
GNMA_ANSWER_SUBMIT_ID = '#ctl00_ctl45_g_174dfd7c_a193_4313_a2ed_0005c00273fc_ctl00_btnAnswerSecret'

TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT_CSV = os.path.join(SCRIPT_DIR, f"gnma_mf_raw_data_{TIMESTAMP}.csv.gz")


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
        # Recent files live under data_bulk/; older files are moved to
        # data_history_cons/. The downloader will try data_bulk first and
        # fall back to data_history_cons (URL-encoded backslash) on failure.
        primary_url = f"{BULK_URL}/protectedfiledownload.aspx?dlfile=data_bulk/{fn}"
        fallback_url = f"{BULK_URL}/protectedfiledownload.aspx?dlfile=data_history_cons%5C{fn}"
        files.append({
            "period": period,
            "filename": fn,
            "url": primary_url,
            "fallback_url": fallback_url,
        })
    return files[:months]


def _validate_mfplmon_zip(filepath):
    """Verify a downloaded file is a real mfplmon3 zip with content.

    GNMA's SharePoint server returns small (~4 KB) HTML error pages instead
    of HTTP errors when a requested mfplmon3 file does not exist (e.g. for
    months before the V3.3 format was introduced in late 2023). Those pages
    bypass any size-based check, so we instead try to open the file as a zip
    and verify it contains a recognizable mfplmon entry.

    Returns (is_valid, reason). is_valid is True if the file is a real
    mfplmon3 zip; reason is a short explanation when invalid.
    """
    try:
        with zipfile.ZipFile(filepath, 'r') as zf:
            names = zf.namelist()
            if not names:
                return False, "empty zip"
            # Look for an mfplmon-like text entry
            has_mfplmon = any(
                ('mfplmon' in n.lower()) or n.lower().endswith('.txt')
                for n in names
            )
            if not has_mfplmon:
                return False, f"no mfplmon entry (contents: {names[:3]})"
            # Sanity check: the inner file should have non-trivial size
            for n in names:
                if 'mfplmon' in n.lower() or n.lower().endswith('.txt'):
                    info = zf.getinfo(n)
                    if info.file_size < 10000:
                        return False, f"inner file too small ({info.file_size} bytes)"
                    return True, "ok"
            return False, "no readable mfplmon entry"
    except zipfile.BadZipFile:
        # Not a valid zip — most likely an HTML error page
        try:
            with open(filepath, 'r', errors='replace') as chk:
                head = chk.read(300).lower()
            if '<html' in head or '<!doctype' in head:
                return False, "HTML error page (file not on server)"
        except Exception:
            pass
        return False, "not a valid zip"
    except Exception as e:
        return False, f"validation error: {e}"


def _ensure_data_dir(path):
    """Ensure `path` exists and is a directory.

    - If it does not exist, create it.
    - If it exists and is a directory, do nothing.
    - If it exists but is NOT a directory (e.g. a zero-byte file
      accidentally created via a file-browser UI), print a clear
      actionable error and SystemExit(1).

    This wraps the footgun in os.makedirs(exist_ok=True), which still
    raises FileExistsError when the path exists but is not a directory,
    producing a cryptic traceback that doesn't explain the fix.
    """
    if os.path.isdir(path):
        return
    if os.path.exists(path):
        # Path exists but isn't a directory — most likely a stray file
        print()
        print(f"[setup] ERROR: '{path}' exists but is not a directory.")
        print(f"[setup]        Expected a directory to hold downloaded")
        print(f"[setup]        mfplmon3 zip files.")
        print(f"[setup]")
        print(f"[setup]        This usually happens when a file with that")
        print(f"[setup]        exact name was created by mistake (on Replit,")
        print(f"[setup]        the 'New File' button is right next to")
        print(f"[setup]        'New Folder').")
        print(f"[setup]")
        print(f"[setup]        To fix, remove or rename the offending path:")
        print(f"[setup]          rm '{path}'")
        print(f"[setup]        then re-run. The script will create the")
        print(f"[setup]        directory itself.")
        print()
        raise SystemExit(1)
    os.makedirs(path)


def _attempt_download(session, url, dest, label):
    """Try a single download URL. Returns (success, reason).

    On success, the file is saved to `dest` and validated as a real mfplmon zip.
    On failure, any partial file at `dest` is removed.
    """
    try:
        r = session.get(url, timeout=120, stream=True, allow_redirects=True)
        if "profile" in r.url.lower():
            return False, "session expired"
        ct = r.headers.get("Content-Type", "")
        if "text/html" in ct and "profile" in (r.text[:500] if hasattr(r, 'text') else "").lower():
            return False, "login redirect"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        total = int(r.headers.get("Content-Length", 0))
        dl = 0
        with open(dest, "wb") as out:
            for chunk in r.iter_content(65536):
                out.write(chunk); dl += len(chunk)
                if total:
                    print(f"\r  > {label} {dl//1024}/{total//1024} KB ({dl*100//total}%)",
                          end="", flush=True)
        ok, reason = _validate_mfplmon_zip(dest)
        if not ok:
            os.remove(dest)
            return False, reason
        return True, "ok"
    except requests.RequestException as e:
        if os.path.exists(dest):
            try: os.remove(dest)
            except OSError: pass
        return False, f"{e}"


def download_files(session, months=6):
    _ensure_data_dir(DATA_DIR)
    files = get_file_list(months)
    print(f"\n[download] Downloading {len(files)} files to {DATA_DIR}\n")
    downloaded = 0
    missing_periods = []
    for f in files:
        dest = os.path.join(DATA_DIR, f["filename"])
        if os.path.exists(dest) and os.path.getsize(dest) > 1000:
            # Re-validate cached files so old bad downloads are caught
            ok, reason = _validate_mfplmon_zip(dest)
            if ok:
                print(f"  ok {f['filename']} (cached, {os.path.getsize(dest)//1024} KB)")
                downloaded += 1; continue
            else:
                print(f"  ! {f['filename']} cached but invalid ({reason}); re-downloading")
                os.remove(dest)

        # Try data_bulk first (recent months)
        ok, reason = _attempt_download(session, f["url"], dest, f["filename"])
        if not ok:
            # Fall back to data_history_cons (older months)
            print(f"\r  > {f['filename']} not in data_bulk ({reason}); trying data_history_cons..." + " "*20)
            ok, reason = _attempt_download(session, f["fallback_url"], dest, f["filename"])

        if ok:
            sz = os.path.getsize(dest)
            print(f"\r  ok {f['filename']} — {sz//1024} KB" + " "*40)
            downloaded += 1
        else:
            print(f"\r  x {f['filename']} — {reason}" + " "*40)
            missing_periods.append(f["period"])
        time.sleep(1)

    print(f"\n[download] {downloaded}/{len(files)} files downloaded")
    if missing_periods:
        print(f"[download] {len(missing_periods)} period(s) unavailable: "
              f"{', '.join(sorted(missing_periods))}")
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

# Expected total field count for each supported layout version.
# V3.3 (Jun 2023+): 31 pool + 44 loan = 75 fields
# V3.1 / V3.2 (May 2022 - May 2023): 29 pool + 44 loan = 73 fields
V33_FIELD_COUNT = 75
V31V32_FIELD_COUNT = 73

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

    # Detect layout version by counting fields in the first loan-level
    # record. Pool-only records have fewer fields, so we specifically
    # look for records with loan data attached.
    detected_field_count = None
    layout = None   # 'V33', 'V31V32', or None (unknown)
    LX = L          # loan-level dict to use; defaults to V3.3

    records = []
    for line in text.split('\n'):
        flds = line.rstrip('\r').split('|')
        if len(flds) < 32: continue
        cusip = flds[0].strip()
        if not cusip or len(cusip) != 9 or not cusip[0].isdigit(): continue

        # First loan-level record: determine layout version
        if detected_field_count is None and len(flds) > 32:
            detected_field_count = len(flds)
            fname = os.path.basename(filepath)
            if detected_field_count == V33_FIELD_COUNT:
                layout = 'V33'
                LX = L
            elif detected_field_count == V31V32_FIELD_COUNT:
                layout = 'V31V32'
                LX = L_V31V32
                print(f"  i {fname}: V3.1/V3.2 layout detected "
                      f"({detected_field_count} fields)")
            else:
                layout = None
                print(f"  ! {fname}: unrecognized layout "
                      f"({detected_field_count} fields, expected "
                      f"{V33_FIELD_COUNT} or {V31V32_FIELD_COUNT}); "
                      f"loan-level parsing will be skipped")

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
            # V3.3-only pool fields. Blank/NaN for V3.1/V3.2 files since
            # those layouts don't have P30/P31 at all.
            'proj_loan_sec_rate': sf(g(P['proj_loan_sec_rate'])) if layout == 'V33' else np.nan,
            'est_mtg_amount': sf(g(P['est_mtg_amount'])) if layout == 'V33' else np.nan,
        }
        if layout is not None and len(flds) > LX['case_number']:
            case = g(LX['case_number'])
            # V3.1 used "NAF" for Affordable Status; V3.2 renamed this to
            # "MKT". Normalize at parse time so downstream code only has
            # to handle the modern value.
            aff_status_raw = g(LX['affordable_status'])
            aff_status = 'MKT' if aff_status_raw == 'NAF' else aff_status_raw
            rec.update({
                'case_number': case, 'loan_id': cusip + '_' + case,
                'agency_type': g(LX['agency_type']), 'loan_type': g(LX['loan_type']),
                'loan_term': si(g(LX['loan_term'])),
                'first_pay_date': g(LX['first_pay_date']),
                'loan_maturity_date': g(LX['loan_maturity_date']),
                'loan_rate': sf(g(LX['loan_rate'])),
                'modified_ind': g(LX['modified_ind']),
                'non_level_ind': g(LX['non_level_ind']),
                'mature_loan_flag': g(LX['mature_loan_flag']),
                'origination_date': g(LX['origination_date']),
                'lockout_term_yrs': si(g(LX['lockout_term'])),
                'lockout_end_date': g(LX['lockout_end_date']),
                'prepay_premium_period_yrs': si(g(LX['prepay_premium_period'])),
                'prepay_end_date': g(LX['prepay_end_date']),
                'prepay_penalty_flag': g(LX['prepay_penalty_flag']),
                'orig_prin_bal': sf(g(LX['orig_prin_bal'])),
                'upb_at_issuance': sf(g(LX['upb_at_issuance'])),
                'upb': sf(g(LX['upb'])),
                'months_dq': si(g(LX['months_dq'])),
                'liquidation_flag': g(LX['liquidation_flag']),
                'removal_reason': g(LX['removal_reason']),
                'property_name': g(LX['property_name']),
                'property_city': g(LX['property_city']),
                'property_state': g(LX['property_state']),
                'msa': g(LX['msa']),
                'num_units': si(g(LX['num_units'])),
                'pi_amount': sf(g(LX['pi_amount'])),
                'prepay_desc': g(LX['prepay_desc']),
                'fha_program_code': g(LX['fha_program_code']),
                'insurance_type': g(LX['insurance_type']),
                'as_of_date': g(LX['as_of_date']),
                'green_status': g(LX['green_status']),
                'affordable_status': aff_status,
            })
        else:
            rec.update({'loan_id': cusip + '_POOL', 'case_number': '',
                        'loan_rate': np.nan, 'upb': np.nan})
        records.append(rec)
    return records



# ═══════════════════════════════════════════════════════════
#  ANALYTICS: PREPAYMENT FLAGS, LOCKOUT, REFI INCENTIVE
# ═══════════════════════════════════════════════════════════

def load_plc_rates():
    """Load GnmaPlcRatesHistorical.csv, return PLC rates in bps keyed by YYYYMM."""
    plc_path = os.path.join(SCRIPT_DIR, 'GnmaPlcRatesHistorical.csv')
    if not os.path.exists(plc_path):
        print("[analytics] WARNING: GnmaPlcRatesHistorical.csv not found, skipping refi incentive")
        return {}
    plc = pd.read_csv(plc_path)
    plc.columns = [c.strip() for c in plc.columns]
    plc['PLC_Rate_BPS'] = pd.to_numeric(plc['PLC_Rate_BPS'], errors='coerce')
    plc = plc.dropna(subset=['PLC_Rate_BPS'])
    plc['Date'] = pd.to_datetime(plc['Date'])
    plc['yyyymm'] = plc['Date'].dt.strftime('%Y%m')
    return dict(zip(plc['yyyymm'], plc['PLC_Rate_BPS']))


def parse_date_yyyymmdd(s):
    """Parse YYYYMMDD or YYYYMM string to a comparable YYYYMM string."""
    s = (s or '').strip()
    if len(s) >= 6:
        return s[:6]
    return ''


def parse_penalty_schedule(prepay_desc, max_entries=None):
    """Parse the declining penalty schedule from prepay_desc.

    Handles various formats found in GNMA data:
      '10,9,8,7,6,5,4,3,2,1,0'
      '10, 10, 8, 7, 6, 5, 4, 3, 2, 1, 0'
      '10/9/8/7/6/5/4/3/2/1% THRU 9/1/2034'
      '0 LOCK, THEN 10,9,8,7,6,5,4,3,2,1,0'
      '0: 10,9,8,7,6,5,4,3,2,1,0'

    Returns a list of penalty percentages by year, e.g. [10, 9, 8, ...].
    max_entries truncates stray numbers from dates embedded in the string.
    """
    s = (prepay_desc or '').strip()
    if not s:
        return []
    # Strip text after keywords that introduce dates (e.g., "THRU 9/1/2034")
    for kw in ['THRU', 'THROUGH', 'UNTIL', 'ENDING']:
        idx = s.upper().find(kw)
        if idx >= 0:
            s = s[:idx]
    # Extract all numbers (int or float) from the cleaned string
    nums = re.findall(r'\d+(?:\.\d+)?', s)
    if not nums:
        return []
    schedule = [float(n) for n in nums]
    # Filter out 4-digit years and large non-penalty numbers
    schedule = [n for n in schedule if n <= 100]
    # Truncate to expected length if provided (period_yrs + 1 for the trailing 0)
    if max_entries and len(schedule) > max_entries:
        schedule = schedule[:max_entries]
    return schedule


def get_current_penalty_points(prepay_desc, prepay_end_yyyymm, prepay_premium_period_yrs, period):
    """Determine the current prepayment penalty percentage for a loan.

    Uses the declining schedule from prepay_desc to look up the penalty
    based on which year of the penalty period the loan is currently in.
    Falls back to years-remaining if the schedule can't be parsed.
    """
    if not prepay_end_yyyymm or period >= prepay_end_yyyymm:
        return 0.0

    try:
        pe_yr = int(prepay_end_yyyymm[:4])
        pe_mo = int(prepay_end_yyyymm[4:6])
        p_yr = int(period[:4])
        p_mo = int(period[4:6])
        months_remaining = (pe_yr - p_yr) * 12 + (pe_mo - p_mo)
    except (ValueError, IndexError):
        return 0.0

    total_period_yrs = prepay_premium_period_yrs or 0
    max_entries = total_period_yrs + 1 if total_period_yrs > 0 else None
    schedule = parse_penalty_schedule(prepay_desc, max_entries)

    if schedule and total_period_yrs > 0:
        # Which year of the penalty period are we in? (0-indexed)
        total_months = total_period_yrs * 12
        months_elapsed = max(0, total_months - months_remaining)
        year_index = min(int(months_elapsed / 12), len(schedule) - 1)
        return schedule[year_index]
    else:
        # Fallback: use years remaining (capped at total period)
        years_remaining = months_remaining / 12.0
        if total_period_yrs > 0:
            return min(years_remaining, total_period_yrs)
        return years_remaining
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
      where PLC is from GnmaPlcRatesHistorical.csv (actual GNMA published rates)
    """
    periods = sorted(monthly_data.keys())
    print(f"\n[analytics] Enriching {len(periods)} periods with prepayment & refi incentive data...")

    # Load PLC rates (already in bps)
    plc_rates = load_plc_rates()

    # Build lookup: period -> {loan_id -> record}
    pmap = {p: {r['loan_id']: r for r in recs} for p, recs in monthly_data.items()}

    all_records = []
    for i, period in enumerate(periods):
        nxt = periods[i + 1] if i + 1 < len(periods) else None
        nl = pmap.get(nxt, {}) if nxt else {}

        # PLC rate for this period (already in bps from CSV)
        plc_bps = plc_rates.get(period, np.nan)

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

            # Current prepay penalty points from declining schedule in prepay_desc
            prepay_penalty_points = get_current_penalty_points(
                r.get('prepay_desc', ''),
                prepay_end_yyyymm,
                r.get('prepay_premium_period_yrs', 0),
                period,
            )
            # Cap at 10 — max possible in any standard GNMA MF declining schedule.
            # Handles ~14 loans (0.08%) with garbled prepay_desc formats.
            prepay_penalty_points = min(prepay_penalty_points, 10.0)
            r['prepay_penalty_points'] = round(prepay_penalty_points, 2)

            # Refi incentive calculation
            # Refi Incentive (bps) = Net Coupon (bps) - (PLC_bps + (1 + penalty_points) * 12.5)
            loan_rate = r.get('loan_rate', np.nan)
            if pd.notna(loan_rate) and pd.notna(plc_bps):
                net_coupon_bps = loan_rate * 100  # e.g., 5.0 -> 500 bps
                refi_incentive = net_coupon_bps - (plc_bps + (1 + prepay_penalty_points) * 12.5)
                r['plc_rate_bps'] = plc_bps
                r['refi_incentive_bps'] = round(refi_incentive, 2)
            else:
                r['plc_rate_bps'] = np.nan
                r['refi_incentive_bps'] = np.nan

            # Prepayment flags (only for loans NOT in lockout)
            lid = r.get('loan_id', '')
            rm = r.get('removal_reason', '').strip()
            mdq = r.get('months_dq', 0) or 0

            if in_lockout or r.get('pool_type', '') in ('CL', 'CS'):
                # Loans in lockout or construction (CL/CS) are excluded from
                # prepayment calculations. CL/CS loans convert to PN on
                # construction completion — their disappearance is not a prepay.
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
        OUTPUT_CSV = os.path.join(args.data_dir, f"gnma_mf_raw_data_{TIMESTAMP}.csv.gz")

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

    _ensure_data_dir(DATA_DIR)
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
