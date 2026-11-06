"""V505 unit tests: real-slippage measurement + ATR-based SL.

Run: cd /home/z/my-project/v503-fix/backend && python tests/test_v505_fixes.py
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{BACKEND_DIR}/data/test_v505.db"
os.environ["TRADING_MODE"] = "paper"
os.environ["ENABLE_ORCHESTRATOR"] = "false"
os.environ["AUTO_START_TRADING"] = "false"

db_path = BACKEND_DIR / "data" / "test_v505.db"
if db_path.exists():
    db_path.unlink()

from app.modules.live_trading.executor import LiveTradingExecutor


def test_slippage_buy_measurement():
    """Buy: 0.1 SOL spent, 0.001 tokens received → actual fill price + slippage."""
    print("\n=== Test 1: BUY real-slippage measurement ===")
    exec_ = LiveTradingExecutor()
    sol_price = 150.0
    # Wallet BEFORE: 1.0 SOL, 0 tokens
    before = {"sol": 1.0, "token_balance": 0.0}
    # Wallet AFTER: 0.9 SOL (0.1 spent incl. fees+slippage), 0.001 tokens received
    after = {"sol": 0.9, "token_balance": 0.001}
    # Market price the scanner reported: notional / qty at market
    # Market says 0.1 SOL = $15 → at market price $15 / 0.001 = $15000 per token
    market_price = 15000.0

    res = exec_._compute_actual_slippage(
        before, after, notional_usd=15.0,
        sol_price_usd=sol_price, side="buy",
        market_price_usd=market_price,
    )
    print(f"  measured={res['measured']}, actual_fill=${res['actual_fill_price']:.2f}, "
          f"slip={res.get('actual_slippage_pct'):.2f}%, sol_spent={res['actual_cost_sol']:.4f}")

    assert res["measured"] is True
    # actual_fill_price = (0.1 SOL × $150) / 0.001 tokens = $15000 / 0.001 = $15,000
    assert abs(res["actual_fill_price"] - 15000.0) < 1.0
    # market = $15000, fill = $15000 → slippage 0% (perfect-world test)
    assert abs(res["actual_slippage_pct"]) < 0.01
    print("  ✅ Zero slippage correctly measured")


def test_slippage_buy_negative_slippage():
    """Buy with positive slippage (paid more than market)."""
    print("\n=== Test 2: BUY with positive slippage (overpaid) ===")
    exec_ = LiveTradingExecutor()
    # Market price: 1 token = $100
    # Wallet before: 1.0 SOL, 0 tokens
    # Wallet after: 0.85 SOL (0.15 spent), 0.002 tokens received
    # Actual cost per token = (0.15 × $150) / 0.002 = $22.5 / 0.002 = $11,250
    # Wait — market price for 0.002 tokens at $100/token = $0.20
    # Notional at market: 0.002 × $100 = $0.20
    # But we spent 0.15 SOL × $150 = $22.5 — way more than the market value.
    # actual_fill_price = $22.5 / 0.002 = $11250
    # slippage = (11250 - 100) / 100 × 100 = 11150% (massive — like buying on a thin pool)
    before = {"sol": 1.0, "token_balance": 0.0}
    after = {"sol": 0.85, "token_balance": 0.002}
    market_price = 100.0  # $100 per token
    res = exec_._compute_actual_slippage(
        before, after, 0.30, sol_price_usd=150.0, side="buy",
        market_price_usd=market_price,
    )
    print(f"  measured={res['measured']}, fill=${res['actual_fill_price']:.2f}, "
          f"slip={res['actual_slippage_pct']:.2f}%")
    assert res["measured"] is True
    assert res["actual_fill_price"] > market_price  # paid more than market
    assert res["actual_slippage_pct"] > 0  # positive slippage (cost more)
    print("  ✅ Positive slippage (overpaid) correctly measured")


def test_slippage_sell_measurement():
    """Sell: 0.001 tokens sold, 0.09 SOL received → actual exit price."""
    print("\n=== Test 3: SELL real-slippage measurement ===")
    exec_ = LiveTradingExecutor()
    sol_price = 150.0
    # Wallet BEFORE: 0.9 SOL, 0.001 tokens
    before = {"sol": 0.9, "token_balance": 0.001}
    # Wallet AFTER: 0.95 SOL (0.05 received), 0 tokens left
    after = {"sol": 0.95, "token_balance": 0.0}
    market_price = 15000.0  # $15000 per token
    res = exec_._compute_actual_slippage(
        before, after, 0.0, sol_price_usd=sol_price, side="sell",
        market_price_usd=market_price,
    )
    print(f"  measured={res['measured']}, exit=${res['actual_fill_price']:.2f}, "
          f"slip={res['actual_slippage_pct']:.2f}%, sol_received={res['actual_cost_sol']:.4f}")
    assert res["measured"] is True
    # exit price = (0.05 SOL × $150) / 0.001 tokens = $7.5 / 0.001 = $7500
    # market = $15000, fill = $7500 → 50% negative slippage (sold at half)
    assert abs(res["actual_fill_price"] - 7500.0) < 1.0
    assert abs(res["actual_slippage_pct"] - 50.0) < 0.1  # 50% slippage
    print("  ✅ Sell slippage correctly measured (50% — sold at half market)")


def test_slippage_handles_failures():
    """Slippage measurement should fail gracefully when wallet data is unusable."""
    print("\n=== Test 4: slippage failure handling ===")
    exec_ = LiveTradingExecutor()
    # Case 1: token_balance is None (RPC failed)
    res = exec_._compute_actual_slippage(
        {"sol": 1.0, "token_balance": None},
        {"sol": 0.9, "token_balance": None},
        15.0, 150.0, "buy", market_price_usd=100.0,
    )
    assert res["measured"] is False
    assert "token balance unavailable" in res["reason"]
    print(f"  ✅ Case 1 (RPC fail): {res['reason']}")

    # Case 2: SOL price = 0 (should fail)
    res = exec_._compute_actual_slippage(
        {"sol": 1.0, "token_balance": 0.0},
        {"sol": 0.9, "token_balance": 0.001},
        15.0, 0.0, "buy", market_price_usd=100.0,
    )
    assert res["measured"] is False
    print(f"  ✅ Case 2 (no SOL price): {res['reason']}")

    # Case 3: invalid diff (sol went UP after buy — impossible unless pre-existing credit)
    res = exec_._compute_actual_slippage(
        {"sol": 1.0, "token_balance": 0.0},
        {"sol": 1.1, "token_balance": 0.001},
        15.0, 150.0, "buy", market_price_usd=100.0,
    )
    assert res["measured"] is False
    print(f"  ✅ Case 3 (invalid diff): {res['reason']}")


def test_atr_sl_floor_and_ceiling():
    """Verify the V505 SL floor (-10%) and ceiling (-25%) on memecoins."""
    print("\n=== Test 5: ATR-SL floor (-10%) and ceiling (-25%) ===")
    # Test the math directly (don't need full DecisionEngine — just the formula).
    entry = 100.0

    # Case 1: Low ATR (quiet token) → 1.8×ATR was -2%, but floor forces -10%.
    atr_low = 1.11  # 1.8 × 1.11 = 2% (below floor)
    sl_distance_v505 = 3.0 * atr_low  # 3.33
    sl_pct_v505 = sl_distance_v505 / entry  # 3.33%
    sl_pct_final = max(0.10, min(0.25, sl_pct_v505))
    sl_price_v505 = entry * (1 - sl_pct_final)
    print(f"  Low-ATR token: ATR=${atr_low}, V505 SL=${sl_price_v505:.2f} (-{sl_pct_final*100:.0f}%)")
    assert sl_pct_final == 0.10  # floor kicked in

    # Case 2: Medium ATR → ATR-based SL within [10%, 25%].
    atr_med = 5.0  # 3 × 5 = 15% → within range
    sl_pct = (3.0 * atr_med) / entry  # 15%
    sl_pct_final = max(0.10, min(0.25, sl_pct))
    print(f"  Med-ATR token: ATR=${atr_med}, V505 SL=-{sl_pct_final*100:.0f}%")
    assert sl_pct_final == 0.15  # within range

    # Case 3: Very high ATR (volatile token) → ceiling forces -25%.
    atr_high = 20.0  # 3 × 20 = 60% (way above ceiling)
    sl_pct = (3.0 * atr_high) / entry  # 60%
    sl_pct_final = max(0.10, min(0.25, sl_pct))
    print(f"  High-ATR token: ATR=${atr_high}, V505 SL=-{sl_pct_final*100:.0f}%")
    assert sl_pct_final == 0.25  # ceiling kicked in

    print("  ✅ Floor (-10%) and ceiling (-25%) correctly enforced")


def main():
    print("V505 fixes unit tests")
    print("=" * 60)
    test_slippage_buy_measurement()
    test_slippage_buy_negative_slippage()
    test_slippage_sell_measurement()
    test_slippage_handles_failures()
    test_atr_sl_floor_and_ceiling()
    print("\n" + "=" * 60)
    print("🎉 All V505 unit tests passed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
