# V504 changes (on top of V503)

## Critical bugs fixed

### 1. SOL price fallback was a $200 constant — no real price floor
**Files:** `app/modules/market_scanner/pumpfun_client.py`, `app/modules/live_trading/executor.py`

Before V504, `_fetch_sol_price()` was initialised to `SOL_PRICE_USD_FALLBACK = 200.0` and never went `None` — if every price source failed it silently returned `$200`. This caused:

* `executor.open_position()` computed `notional_usd = amount_sol * sol_price_usd` with a wrong price (a $20 wallet would look like a $200 trade).
* `executor.close_position()` updated the SOL-denominated `daily["sol_pnl"]` with the wrong exchange rate, corrupting the daily-loss counter.
* The orchestrator's `if not sol_price_usd or sol_price_usd <= 0` skip guard (V407 P1-5) was dead code — it could never fire.

V504 fix:
* `_fetch_sol_price()` now returns `Optional[float]` — `None` means "no real price available".
* Four sources tried in order: **Jupiter → Binance → Coinbase → DexScreener** (each with 5s timeout).
* A stale cache is reused only if it's < 5 minutes old; older stale values are dropped.
* `executor.open_position()` rejects the trade with code `sol_price_unavailable` when SOL price is `None` (the safe choice — never size a position with a fake price).
* `executor.close_position()` still closes (it may be hitting stop-loss / circuit breaker — urgent liquidation) but logs a warning and skips the SOL-denominated `sol_pnl` tracking this cycle.
* `_parse_token()` keeps using `SOL_PRICE_USD_FALLBACK` for the non-critical market-cap display only.

Verified live: the test cycle showed `sol_price_usd=$89.96` returned by `/api/pumpfun/wallet/balance` (real Jupiter price, not the $200 fallback).

### 2. RiskManager %-based gates used paper_capital_usd ($10000) even for live wallets
**Files:** `app/modules/risk_management/manager.py`, `app/modules/live_trading/executor.py`

Before V504, every %-based gate (`daily_pnl_pct`, `weekly_pnl_pct`, `max_drawdown_pct`, `peak_equity`) divided by `settings.paper_capital_usd` (default `$10000`) regardless of trading mode. For a `$20` live wallet, a `$5` loss showed as `0.05%` (instead of `25%`), so the 5% daily-loss limit could NEVER trip on a live wallet — the bot could lose everything without the circuit breaker firing.

V504 fix:
* New `RiskManager._get_capital_base_usd()` reads `live_capital_base` from `SystemState` (set by the executor on the first trade of each UTC day). Falls back to `paper_capital_usd` when no live row exists (correct for paper trading).
* `get_state()` and `record_trade_result()` use `capital_base_usd` instead of `settings.paper_capital_usd` for all percent calcs.
* New `LiveTradingExecutor._record_live_capital_base(wallet_sol, sol_price_usd)` writes the row once per UTC day (and refreshes when wallet USD drifts > 20% — covers deposits / withdrawals / large PnL swings).
* Called from `open_position()` right after the wallet-balance check, before the buy.

Verified: with a simulated `$20` live wallet and a `-$5.50` closed trade, `daily_pnl_pct` now reads `-27.5%` (was `-0.055%`), and the 5% daily-loss circuit breaker correctly trips.

### 3. Pre-existing AsyncSession.merge() bug — token scores never persisted
**File:** `app/jobs/orchestrator.py`

`async with get_session() as s: s.merge(token)` at line 352 was missing `await`. `AsyncSession.merge()` returns a coroutine; without `await` the latest_opportunity_score / latest_risk_score / latest_win_probability were silently never persisted to the DB. Fixed: `await s.merge(token)`.

## Frontend
* Rebuilt `frontend/dist/` with `npm run build` (V503 shipped only source — Railway does not build the frontend).
* The new TokensPage 24h-change cell (Arabic: "التغير 24h") is now in the compiled bundle (`index-BhlT6URF.js`, 788 KB / 228 KB gzipped).

## Tests added
* `backend/tests/test_v504_fixes.py` — unit tests for SOL-price multi-source + capital base + executor recording.
* `backend/tests/test_v504_endpoints.py` — 12 endpoint smoke tests (health, risk state, live status, wallet balance, exit-all, agents, trading start/stop).
* `backend/tests/test_v504_cycle.py` — full orchestrator cycle (scan → AI consensus → paper trade open) with stubbed scanner.

All three test suites pass. The one pre-existing failure in `test_basic.py::test_config_loads` is unrelated to V504 (stale assertion that `min_win_probability == 0.70`, but V31.2 intentionally lowered it to `0.55`).

## Not changed (out of scope)
* The executor's `_sold_pending_db` set is in-memory only; if the process restarts after a sell-on-chain-but-DB-failed event, the auto-exit will try to sell again. Mitigation exists (the trade stays `open` and the operator can recover manually), but a persistent flag would be cleaner. Deferred to V505.
* Paper trading's `manage_open_positions` exit logic uses `token.price_usd` from the DB (refreshed by scanner); a fast price feed (Jupiter/DexScreener poll) for live SL/TP would close positions faster. Deferred.
