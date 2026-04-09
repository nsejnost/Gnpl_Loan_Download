#!/usr/bin/env python3
"""Generate HTML report from analysis results."""

import json

with open('analysis_results.json') as f:
    R = json.load(f)

S = R['summary']

# ── Helper: build Chart.js datasets ─────────────────────────────────────────

def scurve_refi_datasets():
    """Empirical + model S-curves on same chart."""
    emp = R['scurve_refi']
    mod = R['model_scurve']
    return f"""{{
        labels: {json.dumps([d['refi_mid'] for d in emp])},
        datasets: [{{
            label: 'Empirical CPR',
            data: {json.dumps([round(d['cpr']*100, 2) for d in emp])},
            borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,0.1)',
            fill: true, tension: 0.3, pointRadius: 4
        }}, {{
            label: 'Model CPR (no penalty)',
            data: {json.dumps([round(mod['cpr_free'][i]*100, 2) for i, x in enumerate(mod['refi_grid']) if any(abs(x+6.25 - d['refi_mid']) < 1 for d in emp)])},
            borderColor: '#16a34a', borderDash: [6,3], tension: 0.3, pointRadius: 0
        }}]
    }}"""


def scurve_penalty_datasets():
    """S-curves split by penalty status."""
    datasets = []
    colors = {'past_all': '#2563eb', 'in_penalty': '#dc2626', 'in_lockout': '#f59e0b'}
    labels_map = {'past_all': 'Past All Restrictions', 'in_penalty': 'In Penalty Period', 'in_lockout': 'In Lockout'}
    for status, data in R['scurve_penalty'].items():
        datasets.append(f"""{{
            label: '{labels_map.get(status, status)}',
            data: {json.dumps([{'x': d['refi_mid'], 'y': round(d['cpr']*100, 2)} for d in data])},
            borderColor: '{colors.get(status, "#666")}',
            tension: 0.3, pointRadius: 3, fill: false
        }}""")
    return ',\n            '.join(datasets)


def bar_chart_data(records, key_field, val_field='cpr', label='CPR %'):
    labels = [str(d[key_field]) for d in records]
    values = [round(d[val_field]*100, 2) for d in records]
    return f"""{{
        labels: {json.dumps(labels)},
        datasets: [{{ label: '{label}', data: {json.dumps(values)},
            backgroundColor: 'rgba(37,99,235,0.7)', borderColor: '#2563eb', borderWidth: 1 }}]
    }}"""


def feat_imp_data():
    top = R['feat_imp'][:15]
    return f"""{{
        labels: {json.dumps([d['feature'] for d in top])},
        datasets: [{{ label: 'Gain Importance', data: {json.dumps([round(d['importance'], 4) for d in top])},
            backgroundColor: 'rgba(37,99,235,0.7)' }}]
    }}"""


def shap_imp_data():
    top = R['shap_imp'][:15]
    return f"""{{
        labels: {json.dumps([d['feature'] for d in top])},
        datasets: [{{ label: 'Mean |SHAP|', data: {json.dumps([round(d['shap'], 4) for d in top])},
            backgroundColor: 'rgba(220,38,38,0.7)' }}]
    }}"""


def model_scurve_data():
    mod = R['model_scurve']
    return f"""{{
        labels: {json.dumps([float(x) for x in mod['refi_grid']])},
        datasets: [{{
            label: 'Past All Restrictions',
            data: {json.dumps([round(x*100, 2) for x in mod['cpr_free']])},
            borderColor: '#2563eb', tension: 0.3, pointRadius: 0, fill: false
        }}, {{
            label: 'In Penalty (5% pts)',
            data: {json.dumps([round(x*100, 2) for x in mod['cpr_penalty']])},
            borderColor: '#dc2626', tension: 0.3, pointRadius: 0, fill: false, borderDash: [6,3]
        }}]
    }}"""


