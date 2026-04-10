#!/usr/bin/env python3
"""
GNMA Multifamily Prepayment Analysis
Uses ML techniques to predict prepayment behavior, generate S-curves,
and attribute prepayment probability to loan characteristics.
"""

import pandas as pd
import numpy as np
import json
import glob
import os
import warnings
warnings.filterwarnings('ignore')

# ── 1. Load & Preprocess ────────────────────────────────────────────────────

# Auto-detect the most recent gnma_mf_raw_data_*.csv.gz file (matches run.sh)
csv_files = sorted(glob.glob('gnma_mf_raw_data_*.csv.gz'),
                   key=os.path.getmtime, reverse=True)
if not csv_files:
    raise FileNotFoundError(
        "No gnma_mf_raw_data_*.csv.gz files found. Run main.py first.")
INPUT_CSV = csv_files[0]
print(f"Loading data from {INPUT_CSV}...")
df = pd.read_csv(INPUT_CSV, compression='gzip')
print(f"  Loaded {len(df):,} rows, {df['period'].nunique()} periods, "
      f"{df['loan_id'].nunique():,} unique loans")

# Filter to prepay-eligible loans only
eligible = df[df['prepay_eligible'] == 1].copy()
print(f"  Prepay-eligible rows: {len(eligible):,}")
print(f"  Voluntary prepays: {eligible['prepaid_voluntary'].sum():,}")

# ── 2. Feature Engineering ───────────────────────────────────────────────────

print("\nEngineering features...")

def yyyymmdd_to_months(yyyymmdd):
    y = yyyymmdd // 10000
    m = (yyyymmdd % 10000) // 100
    return y * 12 + m

eligible['period_months'] = yyyymmdd_to_months(eligible['period'] * 100 + 1)
eligible['first_pay_months'] = yyyymmdd_to_months(eligible['first_pay_date'])
eligible['loan_age_months'] = eligible['period_months'] - eligible['first_pay_months']
eligible['remaining_term'] = eligible['loan_term'] - eligible['loan_age_months']
eligible['log_upb'] = np.log1p(eligible['upb'])

# Refi incentive buckets (25bp)
eligible['refi_bucket'] = pd.cut(eligible['refi_incentive_bps'],
    bins=list(range(-500, 700, 25)),
    labels=[f"{x}" for x in range(-500, 675, 25)])

# Loan age buckets
eligible['age_bucket'] = pd.cut(eligible['loan_age_months'],
    bins=[0, 24, 60, 120, 180, 240, 600],
    labels=['0-2yr', '2-5yr', '5-10yr', '10-15yr', '15-20yr', '20yr+'])

# UPB bucket
eligible['upb_bucket'] = pd.cut(eligible['upb'] / 1e6,
    bins=[0, 1, 3, 7, 15, 50, 1000],
    labels=['<1M', '1-3M', '3-7M', '7-15M', '15-50M', '50M+'])

# Penalty status
eligible['penalty_status'] = 'past_all'
eligible.loc[eligible['in_prepay_penalty'] == 1, 'penalty_status'] = 'in_penalty'
eligible.loc[eligible['in_lockout'] == 1, 'penalty_status'] = 'in_lockout'

# State grouping
top_states = eligible['property_state'].value_counts().head(15).index.tolist()
eligible['state_group'] = eligible['property_state'].apply(
    lambda x: x if x in top_states else 'Other')

# Pool type
eligible['pool_type_group'] = eligible['pool_type'].map(
    {'PL': 'PL', 'PN': 'PN', 'RX': 'RX', 'LM': 'LM', 'LS': 'LS'}).fillna('Other')

# Green/affordable
eligible['is_green'] = (eligible['green_status'] == 'GRN').astype(int)
eligible['is_affordable'] = (eligible['affordable_status'] != 'MKT').astype(int)
eligible.loc[eligible['affordable_status'].isna(), 'is_affordable'] = 0
eligible['is_dq'] = (eligible['months_dq'] > 0).astype(int)

# Penalty points bucket
eligible['penalty_pts_bucket'] = pd.cut(eligible['prepay_penalty_points'],
    bins=[-0.1, 0, 1, 3, 5, 10.1],
    labels=['0%', '0-1%', '1-3%', '3-5%', '5-10%'])

print(f"  Done. Shape: {eligible.shape}")

