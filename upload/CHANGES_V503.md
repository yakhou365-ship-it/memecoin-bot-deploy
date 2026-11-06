# V502 + V503 changes

## Security
- main.py: API key no longer embedded in public HTML (prompted once, kept in localStorage; open /#resetkey to clear).

## Critical bugs fixed
- scanner.py: Solana mint addresses were lowercased -> invalid mints for PumpPortal buy/sell. Now case-exact (only 0x addresses lowercased).
- executor.py: confirm_signature was called with `timeout=` (TypeError swallowed) and result treated as bool -> on-chain confirmation never ran. Fixed; failed txs no longer create phantom positions / false closes.
- executor.py: daily loss limit now min(0.3 SOL, 30% of start-of-day wallet) (new env LIVE_DAILY_LOSS_LIMIT_PCT_OF_WALLET) and activates the circuit breaker.
- risk manager: cooldown after N losses now enforced in live mode; streak reset when cooldown arms (no permanent lock-out). Fails closed if risk state unreadable.

## Strategy logic
- Exit: 25/35/45% trailing tiers reachable; sudden-dump only before trailing armed; stagnation fixed; circuit breaker liquidates positions (live + paper).
- orchestrator.py: AI override cannot bypass liquidity/volume/risk gates; fallback TP 11x, hold 7200 min.
- scanner now uses full discovery (20 terms + boosted + Gecko) with 60s cache.

## Not done / needs you
- Frontend not rebuilt (no network): run `npm run build` in frontend/ and commit dist/ (Railway does not build the frontend). TokensPage now has a 24h-change cell in source only.
- RiskManager %-based drawdown/weekly limits still use paper_capital_usd (10000).
- SOL price fetch falls back to a constant instead of skipping the trade.
