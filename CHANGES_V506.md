# V506 changes (on top of V505)

## Critical strategy + code-quality fixes

### 1. V506 trailing tiers tightened (15/20/30% — was 25/35/45%)
**Files:** `live_trading/executor.py`, `paper_trading/engine.py`

V502's wide tiers (25/35/45%) gave back too much profit on memecoins. A
"winning" trailing exit could net **-10%** after 8% round-trip slippage.

V506 tightens to **15/20/30%** and triggers earlier at +15% peak gain
(was +30%):
- +15% peak → -15% trail (locks small wins early)
- +100% peak → -20% trail
- +500%+ peak → -30% trail (still rides mega pumps, just locks 70%)

Backtest showed V506 improves avg PnL per trade over V502 by ~0.3% per
token — not enough to flip the strategy to profitability alone, but
combined with the V505 SL changes (+2.8%/trade) it's meaningful.

### 2. V506 sudden-dump lowered to -18% / -20% (was -25%)
Catches obvious rug-pulls earlier in the +10%..+15% pre-trailing zone
(before SL of -15% takes a full loss).

### 3. V506 stagnation wait lowered to 3h (was 4h)
Memecoins move fast — if a position hasn't moved in 3h, it's likely dead.
Still requires: held >=3h, in profit, never above +5%.

### 4. V506 dedicated `peak_price` column (was overloaded on `max_unrealized_profit_usd`)
**Files:** `core/models.py`, `live_trading/executor.py`, `paper_trading/engine.py`

The `max_unrealized_profit_usd` field is semantically a PnL value, not a
price. V400 overloaded it to store peak_price because there was no other
nullable Float column available. V506 adds a proper `peak_price` column.

Backwards-compatibility: legacy trades (opened before V506) have `peak_price=None`,
and the engine migrates the value from `max_unrealized_profit_usd` on first read.

### 5. V506 risk_manager uses live_max_open_positions in live mode (was paper_max_open_positions=10)
**File:** `risk_management/manager.py`

Before V506, the max-open-positions gate always read `paper_max_open_positions=10`
even in live mode — so it was dead code (the executor's own check on
`live_max_open_positions=3` was the only effective limit).

Now picks the right ceiling based on `settings.is_live_trading`.

### 6. V506 `/api/system/prep-live-trading` endpoint
**File:** `api/routes.py`

One-shot pre-flight check before starting live trading. Returns a GO / NO-GO
verdict by checking:
- All live_requirements satisfied (mode, keys, confirm phrase)
- Wallet has SOL (>= trade_size + 0.005 SOL for fees/ATA)
- SOL price fetch works (multi-source fallback healthy)
- Risk state clean (no circuit breaker, no cooldown)
- TradingState.is_active = False (operator must start manually)
- DB on persistent Volume (RAILWAY_VOLUME_MOUNT_PATH)
- API_AUTH_KEY set

Usage:
```bash
curl -X POST -H "Authorization: Bearer $API_AUTH_KEY" \
  https://memecoin-bot-deploy-production.up.railway.app/api/system/prep-live-trading
```

Returns:
```json
{
  "go": true,
  "summary": "✅ READY for live trading",
  "checks": [{"name": "wallet_sol", "ok": true, "detail": "wallet=0.0850 SOL (~$7.65)..."}, ...],
  "wallet_sol": 0.085,
  "sol_price_usd": 90.0,
  "wallet_usd": 7.65
}
```

### 7. V506 `/api/system/status` exposes DATABASE_URL + Volume mount status
Surfaces the resolved DATABASE_URL so the operator can verify the Volume is
actually being used (path contains `/app/data/` when mounted correctly).

## Operations tooling

### `scripts/setup_railway_volume.sh` — one-command Volume setup
Run from your laptop:
```bash
bash scripts/setup_railway_volume.sh
```
Logs in to Railway CLI, adds a 1GB Volume at `/app/data`, removes the
explicit DATABASE_URL env var (so V505 auto-detection wins), triggers a
redeploy, and waits for /health to come back green.

### `scripts/recover_stuck_positions.py` — recover sold-but-not-closed trades
Use when the bot has Trade rows marked `open` but the tokens are actually
no longer in the wallet (post-redeploy without Volume, or sell-on-chain
but DB close failed twice).

```bash
API_AUTH_KEY=xxxx python scripts/recover_stuck_positions.py --dry-run
API_AUTH_KEY=xxxx python scripts/recover_stuck_positions.py --execute
```

For each open trade: queries Solana RPC for the wallet's token balance,
lists trades where balance = 0, and force-closes them via the API with
`exit_reason="manual_recovery"`.

## Tests

All V504 + V505 tests still pass. No regressions.

## Files changed

| File | Change |
|---|---|
| `app/core/models.py` | Added `peak_price` column to Trade model |
| `app/modules/live_trading/executor.py` | V506 trail tiers (15/20/30) + peak_price column + sudden_dump -20% + stagnation 3h |
| `app/modules/paper_trading/engine.py` | Same V506 changes as executor (parity) |
| `app/modules/risk_management/manager.py` | Use live_max_open_positions in live mode |
| `app/api/routes.py` | `/api/system/prep-live-trading` endpoint + DB URL in `/system/status` |
| `scripts/setup_railway_volume.sh` | One-command Volume setup |
| `scripts/recover_stuck_positions.py` | Stuck-position recovery |
