# Real-trade test setup (V505) — Stage 9 of the audit prompt

This document explains how to safely execute the first real buy + sell
cycle on Pump.fun to verify the V504+V505 fixes work on-chain.

**DO NOT execute Stage 9 without explicit user approval.** The bot is
currently in `live` mode on Railway, but `TradingState.is_active` defaults
to `False` (V400 fix), so it is NOT opening trades automatically.

---

## 1. Pre-flight checklist (must be ✅ before doing anything)

| # | Check | How to verify | Status |
|---|---|---|---|
| 1 | Railway Volume mounted | `RAILWAY_VOLUME_MOUNT_PATH` env var present in Railway | ☐ |
| 2 | SQLite persists across redeploys | Open paper trade, redeploy, verify it's still there | ☐ |
| 3 | Wallet funded with 0.05–0.1 SOL test amount | Check on Solscan for `SOLANA_WALLET_ADDRESS` | ☐ |
| 4 | Wallet is NEW (not your main) | Wallet has no other SPL tokens / SOL | ☐ |
| 5 | V505 code deployed | `git log -1` shows V505 commit on Railway | ☐ |
| 6 | `/health` returns ok | curl the URL | ☐ |
| 7 | Dashboard loads + accepts API key | Open the dashboard URL in browser | ☐ |
| 8 | `TradingState.is_active` is False | Dashboard shows "stopped" / "متوقف" | ☐ |

If any of these are ✗, do NOT proceed to Stage 9.

---

## 2. Set these env vars in Railway (Settings → Variables)

For the test phase (one trade at a time, $1-2 max loss):

```bash
TRADING_MODE=live
LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISK
PUMPPORTAL_API_KEY=<your-key>
SOLANA_WALLET_ADDRESS=<your-wallet>
SOLANA_PRIVATE_KEY=<your-key>
API_AUTH_KEY=<strong-random-key>

# Conservative limits for the test
PUMP_MAX_TRADE_SOL=0.01           # max 0.01 SOL per trade (~$1.50)
LIVE_MAX_OPEN_POSITIONS=1         # only 1 position at a time
LIVE_DAILY_CAP_SOL=0.03           # max 0.03 SOL spent per day (~$4.50)
LIVE_DAILY_LOSS_LIMIT_SOL=0.02    # max 0.02 SOL loss per day (~$3)
LIVE_DAILY_LOSS_LIMIT_PCT_OF_WALLET=15.0  # or 15% of wallet, whichever is lower

ENABLE_ORCHESTRATOR=true
AUTO_START_TRADING=false           # IMPORTANT: keep false — you start manually
```

After setting, **redeploy** Railway (Settings → Restart, or push a commit).

---

## 3. Verify V505 fixes are active on the live bot

Once redeployed, hit these endpoints (with `Authorization: Bearer <API_AUTH_KEY>`):

```bash
# Health — should show live_trading=true
curl https://memecoin-bot-deploy-production.up.railway.app/health

# Wallet balance — V505 should return a real sol_price_usd (not 200.0)
curl -H "Authorization: Bearer $API_AUTH_KEY" \
  https://memecoin-bot-deploy-production.up.railway.app/api/pumpfun/wallet/balance
# Expected: {"sol_price_usd": 90.xx, "wallet_sol": 0.05-0.1, ...}

# Live status — V505 circuit-breaker + cooldown gates
curl -H "Authorization: Bearer $API_AUTH_KEY" \
  https://memecoin-bot-deploy-production.up.railway.app/api/live/status
# Expected: configured=true, can_open=true, max_trade_sol=0.01

# Risk state — V504 capital-base-aware fields
curl -H "Authorization: Bearer $API_AUTH_KEY" \
  https://memecoin-bot-deploy-production.up.railway.app/api/risk/state
# Expected: daily_pnl_pct=0.0, circuit_breaker_active=false, can_open=true
```

---

## 4. The first real buy (manual, $0.005-0.01 SOL)

Use the dashboard "Manual Trade" panel (or POST `/api/pumpfun/trade/buy`):

```bash
# Buy 0.005 SOL of a liquid token (e.g. BONK, WIF, POPCAT)
# Pick a token with liquidity > $50k and 24h volume > $100k for the test
curl -X POST \
  -H "Authorization: Bearer $API_AUTH_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "mint": "<TOKEN_MINT>",
    "amount_sol": 0.005,
    "pool": "auto"
  }' \
  https://memecoin-bot-deploy-production.up.railway.app/api/pumpfun/trade/buy
```

Expected response (V505 with real slippage measurement):
```json
{
  "success": true,
  "trade_id": "uuid-here",
  "tx_signature": "5xY...123",
  "symbol": "BONK",
  "amount_sol": 0.005,
  "fill_price": 0.000012345,     // V505: actual fill from wallet diff
  "notional_usd": 0.75,
  "wallet_sol_before": 0.0950
}
```

### What to verify after the buy:
1. **On Solscan** (`https://solscan.io/tx/<tx_signature>`):
   - Transaction status = Success
   - Token balance of the wallet increased by the expected amount
   - SOL balance decreased by ~0.005 + fees (~0.006 total)