def loan_attribution_html(loan):
    """Generate waterfall-style attribution for a single loan."""
    factors = loan['top_factors'][:8]
    base_logodds = loan['base_shap']

    # Color code: positive SHAP = increases prepay risk = red, negative = green
    rows = ""
    for f in factors:
        color = '#dc2626' if f['shap'] > 0 else '#16a34a'
        bar_width = min(abs(f['shap']) / 0.5 * 100, 100)
        direction = 'right' if f['shap'] > 0 else 'left'
        feature_name = f['feature'].replace('_', ' ').replace('penalty status ', '').replace('state group ', 'State: ')

        # Format feature value
        val = f['value']
        if abs(val) > 1000:
            val_str = f"{val:,.0f}"
        elif abs(val) > 1:
            val_str = f"{val:.1f}"
        else:
            val_str = f"{val:.3f}"

        rows += f"""
        <tr>
            <td class="factor-name">{feature_name}</td>
            <td class="factor-value">{val_str}</td>
            <td class="factor-bar">
                <div class="bar-container">
                    <div class="bar" style="width:{bar_width:.0f}%; background:{color};
                         {'margin-left:auto' if direction == 'left' else ''}"></div>
                </div>
            </td>
            <td class="factor-shap" style="color:{color}">{f['shap']:+.3f}</td>
        </tr>"""

    return f"""
    <div class="loan-card {'prepaid' if 'Prepaid' in loan['label'] else 'high-risk' if 'High' in loan['label'] else 'low-risk'}">
        <div class="loan-header">
            <span class="loan-label">{loan['label']}</span>
            <span class="loan-pred">Predicted SMM: {loan['pred_smm']:.4f} | CPR: {loan['pred_cpr']*100:.2f}%</span>
        </div>
        <div class="loan-details">
            <div><strong>State:</strong> {loan['property_state']}</div>
            <div><strong>Rate:</strong> {loan['loan_rate']:.2f}%</div>
            <div><strong>Refi Incentive:</strong> {loan['refi_incentive_bps']:.0f} bps</div>
            <div><strong>Penalty Status:</strong> {loan['penalty_status']}</div>
            <div><strong>Penalty Points:</strong> {loan['prepay_penalty_points']:.1f}%</div>
            <div><strong>Loan Age:</strong> {loan['loan_age_months']} months</div>
            <div><strong>UPB:</strong> ${loan['upb']:,.0f}</div>
            <div><strong>Units:</strong> {loan['num_units']}</div>
        </div>
        <div class="loan-subtitle">Top Factors Driving Prediction vs. Baseline</div>
        <table class="factor-table">
            <thead><tr><th>Factor</th><th>Value</th><th>Impact</th><th>SHAP</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


# ── Build the full HTML ─────────────────────────────────────────────────────

# Empirical refi S-curve data
emp_refi = R['scurve_refi']
emp_labels = json.dumps([d['refi_mid'] for d in emp_refi])
emp_cprs = json.dumps([round(d['cpr']*100, 2) for d in emp_refi])
emp_ns = json.dumps([d['n'] for d in emp_refi])

# Model S-curve matching empirical x-axis
mod = R['model_scurve']
model_free_at_emp = []
model_pen_at_emp = []
for d in emp_refi:
    target = d['refi_mid']
    closest_idx = min(range(len(mod['refi_grid'])),
                      key=lambda i: abs(mod['refi_grid'][i] + 6.25 - target))
    model_free_at_emp.append(round(mod['cpr_free'][closest_idx]*100, 2))
    model_pen_at_emp.append(round(mod['cpr_penalty'][closest_idx]*100, 2))

loan_cards = '\n'.join(loan_attribution_html(l) for l in R['sample_loans'])

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GNMA Multifamily Prepayment Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
  :root {{ --blue: #2563eb; --red: #dc2626; --green: #16a34a; --amber: #f59e0b;
           --gray: #6b7280; --bg: #f8fafc; --card: #ffffff; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          background: var(--bg); color: #1e293b; line-height: 1.6; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 2rem; }}
  h1 {{ font-size: 2rem; font-weight: 700; margin-bottom: 0.5rem; color: #0f172a; }}
  h2 {{ font-size: 1.5rem; font-weight: 600; margin: 2.5rem 0 1rem; color: #0f172a;
        border-bottom: 2px solid var(--blue); padding-bottom: 0.5rem; }}
  h3 {{ font-size: 1.1rem; font-weight: 600; margin: 1.5rem 0 0.75rem; color: #334155; }}
  .subtitle {{ color: var(--gray); font-size: 1rem; margin-bottom: 2rem; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 1rem; margin: 1.5rem 0; }}
  .stat-card {{ background: var(--card); border-radius: 8px; padding: 1.25rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; }}
  .stat-value {{ font-size: 1.8rem; font-weight: 700; color: var(--blue); }}
  .stat-label {{ font-size: 0.85rem; color: var(--gray); margin-top: 0.25rem; }}
  .chart-container {{ background: var(--card); border-radius: 8px; padding: 1.5rem;
                      box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin: 1.5rem 0; }}
  .chart-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }}
  @media (max-width: 768px) {{ .chart-row {{ grid-template-columns: 1fr; }} }}
  canvas {{ max-height: 400px; }}
  .insight {{ background: #eff6ff; border-left: 4px solid var(--blue); padding: 1rem 1.25rem;
              border-radius: 0 8px 8px 0; margin: 1rem 0; font-size: 0.95rem; }}
  .insight strong {{ color: var(--blue); }}
  table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 0.9rem; }}
  th {{ background: #f1f5f9; padding: 0.5rem 0.75rem; text-align: left; font-weight: 600; }}
  td {{ padding: 0.5rem 0.75rem; border-bottom: 1px solid #e2e8f0; }}
  .loan-card {{ background: var(--card); border-radius: 8px; padding: 1.5rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin: 1.25rem 0;
                border-left: 4px solid var(--gray); }}
  .loan-card.prepaid {{ border-left-color: var(--red); }}
  .loan-card.high-risk {{ border-left-color: var(--amber); }}
  .loan-card.low-risk {{ border-left-color: var(--green); }}
  .loan-header {{ display: flex; justify-content: space-between; align-items: center;
                   margin-bottom: 0.75rem; flex-wrap: wrap; gap: 0.5rem; }}
  .loan-label {{ font-weight: 700; font-size: 1.1rem; }}
  .loan-pred {{ font-family: monospace; color: var(--gray); font-size: 0.9rem; }}
  .loan-details {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                    gap: 0.5rem; margin-bottom: 1rem; font-size: 0.9rem; }}
  .loan-subtitle {{ font-weight: 600; font-size: 0.95rem; margin-bottom: 0.5rem; color: #334155; }}
  .factor-table {{ font-size: 0.85rem; }}
  .factor-table th {{ font-size: 0.8rem; }}
  .factor-name {{ font-weight: 500; white-space: nowrap; }}
  .factor-value {{ font-family: monospace; text-align: right; white-space: nowrap; }}
  .factor-shap {{ font-family: monospace; font-weight: 600; text-align: right; white-space: nowrap; }}
  .bar-container {{ width: 120px; height: 14px; background: #f1f5f9; border-radius: 3px;
                     display: flex; }}
  .bar {{ height: 100%; border-radius: 3px; min-width: 2px; }}
  .methodology {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
                   padding: 1.5rem; margin: 1.5rem 0; font-size: 0.9rem; }}
  .methodology ul {{ margin-left: 1.25rem; }}
  .methodology li {{ margin: 0.3rem 0; }}
  .footer {{ text-align: center; color: var(--gray); font-size: 0.8rem; margin-top: 3rem;
              padding-top: 1.5rem; border-top: 1px solid #e2e8f0; }}
</style>
</head>
<body>
<div class="container">

<h1>GNMA Multifamily Prepayment Analysis</h1>
<p class="subtitle">Machine learning analysis of prepayment patterns in GNMA multifamily mortgage-backed securities.<br>
Data: {S['n_periods']} monthly periods &middot; {S['n_loans']:,} unique loans &middot; {S['n_total']:,} loan-month observations</p>

<!-- ── Summary Stats ──────────────────────────────────────── -->
<h2>1. Dataset Overview</h2>
<div class="stat-grid">
  <div class="stat-card"><div class="stat-value">{S['n_loans']:,}</div><div class="stat-label">Unique Loans</div></div>
  <div class="stat-card"><div class="stat-value">{S['n_eligible']:,}</div><div class="stat-label">Eligible Loan-Months</div></div>
  <div class="stat-card"><div class="stat-value">{S['n_prepaid_vol']}</div><div class="stat-label">Voluntary Prepayments</div></div>
  <div class="stat-card"><div class="stat-value">{S['baseline_smm']*100:.2f}%</div><div class="stat-label">Avg Monthly Prepay (SMM)</div></div>
  <div class="stat-card"><div class="stat-value">{S['baseline_cpr']*100:.1f}%</div><div class="stat-label">Annualized CPR</div></div>
  <div class="stat-card"><div class="stat-value">{S['auc_test']:.3f}</div><div class="stat-label">Model AUC (Test)</div></div>
</div>

<div class="insight">
  <strong>Key finding:</strong> The unconditional monthly prepayment rate (SMM) is {S['baseline_smm']*100:.2f}%,
  corresponding to an annualized CPR of {S['baseline_cpr']*100:.1f}%. This serves as the baseline against
  which individual loan characteristics shift prepayment probability.
</div>

<!-- ── Empirical S-Curves ─────────────────────────────────── -->
<h2>2. Prepayment S-Curves</h2>

<h3>2a. Refi Incentive S-Curve (Empirical)</h3>
<p>CPR by refinance incentive bucket (net coupon minus PLC rate, in basis points). The classic S-curve shape
emerges: prepayment accelerates sharply when the refi incentive exceeds ~75-100 bps.</p>

<div class="chart-container">
  <canvas id="chart_scurve_refi"></canvas>
</div>

<h3>2b. S-Curve by Penalty Status</h3>
<p>Loans past all prepayment restrictions show materially higher CPRs than those still in penalty periods,
especially in the money.</p>

<div class="chart-container">
  <canvas id="chart_scurve_penalty"></canvas>
</div>

<h3>2c. Model-Implied S-Curves</h3>
<p>XGBoost model predictions across the full refi incentive spectrum, holding other features at median.
Shows the model's learned relationship between refi incentive and prepayment, with and without penalty protection.</p>

<div class="chart-container">
  <canvas id="chart_model_scurve"></canvas>
</div>

<h3>2d. CPR by Other Dimensions</h3>
<div class="chart-row">
  <div class="chart-container"><canvas id="chart_age"></canvas></div>
  <div class="chart-container"><canvas id="chart_upb"></canvas></div>
</div>
<div class="chart-row">
  <div class="chart-container"><canvas id="chart_penpts"></canvas></div>
  <div class="chart-container"><canvas id="chart_state"></canvas></div>
</div>

<div class="insight">
  <strong>Patterns observed:</strong> Older loans (10yr+) prepay significantly faster.
  Larger loans show higher CPRs, suggesting more sophisticated borrowers with better refinance access.
  Higher penalty points dramatically suppress prepayment.
</div>

<!-- ── Model Performance ──────────────────────────────────── -->
<h2>3. Model Performance &amp; Feature Importance</h2>

<div class="methodology">
  <strong>Methodology:</strong>
  <ul>
    <li><strong>Model:</strong> XGBoost gradient-boosted classifier with moderate class weighting (3x)</li>
    <li><strong>Train/Test Split:</strong> Time-based &mdash; trained on periods 202404&ndash;202509
        ({S['train_size']:,} obs, {S['train_prepays']} prepays); tested on {', '.join(str(p) for p in S['test_periods'])}
        ({S['test_size']:,} obs, {S['test_prepays']} prepays)</li>
    <li><strong>Features:</strong> Refi incentive, penalty points, loan age, remaining term, coupon rate,
        loan size (log UPB), unit count, penalty status, state, pool type, green/affordable flags</li>
    <li><strong>Test AUC:</strong> {S['auc_test']:.3f} &middot; <strong>Test Brier Score:</strong> {S['brier_test']:.4f}</li>
  </ul>
</div>

<div class="chart-row">
  <div class="chart-container"><canvas id="chart_feat_imp"></canvas></div>
  <div class="chart-container"><canvas id="chart_shap_imp"></canvas></div>
</div>

<div class="insight">
  <strong>Top predictors (SHAP):</strong> Remaining term and loan age are the strongest predictors &mdash; loans
  nearing maturity or with long seasoning prepay much more often. Loan size (log UPB) and unit count
  capture borrower sophistication. Refi incentive and penalty points drive the economic motivation layer.
</div>

<!-- ── Sample Loan Attribution ────────────────────────────── -->
<h2>4. Sample Loan Attribution</h2>

<p>For each sample loan below, SHAP values decompose the model's predicted prepayment probability
relative to the population baseline (SMM = {S['baseline_smm']*100:.2f}%, CPR = {S['baseline_cpr']*100:.1f}%).
Red bars indicate factors that <em>increase</em> prepayment risk; green bars indicate factors that <em>decrease</em> it.</p>

{loan_cards}

<div class="insight">
  <strong>Attribution interpretation:</strong> The SHAP waterfall starts from the model's base prediction
  (log-odds = {R['sample_loans'][0]['base_shap']:.3f}). Each factor's SHAP value shifts the prediction
  up or down. The sum of all SHAP values plus the base equals the final log-odds prediction, which is
  then converted to probability via the logistic function.
</div>

<!-- ── Conclusions ─────────────────────────────────────────── -->
<h2>5. Key Findings &amp; Conclusions</h2>

<div class="methodology">
<ol style="margin-left:1.25rem">
  <li><strong>Refi incentive is a key economic driver</strong> but the S-curve is flatter than in single-family
      MBS due to commercial borrower heterogeneity, prepayment penalties, and defeasance costs.</li>
  <li><strong>Penalty protection is highly effective:</strong> Loans with active prepayment penalties show
      near-zero CPRs regardless of refi incentive. Once restrictions expire, CPR jumps significantly.</li>
  <li><strong>Seasoning matters enormously:</strong> Remaining term and loan age are the top two predictors.
      Loans within 5 years of maturity have elevated prepay rates (balloon risk, refinance timing).</li>
  <li><strong>Size effect:</strong> Larger loans (higher UPB, more units) prepay faster, likely because
      larger borrowers have more refinancing options and greater financial sophistication.</li>
  <li><strong>Geography and program type:</strong> Certain states (IN, IL, MN) and FHA programs show
      distinct prepayment profiles, likely driven by local market conditions and program-specific rules.</li>
  <li><strong>Model limitations:</strong> With only 902 voluntary prepayments across 352K eligible observations,
      the event rate is low (0.26% SMM). The model achieves AUC 0.72 on out-of-time test data, indicating
      meaningful predictive power but room for improvement with more history or exogenous rate data.</li>
</ol>
</div>

<div class="footer">
  Generated from GNMA multifamily loan data &middot; 24 monthly periods (Apr 2024 &ndash; Mar 2026)<br>
  Analysis uses XGBoost + SHAP for interpretable ML-based prepayment modeling
</div>

</div>

<script>
// ── Chart configurations ────────────────────────────────────

const COLORS = {{
  blue: '#2563eb', red: '#dc2626', green: '#16a34a', amber: '#f59e0b',
  purple: '#7c3aed', teal: '#0d9488'
}};

// 2a. Refi S-curve (empirical)
new Chart(document.getElementById('chart_scurve_refi'), {{
  type: 'line',
  data: {{
    labels: {emp_labels},
    datasets: [{{
      label: 'Empirical CPR (%)',
      data: {emp_cprs},
      borderColor: COLORS.blue,
      backgroundColor: 'rgba(37,99,235,0.08)',
      fill: true, tension: 0.3, pointRadius: 4, pointHoverRadius: 6
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{
      title: {{ display: true, text: 'Prepayment S-Curve: CPR vs Refi Incentive (bps)', font: {{ size: 14 }} }},
      tooltip: {{
        callbacks: {{
          afterBody: function(ctx) {{
            const ns = {emp_ns};
            return 'n = ' + ns[ctx[0].dataIndex].toLocaleString();
          }}
        }}
      }}
    }},
    scales: {{
      x: {{ title: {{ display: true, text: 'Refi Incentive (bps)' }} }},
      y: {{ title: {{ display: true, text: 'CPR (%)' }}, min: 0 }}
    }}
  }}
}});

// 2b. Penalty status S-curves
new Chart(document.getElementById('chart_scurve_penalty'), {{
  type: 'scatter',
  data: {{
    datasets: [{scurve_penalty_datasets()}]
  }},
  options: {{
    responsive: true,
    showLine: true,
    plugins: {{
      title: {{ display: true, text: 'CPR by Refi Incentive & Penalty Status', font: {{ size: 14 }} }}
    }},
    scales: {{
      x: {{ title: {{ display: true, text: 'Refi Incentive (bps)' }} }},
      y: {{ title: {{ display: true, text: 'CPR (%)' }}, min: 0 }}
    }}
  }}
}});

// 2c. Model-implied S-curves
new Chart(document.getElementById('chart_model_scurve'), {{
  type: 'line',
  data: {model_scurve_data()},
  options: {{
    responsive: true,
    plugins: {{
      title: {{ display: true, text: 'Model-Implied CPR vs Refi Incentive (Median Loan Profile)', font: {{ size: 14 }} }}
    }},
    scales: {{
      x: {{ title: {{ display: true, text: 'Refi Incentive (bps)' }} }},
      y: {{ title: {{ display: true, text: 'CPR (%)' }}, min: 0 }}
    }}
  }}
}});

// 2d. Bar charts
new Chart(document.getElementById('chart_age'), {{
  type: 'bar',
  data: {bar_chart_data(R['scurve_age'], 'bucket')},
  options: {{
    responsive: true,
    plugins: {{ title: {{ display: true, text: 'CPR by Loan Age', font: {{ size: 14 }} }}, legend: {{ display: false }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'CPR (%)' }}, min: 0 }} }}
  }}
}});

new Chart(document.getElementById('chart_upb'), {{
  type: 'bar',
  data: {bar_chart_data(R['scurve_upb'], 'bucket')},
  options: {{
    responsive: true,
    plugins: {{ title: {{ display: true, text: 'CPR by Loan Size (UPB)', font: {{ size: 14 }} }}, legend: {{ display: false }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'CPR (%)' }}, min: 0 }} }}
  }}
}});

new Chart(document.getElementById('chart_penpts'), {{
  type: 'bar',
  data: {bar_chart_data(R['scurve_penpts'], 'bucket')},
  options: {{
    responsive: true,
    plugins: {{ title: {{ display: true, text: 'CPR by Penalty Points', font: {{ size: 14 }} }}, legend: {{ display: false }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'CPR (%)' }}, min: 0 }} }}
  }}
}});

new Chart(document.getElementById('chart_state'), {{
  type: 'bar',
  data: {bar_chart_data(R['scurve_state'], 'state')},
  options: {{
    responsive: true, indexAxis: 'y',
    plugins: {{ title: {{ display: true, text: 'CPR by State', font: {{ size: 14 }} }}, legend: {{ display: false }} }},
    scales: {{ x: {{ title: {{ display: true, text: 'CPR (%)' }}, min: 0 }} }}
  }}
}});

// 3. Feature importance
new Chart(document.getElementById('chart_feat_imp'), {{
  type: 'bar',
  data: {feat_imp_data()},
  options: {{
    responsive: true, indexAxis: 'y',
    plugins: {{ title: {{ display: true, text: 'XGBoost Feature Importance (Gain)', font: {{ size: 14 }} }}, legend: {{ display: false }} }},
    scales: {{ x: {{ min: 0 }} }}
  }}
}});

new Chart(document.getElementById('chart_shap_imp'), {{
  type: 'bar',
  data: {shap_imp_data()},
  options: {{
    responsive: true, indexAxis: 'y',
    plugins: {{ title: {{ display: true, text: 'SHAP Feature Importance (Mean |SHAP|)', font: {{ size: 14 }} }}, legend: {{ display: false }} }},
    scales: {{ x: {{ min: 0 }} }}
  }}
}});

</script>
</body>
</html>"""

with open('prepayment_report.html', 'w') as f:
    f.write(html)

print(f"Report written to prepayment_report.html ({len(html):,} bytes)")