# ── 3. Empirical S-Curves ───────────────────────────────────────────────────

print("\nComputing empirical S-curves...")

def compute_rates(data, group_col, min_obs=50):
    grp = data.groupby(group_col).agg(
        n_eligible=('prepaid_voluntary', 'count'),
        n_prepaid=('prepaid_voluntary', 'sum')
    ).reset_index()
    grp = grp[grp['n_eligible'] >= min_obs]
    grp['smm'] = grp['n_prepaid'] / grp['n_eligible']
    grp['cpr'] = 1 - (1 - grp['smm']) ** 12
    return grp

scurve_refi = compute_rates(eligible, 'refi_bucket', min_obs=100)
scurve_refi['refi_mid'] = scurve_refi['refi_bucket'].astype(str).astype(float) + 12.5

# By penalty status x refi
scurve_penalty = eligible.groupby(['penalty_status', 'refi_bucket']).agg(
    n_eligible=('prepaid_voluntary', 'count'),
    n_prepaid=('prepaid_voluntary', 'sum')
).reset_index()
scurve_penalty = scurve_penalty[scurve_penalty['n_eligible'] >= 50]
scurve_penalty['smm'] = scurve_penalty['n_prepaid'] / scurve_penalty['n_eligible']
scurve_penalty['cpr'] = 1 - (1 - scurve_penalty['smm']) ** 12
scurve_penalty['refi_mid'] = scurve_penalty['refi_bucket'].astype(str).astype(float) + 12.5

scurve_age = compute_rates(eligible, 'age_bucket')
scurve_upb = compute_rates(eligible, 'upb_bucket')
scurve_penpts = compute_rates(eligible, 'penalty_pts_bucket')

# By green/affordable
scurve_green = compute_rates(eligible, 'is_green')
scurve_afford = compute_rates(eligible, 'is_affordable')

# By state
scurve_state = compute_rates(eligible, 'state_group', min_obs=200)

print("  Done.")

# ── 4. ML Model ─────────────────────────────────────────────────────────────

print("\nBuilding XGBoost model...")
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, brier_score_loss
from xgboost import XGBClassifier
import shap

feature_cols = [
    'refi_incentive_bps', 'prepay_penalty_points', 'loan_age_months',
    'remaining_term', 'loan_rate', 'log_upb', 'num_units',
    'in_prepay_penalty', 'past_all_restrictions', 'is_green',
    'is_affordable', 'is_dq', 'security_rate'
]

cat_cols = ['penalty_status', 'state_group', 'pool_type_group']
for col in cat_cols:
    eligible[col] = eligible[col].fillna('Unknown')

eligible_model = eligible.dropna(subset=feature_cols).copy()
cat_dummies = pd.get_dummies(eligible_model[cat_cols], prefix=cat_cols, drop_first=True)
X = pd.concat([eligible_model[feature_cols].reset_index(drop=True),
               cat_dummies.reset_index(drop=True)], axis=1)
y = eligible_model['prepaid_voluntary'].values

# Time-based split: last 6 periods = test
test_periods = sorted(eligible_model['period'].unique())[-6:]
train_mask = ~eligible_model['period'].isin(test_periods).values
test_mask = eligible_model['period'].isin(test_periods).values

X_train, X_test = X[train_mask], X[test_mask]
y_train, y_test = y[train_mask], y[test_mask]
print(f"  Train: {len(X_train):,} ({y_train.sum()} prepays)  Test: {len(X_test):,} ({y_test.sum()} prepays)")

# Train without extreme class weighting — use moderate weight for better calibration
xgb = XGBClassifier(
    n_estimators=500, max_depth=4, learning_rate=0.03,
    scale_pos_weight=3.0,  # moderate — keeps probabilities calibrated
    subsample=0.8, colsample_bytree=0.7,
    min_child_weight=20, reg_alpha=2.0, reg_lambda=10.0,
    eval_metric='auc', random_state=42, verbosity=0
)
xgb.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

pred_train = xgb.predict_proba(X_train)[:, 1]
pred_test = xgb.predict_proba(X_test)[:, 1]

auc_train = roc_auc_score(y_train, pred_train)
auc_test = roc_auc_score(y_test, pred_test)
brier = brier_score_loss(y_test, pred_test)
print(f"  Train AUC: {auc_train:.4f}  Test AUC: {auc_test:.4f}  Test Brier: {brier:.6f}")