2. **In the DB** (`/api/trades`):
   - Trade row exists with `status=open`, `entry_price=fill_price`,
     `stop_loss_price` (V505: -15% from entry), `take_profit_price` (11x entry),
     `planned_holding_period_minutes=7200` (5 days).
   - `entry_slippage_usd` is the V505 measured value (not the 3% estimate).
   - `entry_reasons_json` includes "V505 measured slippage X%".
3. **In the V505 logs** (Railway → Deploy Logs):
   - `📐 V505 real buy slippage: +X.XX% (fill=$..., market=$..., sol_spent=..., tokens=...)`

If the buy failed with `buy_tx_failed_onchain`:
- V503 fix correctly prevented a phantom position from being created.
- No Trade row should exist in the DB.
- The SOL didn't move (verify on Solscan).

---

## 5. The first real sell (manual, full position)

```bash
# Sell 100% of the position
curl -X POST \
  -H "Authorization: Bearer $API_AUTH_KEY" \
  -H "Content-Type: application/json" \
  -d '{"trade_id": "<trade-id-from-step-4>"}' \
  https://memecoin-bot-deploy-production.up.railway.app/api/trades/<trade_id>/close
```

Expected response (V505 with real exit-slippage measurement):
```json
{
  "success": true,
  "trade_id": "uuid-here",
  "tx_signature": "5xY...456",
  "exit_price": 0.000012000,  // V505: actual exit price from wallet diff
  "net_pnl_usd": -0.0003,    // small loss (entry+exit slippage + fees)
  "pnl_pct": -3.5
}
```

### What to verify after the sell:
1. **On Solscan** (`https://solscan.io/tx/<tx_signature>`):
   - Transaction status = Success
   - Token balance = 0
   - SOL balance increased by ~0.004 (less fees)
2. **In the DB** (`/api/trades`):
   - Trade row has `status=closed`, `exit_price=actual_exit_price`,
     `net_pnl_usd` (should be small negative — entry+exit fees + slippage),
     `pnl_pct`, `exit_reason="manual"`.
3. **In the V505 logs**:
   - `📐 V505 real sell slippage: +X.XX% (exit=$..., market=$..., tokens_sold=..., sol_received=...)`
4. **In the RiskManager state** (`/api/risk/state`):
   - `consecutive_losses` should now be 1 (if pnl was negative).
   - `last_trade_at` should be set.

---

## 6. Test the auto-exit path (one trade, let it run)

After the manual test passes:

1. Open a new trade manually (same as Step 4).
2. **Don't close it manually** — let the orchestrator manage it.
3. Watch the dashboard's "Trades" page every 5 minutes.
4. Within 1-2 cycles (10-20s), the fast-monitor should:
   - Update `peak_price` (V400 fix) when price moves up.
   - Possibly trigger an exit if any V500 Smart Exit condition hits:
     - `stop_loss` (V505: -15% from entry)
     - `sudden_dump` (-25% from peak, only before trailing armed)
     - `smart_trailing` (-25% / -35% / -45% from peak, tiered by peak_gain)
     - `mega_tp` (price × 11)
     - `stagnation` (4h+ held, never went above +5%)
     - `time_exit` (5 days held)
     - `circuit_breaker` (if circuit breaker activates)

5. When the trade closes, verify the `exit_reason` matches what you observed.

---

## 7. Test "Exit All" button

1. Open 1 trade manually.
2. Click "🚪 إغلاق الكل" on the Trades page.
3. Confirm in the dialog.
4. Verify all open trades close with `exit_reason="exit_all"`.

---

## 8. After the test — revert to ultra-safe limits

Once the test cycle completes successfully:

```bash
# In Railway Variables, set these to make auto-trading safe (still requires manual Start):
PUMP_MAX_TRADE_SOL=0.005           # even smaller per-trade cap
LIVE_MAX_OPEN_POSITIONS=1
LIVE_DAILY_CAP_SOL=0.02
LIVE_DAILY_LOSS_LIMIT_SOL=0.015
LIVE_DAILY_LOSS_LIMIT_PCT_OF_WALLET=15.0
```

Now you can safely click "ابدأ" on the dashboard and let the bot run on
**one position at a time** with a hard daily-loss ceiling.

---

## 9. If anything goes wrong

- **Emergency stop**: Click "إيقاف" on the dashboard (sets `TradingState.is_active=False`).
- **Hard kill**: Railway → Settings → Restart (the orchestrator stops on shutdown).
- **Recover stuck tokens** (sell failed but DB close also failed):
  - V503 `_sold_pending_db` set prevents auto-retry.
  - But if the process restarts, the set is wiped and the bot WILL try to sell again.
  - Manual recovery: go to the dashboard, click "إغلاق" on the stuck trade.
  - If the token was already sold (on-chain) but DB says open, force-close via
    direct DB edit or just leave it (the bot will re-attempt sell, get a "nothing
    to sell" error from PumpPortal, and the trade will remain open — annoying
    but not catastrophic).

---

## 10. Hard limits — DO NOT change without explicit user approval

These come from the original prompt:

- `PUMP_MAX_TRADE_SOL` — DO NOT raise above 0.01 during the test phase.
- `LIVE_DAILY_CAP_SOL` — DO NOT raise above 0.03 during the test phase.
- `LIVE_DAILY_LOSS_LIMIT_SOL` — DO NOT raise above 0.02 during the test phase.
- `LIVE_MAX_OPEN_POSITIONS` — DO NOT raise above 1 during the test phase.

The user must explicitly approve any increase.
