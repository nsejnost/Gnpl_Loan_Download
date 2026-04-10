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

# ─── Historical layouts (V3.0, V2.0, V1.0) ────────────────
# Every historical GNMA multifamily layout is derivable from the V3.3
# dicts above because each later version only *appended* fields at the
# tail of its section (pool or loan). See Historical_Layouts_Guide_Feb2024.pdf
# for the definitive version history.
#
# V3.0 (Jan 2022 - Apr 2022, mfplmon3): V3.1/V3.2 minus the
# Affordable Status loan field (which was added in V3.1 as a new
# trailing loan field). Pool section is identical to V3.1/V3.2.
L_V30 = {k: v for k, v in L_V31V32.items() if k != 'affordable_status'}

# V2.0 (Jul 2021 - Dec 2021, mfplmon2): V3.0 minus Green Status
# (added in V3.0 as a new trailing loan field). Pool section is
# identical to V3.0 (29 fields).
L_V20 = {k: v for k, v in L_V30.items() if k != 'green_status'}

# V1.0 (Dec 2018 - Jun 2021, mfplmon): V2.0 but the pool section also
# loses Security RPB (P28) and RPB Factor (P29), which were added in
# V2.0. Since those were the last two pool fields, every loan index
# shifts down by a further 2.
L_V10 = {k: v - 2 for k, v in L_V20.items()}

# Pool-level dict for V1.0 only. V2.0/V3.0/V3.1/V3.2 pool sections are
# all identical to P's first 29 fields (indices 0-28), so P itself
# serves them as long as proj_loan_sec_rate / est_mtg_amount reads are
# gated on layout == 'V33' (already done at the read site below).
P_V10 = {k: v for k, v in P.items()
         if k not in ('security_rpb', 'rpb_factor',
                      'proj_loan_sec_rate', 'est_mtg_amount')}

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


def _get_candidates_for_period(period):
    """Return an ordered list of (filename, url) candidates to try for `period`.

    GNMA publishes multifamily pool/loan data under three historical
    filename prefixes and two extensions depending on the era:

        mfplmon3_{YYYYMM}.zip   Jan 2022 - present   (V3.0 / V3.1 / V3.2 / V3.3)
        mfplmon2_{YYYYMM}.zip   Jul 2021 - Dec 2021  (V2.0)
        mfplmon_{YYYYMM}.txt    Dec 2018 - Jun 2021  (V1.0, plain text)

    Recent files live under `data_bulk/` (forward slash); everything
    older is moved to `data_history_cons` (URL-encoded backslash
    `%5C`). Older mfplmon2/mfplmon files may also appear as `.zip` or
    `.txt` on the server, so we generate a candidate per plausible
    extension and let the downloader try them in priority order.

    The era cutoffs come from `Historical_Layouts_Guide_Feb2024.pdf`
    (in the repo root). Do not reorder the candidates without updating
    this comment: the order matters for the slow, HTTP-hitting fallback
    loop in `download_files`.
    """
    yyyymm = int(period)
    candidates = []

    def add(prefix, directory, ext):
        fn = f"{prefix}_{period}{ext}"
        if directory == "data_bulk/":
            dlfile = f"{directory}{fn}"
        else:
            dlfile = f"{directory}%5C{fn}"
        url = f"{BULK_URL}/protectedfiledownload.aspx?dlfile={dlfile}"
        candidates.append((fn, url))

    if yyyymm >= 202201:
        # mfplmon3 era (V3.0 / V3.1 / V3.2 / V3.3). data_bulk only
        # holds the most recent months; everything older has been
        # moved into data_history_cons.
        add("mfplmon3", "data_bulk/",        ".zip")
        add("mfplmon3", "data_history_cons", ".zip")
        add("mfplmon3", "data_history_cons", ".txt")
    elif yyyymm >= 202107:
        # mfplmon2 era (V2.0). Jul 2021 - Dec 2021.
        add("mfplmon2", "data_history_cons", ".zip")
        add("mfplmon2", "data_history_cons", ".txt")
    else:
        # mfplmon era (V1.0). Dec 2018 - Jun 2021. The V1.0 disclosure
        # history states files are published as plain text; a .zip
        # fallback is kept in case some months were re-uploaded.
        add("mfplmon",  "data_history_cons", ".txt")
        add("mfplmon",  "data_history_cons", ".zip")

    return candidates


