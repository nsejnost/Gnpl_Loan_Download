"""
GNMA Multifamily Data Downloader & S-Curve Builder
===================================================
Authenticates with GNMA Disclosure Data Download using Firefox (Playwright),
downloads mfplmon3 monthly portfolio files, tracks loans across months,
identifies prepayments, and builds an S-curve panel dataset in Excel.

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
    print(f"Missing: {e}\nInstall: pip install requests pandas numpy openpyxl")
    sys.exit(1)

# ─── CONFIGURATION ────────────────────────────────────────
BASE_URL = "https://www.ginniemae.gov"
BULK_URL = "https://bulk.ginniemae.gov"
DOWNLOAD_PAGE = f"{BASE_URL}/data_and_reports/disclosure_data/Pages/datadownload_bulk.aspx"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "gnma_mf_data")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "gnma_mf_scurve_dataset.xlsx")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

POOL_TYPE_NAMES = {
    'PL': 'Project Loan', 'PN': 'Non-Level PL', 'LM': 'Mature/Modified',
    'LS': 'Small PL', 'RX': 'Mark-to-Market',
    'CL': 'Construction (Same)', 'CS': 'Construction (Diff)',
}
SCURVE_POOL_TYPES = {'PL', 'PN', 'LM', 'LS', 'RX'}

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
    browser = pw.firefox.launch(headless=True)
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

def pd_(s):
    s = (s or "").strip()
    if not s or len(s) < 6: return pd.NaT
    try:
        if len(s) == 8: return pd.Timestamp(f"{s[:4]}-{s[4:6]}-{s[6:8]}")
        if len(s) == 6: return pd.Timestamp(f"{s[:4]}-{s[4:6]}-01")
    except: pass
    return pd.NaT

def mb(d1, d2):
    if pd.isna(d1) or pd.isna(d2): return np.nan
    return (d2.year - d1.year) * 12 + (d2.month - d1.month)

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
#  PANEL CONSTRUCTION & S-CURVE
# ═══════════════════════════════════════════════════════════

def build_panel(monthly_data):
    periods = sorted(monthly_data.keys())
    print(f"\n[panel] Building: {len(periods)} periods, "
          f"{sum(len(v) for v in monthly_data.values())} total records")
    pmap = {p: {r['loan_id']: r for r in recs} for p, recs in monthly_data.items()}
    rows = []
    for i, period in enumerate(periods):
        nxt = periods[i+1] if i+1 < len(periods) else None
        nl = pmap.get(nxt, {}) if nxt else {}
        od = pd_(period)
        for lid, r in pmap[period].items():
            if r['pool_type'] not in SCURVE_POOL_TYPES: continue
            rm = r.get('removal_reason','').strip()
            iv = rm == '1'; ii = rm in ('2','3','4','6')
            dis = nxt is not None and lid not in nl
            mdq = r.get('months_dq',0) or 0
            if dis and not iv and not ii:
                iv = mdq == 0; ii = mdq > 0
            fp = pd_(r.get('first_pay_date','')); lm = pd_(r.get('loan_maturity_date',''))
            le = pd_(r.get('lockout_end_date','')); pe = pd_(r.get('prepay_end_date',''))
            age = mb(fp, od); rem = mb(od, lm)
            il = 1 if pd.notna(le) and od < le else 0
            ip = 1 if pd.notna(pe) and od < pe else 0
            pa = 1 if not il and not ip else 0
            lr = r.get('loan_rate', np.nan); sr = r.get('security_rate', np.nan)
            smm = np.nan
            if nxt and lid in nl:
                cu = r.get('upb', np.nan); nu = nl[lid].get('upb', np.nan)
                if pd.notna(cu) and pd.notna(nu) and cu > 0: smm = 1-(nu/cu)
            rows.append({
                'period': period, 'obs_date': od, 'loan_id': lid,
                'pool_cusip': r['pool_cusip'], 'pool_number': r['pool_number'],
                'pool_type': r['pool_type'], 'case_number': r.get('case_number',''),
                'agency_type': r.get('agency_type',''),
                'fha_program_code': r.get('fha_program_code',''),
                'loan_rate': lr, 'security_rate': sr,
                'servicing_spread': (lr-sr) if pd.notna(lr) and pd.notna(sr) else np.nan,
                'benchmark_rate': np.nan, 'refi_incentive_bps': np.nan,
                'orig_prin_bal': r.get('orig_prin_bal', np.nan),
                'upb_at_issuance': r.get('upb_at_issuance', np.nan),
                'current_upb': r.get('upb', np.nan),
                'pool_security_rpb': r.get('security_rpb', np.nan),
                'rpb_factor': r.get('rpb_factor', np.nan),
                'origination_date': r.get('origination_date',''),
                'first_pay_date': r.get('first_pay_date',''),
                'loan_maturity_date': r.get('loan_maturity_date',''),
                'loan_term_months': r.get('loan_term', 0),
                'age_months': age, 'remaining_term_months': rem,
                'lockout_term_yrs': r.get('lockout_term_yrs', 0),
                'lockout_end_date': r.get('lockout_end_date',''),
                'prepay_premium_period_yrs': r.get('prepay_premium_period_yrs', 0),
                'prepay_end_date': r.get('prepay_end_date',''),
                'prepay_penalty_flag': r.get('prepay_penalty_flag',''),
                'prepay_desc': r.get('prepay_desc',''),
                'in_lockout': il, 'in_prepay_penalty': ip, 'past_all_restrictions': pa,
                'months_to_lockout_end': max(0, mb(od,le) or 0) if pd.notna(le) and od<le else 0,
                'months_to_prepay_end': max(0, mb(od,pe) or 0) if pd.notna(pe) and od<pe else 0,
                'prepaid_voluntary': 1 if iv else 0,
                'prepaid_involuntary': 1 if ii else 0,
                'prepaid_any': 1 if iv or ii else 0,
                'removal_reason': rm, 'liquidation_flag': r.get('liquidation_flag',''),
                'months_dq': mdq, 'modified_ind': r.get('modified_ind',''),
                'insurance_type': r.get('insurance_type',''),
                'property_name': r.get('property_name',''),
                'property_state': r.get('property_state',''),
                'property_city': r.get('property_city',''),
                'msa': r.get('msa',''), 'num_units': r.get('num_units', 0),
                'green_status': r.get('green_status',''),
                'affordable_status': r.get('affordable_status',''),
                'non_level_ind': r.get('non_level_ind',''),
                'mature_loan_flag': r.get('mature_loan_flag',''),
                'issuer_number': r.get('issuer_number',''),
                'issuer_name': r.get('issuer_name',''),
                'smm_approx': smm,
            })
    return pd.DataFrame(rows)


def build_summary(df):
    rows = []
    for period in sorted(df['period'].unique()):
        p = df[df['period']==period]; n = len(p)
        upb = p['current_upb'].sum(); vol = p['prepaid_voluntary'].sum()
        inv = p['prepaid_involuntary'].sum()
        wr = np.average(p['loan_rate'].dropna(),
            weights=p.loc[p['loan_rate'].notna(),'current_upb'].clip(lower=.01)
        ) if p['loan_rate'].notna().any() else np.nan
        wa = np.average(p['age_months'].dropna(),
            weights=p.loc[p['age_months'].notna(),'current_upb'].clip(lower=.01)
        ) if p['age_months'].notna().any() else np.nan
        rows.append({
            'Period': period, 'Loans': n, 'UPB ($M)': upb/1e6,
            'Vol': int(vol), 'Invol': int(inv),
            'CPR (ann %)': (1-(1-vol/max(n,1))**12)*100,
            'WA Rate': wr, 'WA Age': wa,
            '% Lock': p['in_lockout'].mean()*100,
            '% Pen': p['in_prepay_penalty'].mean()*100,
            '% Open': p['past_all_restrictions'].mean()*100,
        })
    return pd.DataFrame(rows)


def build_scurve_buckets(df):
    d = df[df['loan_rate'].notna()].copy()
    d['b'] = (d['loan_rate']*4).round()/4
    g = d.groupby('b').agg(
        n=('loan_id','count'), upb=('current_upb','sum'),
        pp=('prepaid_voluntary','sum'), age=('age_months','mean'),
        lk=('in_lockout','mean'), op=('past_all_restrictions','mean'),
    ).reset_index()
    g['smm'] = g['pp']/g['n'].clip(lower=1)
    g['cpr'] = (1-(1-g['smm'])**12)*100
    g['upb'] /= 1e6; g['lk'] *= 100; g['op'] *= 100
    return g.rename(columns={
        'b':'Rate (%)','n':'Loans','upb':'UPB ($M)','pp':'Prepaid',
        'age':'WA Age','lk':'% Locked','op':'% Open','smm':'SMM','cpr':'CPR (ann %)',
    })[['Rate (%)','Loans','UPB ($M)','Prepaid','SMM','CPR (ann %)','WA Age','% Locked','% Open']].sort_values('Rate (%)')


def build_lockout(df):
    cats = [('In Lockout', df[df['in_lockout']==1]),
            ('In Penalty', df[(df['in_lockout']==0)&(df['in_prepay_penalty']==1)]),
            ('Open', df[df['past_all_restrictions']==1])]
    rows = []
    for lbl, sub in cats:
        n = len(sub)
        if n == 0: continue
        rows.append({
            'Category': lbl, 'Loans': n, 'UPB ($M)': sub['current_upb'].sum()/1e6,
            'Vol': int(sub['prepaid_voluntary'].sum()),
            'SMM': sub['prepaid_voluntary'].sum()/max(n,1),
            'CPR (ann %)': (1-(1-sub['prepaid_voluntary'].sum()/max(n,1))**12)*100,
            'WA Rate': np.average(sub['loan_rate'].dropna(),
                weights=sub.loc[sub['loan_rate'].notna(),'current_upb'].clip(lower=.01)
            ) if sub['loan_rate'].notna().any() else np.nan,
            'WA Age': sub['age_months'].mean(),
        })
    return pd.DataFrame(rows)


def write_output(df, monthly_data):
    """Write Excel output using only pandas ExcelWriter (no load_workbook).
    This avoids the XML/expat conflict on Replit."""
    print(f"\n[output] Writing {OUTPUT_FILE}...")

    pcols = [
        'period','loan_id','pool_cusip','pool_number','pool_type',
        'case_number','fha_program_code','agency_type',
        'loan_rate','security_rate','servicing_spread',
        'benchmark_rate','refi_incentive_bps',
        'current_upb','orig_prin_bal','rpb_factor',
        'age_months','remaining_term_months','loan_term_months',
        'in_lockout','in_prepay_penalty','past_all_restrictions',
        'months_to_lockout_end','months_to_prepay_end',
        'lockout_end_date','prepay_end_date','prepay_desc',
        'prepaid_voluntary','prepaid_involuntary','prepaid_any',
        'removal_reason','months_dq','modified_ind',
        'property_state','num_units','green_status','affordable_status',
        'issuer_name','smm_approx',
    ]

    # Build instructions as a DataFrame
    instr_rows = [
        {'A': 'GNMA MF Prepayment S-Curve Dataset', 'B': ''},
        {'A': '', 'B': ''},
        {'A': 'Generated', 'B': datetime.now().strftime('%Y-%m-%d %H:%M')},
        {'A': 'Periods', 'B': ', '.join(sorted(monthly_data.keys()))},
        {'A': 'Observations', 'B': str(len(df))},
        {'A': 'Unique Loans', 'B': str(df['loan_id'].nunique())},
        {'A': 'Vol Prepays', 'B': str(int(df['prepaid_voluntary'].sum()))},
        {'A': '', 'B': ''},
        {'A': 'TO COMPLETE S-CURVE:', 'B': ''},
        {'A': '1.', 'B': 'Fill benchmark_rate column with prevailing GNMA MF coupon for each period'},
        {'A': '2.', 'B': 'Compute: refi_incentive_bps = (loan_rate - benchmark_rate) * 10000'},
        {'A': '3.', 'B': 'Re-bucket by incentive for the final S-curve shape'},
        {'A': '4.', 'B': 'Fit: P(prepay) = f(incentive, age, lockout, penalty, loan_size, ...)'},
        {'A': '', 'B': ''},
        {'A': 'PREPAYMENT FLAGS:', 'B': ''},
        {'A': 'prepaid_voluntary=1', 'B': 'Mortgagor payoff or loan disappears while current'},
        {'A': 'prepaid_involuntary=1', 'B': 'Repurchase/foreclosure or disappears while delinquent'},
        {'A': 'Note', 'B': 'Construction loans (CL/CS) excluded from panel'},
    ]
    instr_df = pd.DataFrame(instr_rows)

    # Write all tabs using pandas only (no load_workbook)
    with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as writer:
        instr_df.to_excel(writer, sheet_name='Instructions', index=False, header=False)
        build_summary(df).to_excel(writer, sheet_name='Summary', index=False)
        build_scurve_buckets(df).to_excel(writer, sheet_name='S-Curve Buckets', index=False)
        build_lockout(df).to_excel(writer, sheet_name='Lockout Analysis', index=False)
        df[pcols].to_excel(writer, sheet_name='Loan Panel', index=False)
        df.to_excel(writer, sheet_name='Full Detail', index=False)

    print(f"\n{'='*60}")
    print(f"  ok {OUTPUT_FILE}")
    print(f"     {len(df)} obs, {df['loan_id'].nunique()} loans, "
          f"{int(df['prepaid_voluntary'].sum())} vol prepays")
    print(f"     Tabs: Instructions | Summary | S-Curve Buckets | "
          f"Lockout | Loan Panel | Full Detail")
    print(f"{'='*60}")


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="GNMA MF Downloader & S-Curve Builder")
    ap.add_argument("--email", help="GNMA Disclosure email")
    ap.add_argument("--answer", help="Security question answer")
    ap.add_argument("--months", type=int, default=6, help="Months to download (default: 6)")
    ap.add_argument("--skip-download", action="store_true", help="Skip download, parse existing files")
    ap.add_argument("--data-dir", help="Override data directory")
    args = ap.parse_args()

    global DATA_DIR, OUTPUT_FILE
    if args.data_dir:
        DATA_DIR = args.data_dir
        OUTPUT_FILE = os.path.join(args.data_dir, "gnma_mf_scurve_dataset.xlsx")

    print("""
+==============================================================+
|  GNMA Multifamily Data Downloader & S-Curve Builder          |
|  Auth: Firefox (Playwright) -> cookie transfer -> downloads  |
+==============================================================+
""")

    if not args.skip_download:
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

    # Clear LD_LIBRARY_PATH to avoid expat/XML conflicts during Excel write
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

    if len(md) < 2:
        print("\nNeed 2+ months for prepay identification."); return

    df = build_panel(md)
    print(f"\n[panel] {len(df)} loan-month obs, {df['loan_id'].nunique()} unique loans")
    print(f"  Vol prepays: {df['prepaid_voluntary'].sum():.0f}")
    print(f"  Invol removals: {df['prepaid_involuntary'].sum():.0f}")
    write_output(df, md)


if __name__ == "__main__":
    main()