# Feature importance
feat_imp = pd.DataFrame({'feature': X.columns, 'importance': xgb.feature_importances_})
feat_imp = feat_imp.sort_values('importance', ascending=False)

# ── 5. SHAP ─────────────────────────────────────────────────────────────────

print("\nComputing SHAP values...")
explainer = shap.TreeExplainer(xgb)

# Global SHAP on test sample
np.random.seed(42)
shap_idx = np.random.choice(len(X_test), min(3000, len(X_test)), replace=False)
X_shap_global = X_test.iloc[shap_idx]
shap_vals_global = explainer.shap_values(X_shap_global)

shap_importance = pd.DataFrame({
    'feature': X.columns,
    'mean_abs_shap': np.abs(shap_vals_global).mean(axis=0)
}).sort_values('mean_abs_shap', ascending=False)

print("  Top 10 SHAP features:")
for _, row in shap_importance.head(10).iterrows():
    print(f"    {row['feature']:35s} {row['mean_abs_shap']:.4f}")

# ── 6. Sample Loan Attribution ──────────────────────────────────────────────

print("\nBuilding sample loan attributions...")

baseline_smm = y.mean()  # unconditional SMM
baseline_cpr = 1 - (1 - baseline_smm) ** 12
print(f"  Unconditional baseline SMM: {baseline_smm:.4f} ({baseline_smm*100:.2f}%), CPR: {baseline_cpr:.2%}")

test_df = eligible_model[test_mask].copy()
test_df['pred_smm'] = pred_test

base_val = explainer.expected_value
if isinstance(base_val, np.ndarray):
    base_val = float(base_val[0])

# Select 7 diverse sample loans
samples = []

# 3 that actually prepaid
prepaid_df = test_df[test_df['prepaid_voluntary'] == 1]
if len(prepaid_df) >= 3:
    chosen = prepaid_df.sample(3, random_state=42)
    for idx in chosen.index:
        samples.append((idx, 'Prepaid'))

# 2 high-risk that didn't prepay
not_prepaid = test_df[test_df['prepaid_voluntary'] == 0]
high_risk = not_prepaid.nlargest(100, 'pred_smm').sample(2, random_state=42)
for idx in high_risk.index:
    samples.append((idx, 'High-Risk (survived)'))

# 2 low-risk
low_risk = not_prepaid.nsmallest(100, 'pred_smm').sample(2, random_state=42)
for idx in low_risk.index:
    samples.append((idx, 'Low-Risk'))

sample_results = []
for orig_idx, label in samples:
    row = test_df.loc[orig_idx]
    # Find position in X_test
    pos = test_df.index.get_loc(orig_idx)
    x_row = X_test.iloc[[pos]]
    sv = explainer.shap_values(x_row)[0]
    pred = xgb.predict_proba(x_row)[0, 1]

    contribs = sorted(
        zip(X.columns, sv, x_row.values[0]),
        key=lambda t: abs(t[1]), reverse=True
    )[:10]

    sample_results.append({
        'label': label,
        'loan_id': str(row['loan_id']),
        'property_name': str(row.get('property_name', 'N/A')),
        'property_state': str(row['property_state']),
        'loan_rate': float(row['loan_rate']),
        'refi_incentive_bps': float(row['refi_incentive_bps']),
        'penalty_status': str(row['penalty_status']),
        'prepay_penalty_points': float(row['prepay_penalty_points']),
        'loan_age_months': int(row['loan_age_months']),
        'upb': float(row['upb']),
        'num_units': int(row['num_units']),
        'pred_smm': float(pred),
        'pred_cpr': float(1 - (1 - pred) ** 12),
        'base_shap': float(base_val),
        'top_factors': [
            {'feature': f, 'shap': float(s), 'value': float(v)}
            for f, s, v in contribs
        ]
    })

print(f"  Built attributions for {len(sample_results)} loans.")

# ── 7. Model-Implied S-Curve ────────────────────────────────────────────────

print("\nGenerating model-implied S-curves...")
median_feats = X_train.median()
refi_grid = np.arange(-500, 675, 12.5)

# Overall model curve
syn = pd.DataFrame([median_feats] * len(refi_grid))
syn['refi_incentive_bps'] = refi_grid
syn['in_prepay_penalty'] = 0
syn['past_all_restrictions'] = 1
# Reset penalty status dummies
for c in syn.columns:
    if c.startswith('penalty_status_'):
        syn[c] = 0