def get_file_list(months=6):
    """Return one {period, candidates} entry per calendar month, ending
    with the most recent *complete* month (the previous calendar month).

    Uses explicit year/month arithmetic rather than `timedelta(days=30)`
    so that a 120-month window yields exactly 120 distinct YYYYMM
    periods. The old 30-day stepping drifted ~0.44 days per iteration
    and, over long windows, would silently collide on some periods
    while skipping others.
    """
    now = datetime.now()
    y, m = now.year, now.month
    files = []
    seen = set()
    for _ in range(months):
        # Step back one calendar month. Skip the current month since
        # GNMA has not yet published data for it.
        m -= 1
        if m == 0:
            y -= 1
            m = 12
        period = f"{y:04d}{m:02d}"
        if period in seen:
            # Defensive: with calendar stepping, collisions are
            # structurally impossible, but keep the guard so a future
            # refactor can't silently reintroduce the drift bug.
            continue
        seen.add(period)
        files.append({
            "period": period,
            "candidates": _get_candidates_for_period(period),
        })
    return files


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


def _validate_mfplmon_txt(filepath):
    """Verify a .txt file is a real mfplmon plain-text file.

    GNMA's server returns ~4 KB HTML error pages for missing files;
    real V1.0/V2.0 text files are pipe-delimited with many fields per
    line.
    """
    try:
        size = os.path.getsize(filepath)
        with open(filepath, 'r', errors='replace') as f:
            head = f.read(2000)
        low = head.lower()
        if '<html' in low or '<!doctype' in low:
            return False, "HTML error page (file not on server)"
        if size < 1000:
            return False, f"file too small ({size} bytes)"
        first_line = head.split('\n', 1)[0]
        if first_line.count('|') < 20:
            return False, f"not pipe-delimited (first line: {first_line[:80]})"
        return True, "ok"
    except Exception as e:
        return False, f"validation error: {e}"


def _validate_mfplmon_file(filepath):
    """Dispatch to the right validator based on extension."""
    if filepath.endswith('.zip'):
        return _validate_mfplmon_zip(filepath)
    if filepath.endswith('.txt'):
        return _validate_mfplmon_txt(filepath)
    return False, f"unknown extension: {filepath}"


