"""
V539 Ratchet verification — runs the REAL paper-engine exit path (manage_open_positions)
against controlled price scenarios to prove the new adaptive_pro exit system.

All price paths are MULTIPLIERS of the actual filled entry price (slippage-aware).

Run: cd /home/z/my-project/backend && python3 ../scripts/v539_ratchet_verify.py
"""
import asyncio
import os
import sys
from pathlib import Path

BACKEND_DIR = Path("/home/z/my-project/backend")
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/test_v539.db"
os.environ["TRADING_MODE"] = "paper"
os.environ["ENABLE_ORCHESTRATOR"] = "false"
os.environ["AUTO_START_TRADING"] = "false"
os.environ["API_AUTH_KEY"] = "test-key-123"
os.environ["TRADING_PROFILE"] = "adaptive_pro"

DB = BACKEND_DIR / "data" / "test_v539.db"
if DB.exists():
    DB.unlink()

PASSED, FAILED = 0, 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name} — {detail}")


async def make_token(session_mod, token_id, price=1.0):
    from app.core.models import Token
    async with session_mod.get_session() as s:
        t = Token(
            id=token_id, symbol=token_id[:8], name="V539 Test", address=f"mint_{token_id}",
            chain="solana", price_usd=price, liquidity_usd=200000.0,
            volume_24h_usd=1000000.0,
        )
        s.add(t)
        await s.commit()


async def set_price(session_mod, token_id, price):
    from app.core.models import Token
    async with session_mod.get_session() as s:
        t = await s.get(Token, token_id)
        t.price_usd = float(price)
        s.add(t)
        await s.commit()


async def run_scenario(engine, session_mod, name, mult_path, expect_reasons, lo, hi):
    """mult_path: multipliers of the actual entry price."""
    from app.core.models import Trade
    await make_token(session_mod, f"TK_{name}", price=1.0)
    trade = await engine.open_position(
        token_id=f"TK_{name}", entry_price=1.0, position_size_usd=100.0,
        liquidity_usd=200000.0, holding_period_minutes=120,
    )
    check(f"{name}: trade opened", trade is not None, f"open_position returned {trade}")
    if trade is None:
        return None
    entry = float(trade.entry_price)
    stop_loss = entry * 0.96
    take_profit = entry * 2.0
    async with session_mod.get_session() as s:
        db_tr = await s.get(Trade, trade.id)
        db_tr.stop_loss_price = stop_loss
        db_tr.take_profit_price = take_profit
        s.add(db_tr)
        await s.commit()

    reasons = []
    final_pnl_pct = None
    for m in mult_path:
        await set_price(session_mod, f"TK_{name}", entry * m)
        closed = await engine.manage_open_positions()
        for r, n in closed.items():
            if n:
                reasons.append(r)
        async with session_mod.get_session() as s:
            tr = await s.get(Trade, trade.id)
            if tr.status == "closed":
                final_pnl_pct = float(tr.pnl_pct or 0.0)
                break

    ok_reason = (not expect_reasons) or any(r in expect_reasons for r in reasons)
    check(f"{name}: exit reason ∈ {sorted(expect_reasons)}", ok_reason, f"got {reasons}")
    if lo is not None:
        pnl_txt = f"{final_pnl_pct:+.2f}%" if final_pnl_pct is not None else "N/A"
        check(
            f"{name}: pnl {pnl_txt} ∈ [{lo:+.1f}%, {hi:+.1f}%]",
            final_pnl_pct is not None and lo <= final_pnl_pct <= hi,
            f"pnl={final_pnl_pct}",
        )
    return final_pnl_pct


async def main():
    from app.core.database import init_db, close_db
    from app.modules.paper_trading import engine as engine_mod

    await init_db()
    engine = engine_mod.PaperTradingEngine()

    print("\n=== V539 RATCHET — real paper-engine exit path (profile=adaptive_pro) ===\n")

    # A) +10% peak then dump → lock +3% (exit ≈ entry×1.03)
    await run_scenario(
        engine, engine_mod, "A_lock3pct",
        mult_path=[1.09, 1.10, 1.06, 1.035, 1.025, 1.00, 0.98],
        expect_reasons={"smart_trailing", "sudden_dump"},
        lo=+0.5, hi=+4.0,
    )

    # B) +30% peak then dump → lock +15% (exit ≈ entry×1.15) — old code sold at +8%
    await run_scenario(
        engine, engine_mod, "B_lock15pct",
        mult_path=[1.20, 1.31, 1.25, 1.17, 1.15, 1.10, 1.00],
        expect_reasons={"smart_trailing", "sudden_dump"},
        lo=+10.0, hi=+18.0,
    )

    # C) moonshot +100% then 25% drop → 20% trailing (exit ≈ peak×0.8 ≈ entry×1.64)
    await run_scenario(
        engine, engine_mod, "C_moonshot_trail",
        mult_path=[1.5, 1.85, 2.05, 1.9, 1.7, 1.6],
        expect_reasons={"smart_trailing", "sudden_dump", "mega_tp"},
        lo=+40.0, hi=+110.0,
    )

    # D) straight dump → hard SL -4%
    await run_scenario(
        engine, engine_mod, "D_hard_sl",
        mult_path=[0.985, 0.955, 0.94],
        expect_reasons={"stop_loss"},
        lo=-8.0, hi=-2.0,
    )

    # E) +9% peak, bleed 8%+ from peak → sudden_dump fires
    await run_scenario(
        engine, engine_mod, "E_sudden_dump",
        mult_path=[1.06, 1.09, 1.005, 0.995],
        expect_reasons={"sudden_dump", "smart_trailing"},
        lo=-4.0, hi=+4.0,
    )

    print(f"\n=== RESULT: {PASSED} passed, {FAILED} failed ===")
    await close_db()
    sys.exit(1 if FAILED else 0)


asyncio.run(main())
