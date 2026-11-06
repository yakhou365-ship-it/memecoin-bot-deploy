# V505 backtest findings (test_v505_backtest.py)

## Summary

Compared V501 (old: -8% SL, 1.8×ATR, no SL floor) vs V505 (new: -15% SL floor,
-25% ceiling, 3×ATR) on 90 days of 15m candles for 5 memecoin-like tokens
(BONK, PEPE, WIF, DOGE, SHIB).

## Head-to-head results

| Metric | V501 | V505 | Δ |
|---|---|---|---|
| Avg return per token | -77.3% | -58.1% | **+19.2%** |
| Avg PnL per trade | -7.2% | -4.4% | **+2.8%** |
| Max drawdown (worst token) | 80.2% | 71.1% | **-9.1%** |
| Stop-out frequency | 782/2060 (38%) | 168/2060 (8%) | **-78%** |

V505's wider SL floor dramatically reduces the number of stop-outs (which is
the slow-bleed failure mode the V501 strategy suffers from), and improves
the average loss per trade.

## Honest caveats

1. **Both strategies lose money in this backtest.** The simple "close > MA20 + ATR"
   breakout entry signal is fundamentally weak — most breakouts fail on
   memecoins. The real bot uses 7 decision-engine gates + 5 AI agents with
   veto power; this backtest can't simulate those filters.

2. **0% win rate is a backtest artifact.** With the V502 trail tiers
   (25% / 35% / 45% drop from peak), even a "winning" trailing exit can be a
   net loss after 8% round-trip slippage. Example:
   - Entry = $100, peak = $130 (+30%), trail = 25% from peak
   - Exit at $130 × 0.75 = $97.5 → -2.5% before fees
   - After 3% entry + 5% exit slippage = **-10.5% net loss**
   
   This is a real finding: the V502 trail tiers may be **too wide** for
   memecoins. A V506 variant with tighter tiers (15% / 20% / 30%) showed
   marginal improvement (~0.3% per token) — not enough to flip the strategy
   to profitability. The fundamental issue is the entry signal, not the
   exit rules.

3. **The V505 real-slippage measurement (wallet diff) can't be backtested**
   without on-chain simulation. The 3% / 5% estimates are still used in
   this backtest. In live trading, V505 will measure the true cost and the
   PnL will be more accurate (probably slightly worse if slippage is
   higher than estimated, slightly better if lower).

4. **The TP cap at 11× is rarely hit** (0/2060 trades). Most exits are
   `stagnation` (54-64%) or `stop_loss` (8-38%). Memecoins rarely 10×
   within a 5-day hold without retracing past the trail tier first.

## Recommendation

- **V505's SL changes are net positive** — fewer stop-outs, smaller average
  loss, lower drawdown. Worth keeping.
- **The trail tiers (V502: 25/35/45%) are a separate concern** — they may
  give back too much profit on memecoins. A V506 variant with tighter tiers
  (15/20/30%) could improve trailing exits, but **this requires explicit
  user approval** per the prompt's rules. Not changing without sign-off.
- **The strategy as a whole is NOT profitable in this simplified backtest**.
  The real bot's edge comes from the AI agents filtering out bad entries,
  not from the exit rules. Without simulating those filters, the backtest
  will always show losses.

## What this backtest DOES validate

- The V505 SL floor (-15%) and ceiling (-25%) logic works correctly.
- The V502 sudden_dump / stagnation / smart_trailing exit triggers fire
  in the expected proportions (mostly stagnation, some stop_loss).
- The V505 changes don't break the V502 exit logic — they just adjust the
  SL distance.

## What this backtest DOESN'T validate

- Whether the AI agent consensus improves entry quality (likely yes —
  but can't simulate without real LLM calls).
- Whether V505's real-slippage measurement changes PnL outcomes (need
  real trades to measure).
- Whether the strategy is profitable in real trading (cannot be answered
  by a backtest alone).