def _find_cached_file(data_dir, period):
    """Return the first existing file for `period` across any supported
    prefix/extension, or None if no cached file exists."""
    for prefix in ("mfplmon3", "mfplmon2", "mfplmon"):
        for ext in (".zip", ".txt"):
            p = os.path.join(data_dir, f"{prefix}_{period}{ext}")
            if os.path.exists(p) and os.path.getsize(p) > 1000:
                return p
    return None


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

    On success, the file is saved to `dest` and validated as a real
    mfplmon file (extension-aware: zip or txt).
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
        ok, reason = _validate_mfplmon_file(dest)
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
        period = f["period"]

        # Cache lookup: the file for this period could be present
        # under any of the 3 prefixes × 2 extensions, since a user may
        # have pulled files across multiple historical eras.
        cached = _find_cached_file(DATA_DIR, period)
        if cached:
            ok, reason = _validate_mfplmon_file(cached)
            if ok:
                print(f"  ok {os.path.basename(cached)} (cached, "
                      f"{os.path.getsize(cached)//1024} KB)")
                downloaded += 1
                continue
            print(f"  ! {os.path.basename(cached)} cached but invalid "
                  f"({reason}); re-downloading")
            os.remove(cached)

        # Try each candidate URL in priority order until one works.
        success = False
        last_reason = None
        for fn, url in f["candidates"]:
            dest = os.path.join(DATA_DIR, fn)
            ok, reason = _attempt_download(session, url, dest, fn)
            if ok:
                print(f"\r  ok {fn} — {os.path.getsize(dest)//1024} KB"
                      + " " * 40)
                downloaded += 1
                success = True
                break
            last_reason = reason

        if not success:
            print(f"\r  x {period} — no candidate worked "
                  f"(last: {last_reason})" + " " * 20)
            missing_periods.append(period)
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
# V3.3 (Jun 2023+):                31 pool + 44 loan = 75 fields
# V3.1 / V3.2 (May 2022-May 2023): 29 pool + 44 loan = 73 fields
# V3.0 (Jan 2022 - Apr 2022):      29 pool + 43 loan = 72 fields
# V2.0 (Jul 2021 - Dec 2021):      29 pool + 42 loan = 71 fields
# V1.0 (Dec 2018 - Jun 2021):      27 pool + 42 loan = 69 fields
V33_FIELD_COUNT    = 75
V31V32_FIELD_COUNT = 73
V30_FIELD_COUNT    = 72
V20_FIELD_COUNT    = 71
V10_FIELD_COUNT    = 69

# ─── File-level deduplication (one file per period) ────────────
# GNMA published the same period under different filename prefixes
# across eras. If a user accumulates files from multiple runs, the
# same period can end up represented by two different cached files
# (e.g. mfplmon_202106.zip AND mfplmon2_202106.zip, or a .txt and a
# .zip). The parse loop dedupes to one file per period using the
# era-correct prefix below as the tiebreaker. Out-of-era files are
# only used if no in-era file exists for that period.
ERA_PREFERENCE = [
    # (lowest_period, highest_period_inclusive, ordered prefixes by preference)
    (202201, 999912, ["mfplmon3"]),
    (202107, 202112, ["mfplmon2"]),
    (201812, 202106, ["mfplmon"]),
]

_PERIOD_FILENAME_RE = re.compile(
    r'^(mfplmon3|mfplmon2|mfplmon)_(\d{6})\.(zip|txt)$'
)


def _prefix_priority_for_period(period):
    """Return the ordered list of filename prefixes preferred for
    `period` (a YYYYMM string). Prefixes not in the list are treated
    as lowest-priority fallbacks — only chosen if no preferred file
    exists for that period."""
    p = int(period)
    for lo, hi, prefs in ERA_PREFERENCE:
        if lo <= p <= hi:
            return prefs
    return []


def _dedupe_files_by_period(filepaths):
    """Collapse a list of filepaths to one file per period.

    Preference order, per period:
      1. Era-correct prefix (mfplmon3 / mfplmon2 / mfplmon by YYYYMM)
      2. Any other known prefix
      3. .zip over .txt (smaller + faster to parse)

    Files whose basename doesn't match the known mfplmon*_YYYYMM.{zip,txt}
    pattern are ignored entirely, so stray files in the data dir (e.g.
    notes_202401.txt, README.md) can't be misinterpreted as period
    files.

    Returns a dict {period: filepath}.
    """
    # period -> (rank, filepath); lower rank = more preferred
    by_period = {}
    for fp in filepaths:
        m = _PERIOD_FILENAME_RE.match(os.path.basename(fp))
        if not m:
            continue
        prefix, period, ext = m.group(1), m.group(2), m.group(3)
        prefs = _prefix_priority_for_period(period)
        prefix_rank = prefs.index(prefix) if prefix in prefs else 99
        # Tie-break on extension: prefer .zip
        ext_rank = 0 if ext == 'zip' else 1
        rank = prefix_rank * 10 + ext_rank
        prev = by_period.get(period)
        if prev is None or rank < prev[0]:
            by_period[period] = (rank, fp)
    return {per: fp for per, (_, fp) in by_period.items()}