if 'penalty_status_past_all' in syn.columns:
    syn['penalty_status_past_all'] = 1

model_cpr_free = 1 - (1 - xgb.predict_proba(syn)[:, 1]) ** 12

# In-penalty curve
syn2 = syn.copy()
syn2['in_prepay_penalty'] = 1
syn2['past_all_restrictions'] = 0
syn2['prepay_penalty_points'] = 5.0
for c in syn2.columns:
    if c.startswith('penalty_status_'):
        syn2[c] = 0
if 'penalty_status_in_penalty' in syn2.columns:
    syn2['penalty_status_in_penalty'] = 1
model_cpr_penalty = 1 - (1 - xgb.predict_proba(syn2)[:, 1]) ** 12

print("  Done.")

# ── 8. Save everything as JSON for the report ───────────────────────────────

print("\nSaving results...")

def df_to_records(d):
    return d.to_dict('records')

results = {
    'summary': {
        'n_total': int(len(df)),
        'n_eligible': int(len(eligible)),
        'n_prepaid_vol': int(eligible['prepaid_voluntary'].sum()),
        'n_prepaid_invol': int(eligible['prepaid_involuntary'].sum()),
        'n_periods': int(df['period'].nunique()),
        'n_loans': int(df['loan_id'].nunique()),
        'baseline_smm': float(baseline_smm),
        'baseline_cpr': float(baseline_cpr),
        'train_size': int(len(X_train)),
        'test_size': int(len(X_test)),
        'train_prepays': int(y_train.sum()),
        'test_prepays': int(y_test.sum()),
        'auc_train': float(auc_train),
        'auc_test': float(auc_test),
        'brier_test': float(brier),
        'test_periods': [int(p) for p in test_periods],
    },
    'scurve_refi': [{'refi_mid': float(r['refi_mid']), 'cpr': float(r['cpr']),
                      'smm': float(r['smm']), 'n': int(r['n_eligible'])}
                     for _, r in scurve_refi.iterrows()],
    'scurve_penalty': {},
    'scurve_age': [{'bucket': str(r['age_bucket']), 'cpr': float(r['cpr']),
                     'smm': float(r['smm']), 'n': int(r['n_eligible'])}
                    for _, r in scurve_age.iterrows()],
    'scurve_upb': [{'bucket': str(r['upb_bucket']), 'cpr': float(r['cpr']),
                     'smm': float(r['smm']), 'n': int(r['n_eligible'])}
                    for _, r in scurve_upb.iterrows()],
    'scurve_penpts': [{'bucket': str(r['penalty_pts_bucket']), 'cpr': float(r['cpr']),
                        'smm': float(r['smm']), 'n': int(r['n_eligible'])}
                       for _, r in scurve_penpts.iterrows()],
    'scurve_state': [{'state': str(r['state_group']), 'cpr': float(r['cpr']),
                       'smm': float(r['smm']), 'n': int(r['n_eligible'])}
                      for _, r in scurve_state.sort_values('cpr', ascending=False).iterrows()],
    'feat_imp': [{'feature': str(r['feature']), 'importance': float(r['importance'])}
                  for _, r in feat_imp.head(20).iterrows()],
    'shap_imp': [{'feature': str(r['feature']), 'shap': float(r['mean_abs_shap'])}
                  for _, r in shap_importance.head(20).iterrows()],
    'model_scurve': {
        'refi_grid': [float(x) for x in refi_grid],
        'cpr_free': [float(x) for x in model_cpr_free],
        'cpr_penalty': [float(x) for x in model_cpr_penalty],
    },
    'sample_loans': sample_results,
}

# Penalty S-curves by status
for ps in scurve_penalty['penalty_status'].unique():
    sub = scurve_penalty[scurve_penalty['penalty_status'] == ps]
    results['scurve_penalty'][ps] = [
        {'refi_mid': float(r['refi_mid']), 'cpr': float(r['cpr']),
         'smm': float(r['smm']), 'n': int(r['n_eligible'])}
        for _, r in sub.iterrows()
    ]

with open('analysis_results.json', 'w') as f:
    json.dump(results, f, indent=2)

print("Results saved to analysis_results.json")
print("\nDone!")
