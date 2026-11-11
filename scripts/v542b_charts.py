"""V542b charts — English labels to avoid Arabic glyph issues."""
import json
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

plt.rcParams['font.sans-serif'] = ['DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

OUT = Path('/home/z/my-project/download/v542_simulation')
r = json.load(open(OUT / 'results_v542b.json'))

# Equity curves chart
fig, axes = plt.subplots(3, 1, figsize=(11, 12), constrained_layout=True)
regimes = ['harsh', 'base', 'normal']
regime_labels = {
    'harsh':  'HARSH market (1-2 candidates/day, 73% fail_day1, 17% rug)',
    'base':   'BASE market  (2-4 candidates/day, 69% fail_day1, 15% rug)',
    'normal': 'NORMAL market (4-8 candidates/day, 65% fail_day1, 12% rug)',
}

for ax, regime in zip(axes, regimes):
    curves = np.load(OUT / f'curves_{regime}_v542b.npy')
    if curves.ndim != 2:
        curves = np.array([c for c in curves])
    n_seeds, n_pts = curves.shape
    days = np.arange(n_pts)
    for s in range(n_seeds):
        ax.plot(days, curves[s], color='steelblue', alpha=0.15, linewidth=0.8)
    med = np.median(curves, axis=0)
    p5 = np.percentile(curves, 5, axis=0)
    p25 = np.percentile(curves, 25, axis=0)
    p75 = np.percentile(curves, 75, axis=0)
    p95 = np.percentile(curves, 95, axis=0)
    ax.fill_between(days, p5, p95, color='steelblue', alpha=0.15, label='P5-P95 band')
    ax.fill_between(days, p25, p75, color='steelblue', alpha=0.30, label='P25-P75 band')
    ax.plot(days, med, color='crimson', linewidth=2.2, label='Median')
    ax.axhline(20, color='green', linestyle='--', alpha=0.6, linewidth=1.2, label='$20 starting capital')
    s = r['summary_by_regime'][regime]
    title = (f"{regime_labels[regime]}\n"
             f"  Median ROI: {s['roi_median_pct']:+.1f}%   |   "
             f"P(profit): {s['prob_profit_pct']:.0f}%   |   "
             f"Avg trades: {s['avg_trades']:.0f}   |   "
             f"Max DD: {s['max_drawdown_pct_median']:.0f}%   |   "
             f"Avg WR: {s['avg_win_rate_pct']:.1f}%")
    ax.set_title(title, fontsize=10, fontweight='bold')
    ax.set_xlabel('Day')
    ax.set_ylabel('Capital ($)')
    ax.legend(loc='upper right', fontsize=8)
    ax.grid(alpha=0.3)
    ax.set_ylim(8, 35)

fig.suptitle('V542b SIMULATION: V541 unified strategy on Pump.fun (30 seeds x 30 days x $20)',
             fontsize=12, fontweight='bold')
plt.savefig(OUT / 'equity_curves_v542b.png', dpi=120)
print(f"Equity chart: {OUT / 'equity_curves_v542b.png'}")

# Summary bar chart
fig2, ax = plt.subplots(1, 1, figsize=(10, 6), constrained_layout=True)
labels = ['HARSH', 'BASE', 'NORMAL']
medians = [r['summary_by_regime'][rg]['final_median'] for rg in regimes]
p5s = [r['summary_by_regime'][rg]['final_p5'] for rg in regimes]
p95s = [r['summary_by_regime'][rg]['final_p95'] for rg in regimes]
means = [r['summary_by_regime'][rg]['final_mean'] for rg in regimes]
x = np.arange(len(labels))
w = 0.22
b1 = ax.bar(x - w, p5s, w, label='P5 (worst 5%)', color='crimson', alpha=0.7)
b2 = ax.bar(x, medians, w, label='Median', color='steelblue', alpha=0.9)
b3 = ax.bar(x + w, p95s, w, label='P95 (best 5%)', color='forestgreen', alpha=0.7)
ax.axhline(20, color='black', linestyle='--', linewidth=1.2, label='$20 starting capital')
ax.set_xticks(x)
ax.set_xticklabels(labels)
ax.set_ylabel('Final capital after 30 days ($)')
ax.set_title('V541 unified strategy LOSES in ALL 3 regimes (90 seeds total)',
             fontsize=12, fontweight='bold')
ax.legend()
ax.grid(alpha=0.3, axis='y')
for i, v in enumerate(medians):
    ax.text(i, v + 0.4, f'${v:.2f}', ha='center', fontweight='bold', fontsize=11)
plt.savefig(OUT / 'summary_bars_v542b.png', dpi=120)
print(f"Summary chart: {OUT / 'summary_bars_v542b.png'}")