def read_mfplmon3(filepath):
    """Parse a GNMA multifamily pool/loan file.

    Handles every historical layout from V1.0 (Dec 2018) through V3.3
    (Jun 2023+). Layout is detected by counting fields in the first
    loan-level record; missing fields for older layouts (e.g.
    green_status on V2.0/V1.0, affordable_status on V3.0/V2.0/V1.0,
    security_rpb/rpb_factor on V1.0) are emitted as empty/NaN.

    Accepts both `.zip` (V2.0+) and `.txt` (V1.0 and some V2.0) inputs.
    """
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
    layout = None   # 'V33' | 'V31V32' | 'V30' | 'V20' | 'V10' | None
    LX = L          # loan-level dict to use; defaults to V3.3
    PX = P          # pool-level dict to use; defaults to V3.3

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
                layout, LX, PX = 'V33', L, P
            elif detected_field_count == V31V32_FIELD_COUNT:
                layout, LX, PX = 'V31V32', L_V31V32, P
                print(f"  i {fname}: V3.1/V3.2 layout detected "
                      f"({detected_field_count} fields)")
            elif detected_field_count == V30_FIELD_COUNT:
                layout, LX, PX = 'V30', L_V30, P
                print(f"  i {fname}: V3.0 layout detected "
                      f"({detected_field_count} fields)")
            elif detected_field_count == V20_FIELD_COUNT:
                layout, LX, PX = 'V20', L_V20, P
                print(f"  i {fname}: V2.0 layout detected "
                      f"({detected_field_count} fields)")
            elif detected_field_count == V10_FIELD_COUNT:
                layout, LX, PX = 'V10', L_V10, P_V10
                print(f"  i {fname}: V1.0 layout detected "
                      f"({detected_field_count} fields)")
            else:
                layout, LX, PX = None, L, P
                print(f"  ! {fname}: unrecognized layout "
                      f"({detected_field_count} fields, expected "
                      f"{V33_FIELD_COUNT}/{V31V32_FIELD_COUNT}/"
                      f"{V30_FIELD_COUNT}/{V20_FIELD_COUNT}/"
                      f"{V10_FIELD_COUNT}); loan-level parsing will "
                      f"be skipped")

        pt = flds[PX['pool_type']].strip()
        def g(idx): return flds[idx].strip() if len(flds) > idx else ''
        def gL(key, default=''):
            """Loan-level field lookup that tolerates missing keys.

            Older layouts drop trailing fields entirely (e.g. V2.0 has
            no green_status, V3.0/V2.0/V1.0 have no affordable_status),
            so a plain LX[key] would KeyError. Return `default` when
            the key is absent or the line is truncated.
            """
            idx = LX.get(key)
            if idx is None or len(flds) <= idx:
                return default
            return flds[idx].strip()

        rec = {
            'pool_cusip': cusip, 'pool_number': g(PX['pool_number']),
            'pool_type': pt, 'pool_type_name': POOL_TYPE_NAMES.get(pt, pt),
            'security_rate': sf(g(PX['security_rate'])),
            'issue_date': g(PX['issue_date']), 'pool_maturity_date': g(PX['maturity_date']),
            'orig_agg_amount': sf(g(PX['orig_agg_amount'])),
            'issuer_number': g(PX['issuer_number']), 'issuer_name': g(PX['issuer_name']),
            'pool_upb': sf(g(PX['pool_upb'])),
            # Security RPB / RPB Factor were added in V2.0. V1.0 files
            # don't have them at all, so PX_V10 drops those keys.
            'security_rpb': sf(g(PX['security_rpb'])) if 'security_rpb' in PX else np.nan,
            'rpb_factor':   sf(g(PX['rpb_factor']))   if 'rpb_factor'   in PX else np.nan,
            # V3.3-only pool fields (CL/CS only). NaN on every older
            # layout since P30/P31 don't exist before V3.3.
            'proj_loan_sec_rate': sf(g(P['proj_loan_sec_rate'])) if layout == 'V33' else np.nan,
            'est_mtg_amount':     sf(g(P['est_mtg_amount']))     if layout == 'V33' else np.nan,
        }
        if layout is not None and len(flds) > LX['case_number']:
            case = gL('case_number')
            # V3.1 used "NAF" for Affordable Status; V3.2 renamed this
            # to "MKT". Normalize at parse time so downstream code only
            # has to handle the modern value. For V3.0/V2.0/V1.0 the
            # field doesn't exist, gL returns '', and normalization is
            # a no-op.
            aff_status_raw = gL('affordable_status')
            aff_status = 'MKT' if aff_status_raw == 'NAF' else aff_status_raw
            rec.update({
                'case_number': case, 'loan_id': cusip + '_' + case,
                'agency_type': gL('agency_type'), 'loan_type': gL('loan_type'),
                'loan_term': si(gL('loan_term')),
                'first_pay_date': gL('first_pay_date'),
                'loan_maturity_date': gL('loan_maturity_date'),
                'loan_rate': sf(gL('loan_rate')),
                'modified_ind': gL('modified_ind'),
                'non_level_ind': gL('non_level_ind'),
                'mature_loan_flag': gL('mature_loan_flag'),
                'origination_date': gL('origination_date'),
                'lockout_term_yrs': si(gL('lockout_term')),
                'lockout_end_date': gL('lockout_end_date'),
                'prepay_premium_period_yrs': si(gL('prepay_premium_period')),
                'prepay_end_date': gL('prepay_end_date'),
                'prepay_penalty_flag': gL('prepay_penalty_flag'),
                'orig_prin_bal': sf(gL('orig_prin_bal')),
                'upb_at_issuance': sf(gL('upb_at_issuance')),
                'upb': sf(gL('upb')),
                'months_dq': si(gL('months_dq')),
                'liquidation_flag': gL('liquidation_flag'),
                'removal_reason': gL('removal_reason'),
                'property_name': gL('property_name'),
                'property_city': gL('property_city'),
                'property_state': gL('property_state'),
                'msa': gL('msa'),
                'num_units': si(gL('num_units')),
                'pi_amount': sf(gL('pi_amount')),
                'prepay_desc': gL('prepay_desc'),
                'fha_program_code': gL('fha_program_code'),
                'insurance_type': gL('insurance_type'),
                'as_of_date': gL('as_of_date'),
                'green_status': gL('green_status'),
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

    # Defensive row-level dedup. The parse-time `_dedupe_files_by_period`
    # already guarantees one source file per period, so duplicates here
    # would only come from a single source file listing the same
    # (pool_cusip, case_number) twice — rare but possible as a GNMA
    # data-quality issue. Drop such duplicates keeping the first row,
    # and log if any were removed so we notice if it becomes common.
    before = len(df)
    df = df.drop_duplicates(subset=['period', 'loan_id'], keep='first')
    dropped = before - len(df)
    if dropped:
        print(f"  ! dropped {dropped} duplicate (period, loan_id) row(s)")

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

    # Collapse to one file per period, preferring the era-correct
    # prefix. This also drops any stray files in DATA_DIR whose names
    # don't match the expected mfplmon*_YYYYMM.{zip,txt} pattern.
    file_by_period = _dedupe_files_by_period(allf)
    skipped = len(allf) - len(file_by_period)
    if skipped:
        print(f"[parse] collapsed {len(allf)} files to "
              f"{len(file_by_period)} (one per period; "
              f"{skipped} duplicate/unrecognized file(s) skipped)")

    print(f"\n[parse] Parsing {len(file_by_period)} files...")
    md = {}
    for per, fp in sorted(file_by_period.items()):
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
