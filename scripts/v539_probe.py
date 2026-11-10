"""Debug probe: replicate scenario A step by step and print internal exit vars."""
import asyncio, os, sys
from pathlib import Path

BACKEND_DIR = Path("/home/z/my-project/backend")
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/test_probe.db"
os.environ["TRADING_MODE"] = "paper"
os.environ["ENABLE_ORCHESTRATOR"] = "false"
os.environ["AUTO_START_TRADING"] = "false"
os.environ["TRADING_PROFILE"] = "adaptive_pro"

from app.core.config import settings
print("profile:", settings.trading_profile, "| is_adaptive_pro_mode:", settings.is_adaptive_pro_mode)
print("quick/partial/moon/trail:",
      settings.adaptive_pro_quick_tp, settings.adaptive_pro_partial_tp,
      settings.adaptive_pro_moonshot_threshold, settings.adaptive_pro_moonshot_trail)


async def main():
    from app.core.database import init_db, close_db, get_session
    from app.core.models import Token, Trade
    from app.modules.paper_trading import engine as engine_mod

    db = BACKEND_DIR / "data" / "test_probe.db"
    if db.exists():
        db.unlink()
    await init_db()
    e = engine_mod.PaperTradingEngine()

    async with get_session() as s:
        t = Token(id="TP1", symbol="TP1", name="probe", address="m1", chain="solana",
                  price_usd=1.0, liquidity_usd=200000.0, volume_24h_usd=1000000.0)
        s.add(t)
        await s.commit()

    tr = await e.open_position(token_id="TP1", entry_price=1.0, position_size_usd=100.0,
                               liquidity_usd=200000.0, holding_period_minutes=120)
    entry = float(tr.entry_price)
    print("entry:", entry)
    async with get_session() as s:
        d = await s.get(Trade, tr.id)
        d.stop_loss_price = entry * 0.96
        d.take_profit_price = entry * 2.0
        s.add(d)
        await s.commit()

    for m in [1.09, 1.10, 1.025]:
        price = entry * m
        async with get_session() as s:
            tk = await s.get(Token, "TP1")
            tk.price_usd = price
            s.add(tk)
            await s.commit()
        # inspect BEFORE manage
        async with get_session() as s:
            d = await s.get(Trade, tr.id)
            peak = d.max_unrealized_profit_usd
        peak = peak if peak else max(entry, price)
        peak_gain = peak / entry - 1
        drop = (peak - price) / peak if peak > 0 else 0
        lock_price = entry * (1 + 0.08 * 0.375)
        print(f"m={m} price={price:.5f} peak={peak:.5f} peak_gain={peak_gain:+.3f} "
              f"drop={drop:.3f} lock_price={lock_price:.5f} price<=lock? {price <= lock_price}")
        closed = await e.manage_open_positions()
        print("  closed:", {k: v for k, v in closed.items() if v})
        async with get_session() as s:
            d2 = await s.get(Trade, tr.id)
            print("  status:", d2.status, "| peak_after:", d2.max_unrealized_profit_usd)
            if d2.status == "closed":
                print("  exit_reason:", d2.exit_reason, "| pnl:", d2.pnl_pct)
                break
    await close_db()

asyncio.run(main())
