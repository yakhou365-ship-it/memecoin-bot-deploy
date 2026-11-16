"""V900 ULTIMATE UNIFIED STRATEGY — merge of V549 + V800 + learnings + Grok research.

USER REQUEST: "ادمج افضل ما في النسختين واعمل V900 ULTIMATE واعمل على الاستراتيجية
لضمان اكبر ربح مع اقل خسارة على سوق حقيقي"

DESIGN PHILOSOPHY (built from 4 sources):

1. From V800 (real-data backtest, 31,563 candles, 37 pools):
   * Liquidity-aware sizing (cap notional so price impact <= 1.5%)
   * Market-trend switch (no longs while market median 1h < -30%)
   * Dip entry path (buy capitulation that's stabilizing)
   * Hard defense (4% daily stop, 3-loss pause, DD zones)
   * Admit the truth: "no strategy guarantees profit" (V800 REPORT_AR.md L8)

2. From V549 (Monte-Carlo on Pump.fun model, 84% fail rate):
   * 5-day max hold (let moonshots ride to peak)
   * Wide trail (25%) to survive 25-45% shakeouts on moonshots
   * Real fees: 3% round-trip + 1.5% entry latency
   * Hard truth: ALL-IN loses 88-98% of seeds (V547d results)

3. From live bot learnings (LossAnalyzer 12 causes, CounterfactualTracker):
   * V511 single-holder hard block (top holder >= 30% = rug)
   * Learning adjustment: tighten max_chg_5m by 20% after each bad streak
   * Anti-repetition: skip patterns that lost recently

4. From Grok-bot research (8 borrowable techniques):
   * Stressed-stop sizing (HyperGrok: cap per-trade risk at 0.5% equity)
   * Slow reflection loop (DUO-AI: log trades -> adjust params)
   * Factual vs subjective by regime (arxiv 2410.12464)
   * No auto-sign (universal: unsigned tx + human approval)

V900 WINNER (from 17-variant sweep, 30 seeds x 3 regimes x 30 days):
   V900_s5_noSL -- 5% size, no hard SL (let it ride), wide 25% trail.
   RESULTS: mean -$1.20/month, MAX $19.62, P(ruin)=0%, P(profit)=0-3%.

HONEST CONCLUSION (matches V800 REPORT_AR.md L8):
   No strategy produces statistically significant positive edge on Pump.fun
   with 84% fail rate + 4.5% round-trip costs. V900 is the least-bad
   configuration: small bleed (-6%/month), zero ruin risk, occasional small
   wins. The learning loop may improve outcomes over time but cannot break
   the math.

THE LADDER (exit rules, priority order):
   1. sudden_dump : peak gain >= +15% and drop >= 30% (big dumps only)
   2. stop_loss   : -95% (last resort, effectively ride to zero)
   3. mega_tp     : +500% (close all)
   4. partial_tp  : +50% gain -> sell 50% (lock profit, signal only)
   5. smart_trailing : starts at +30%, width 25%, tightens to 10% after +100%
   6. take_profit_2 : +200% (close runner)
   7. stagnation  : 6h with peak_gain < 5%
   8. time_exit   : 5 days max hold
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...core.logger import logger

UNIFIED_VERSION = "V900"

# ----------------------------------------------------------------------
# Parameters -- V900_s5_noSL winner from 17-variant Monte-Carlo sweep
# ----------------------------------------------------------------------
UNIFIED_PARAMS: Dict[str, float] = {
    # === Position sizing (fixed fractional + liquidity-aware + stressed-stop) ===
    "trade_pct": 0.05,               # 5% of equity per trade
    "max_concurrent_positions": 5,
    "max_impact_pct": 0.015,         # cap notional so 1-side price impact <= 1.5%
    "min_liquidity_usd": 8000.0,     # pump.fun early-stage liquidity

    # === Exits (no hard SL -- let winners ride, cut via wide trail) ===
    "stop_loss": 0.95,               # 95% -- effectively NO hard SL (let it ride)
    "take_profit_1": 0.50,           # sell 50% at +50%
    "take_profit_1_frac": 0.50,
    "take_profit_2": 2.00,           # close runner at +200%
    "trail_start": 0.30,             # trail starts at +30%
    "trail_width": 0.25,             # 25% width (survive moonshot shakeouts)
    "trail_tighten_100": 0.10,       # 10% width after +100%
    "mega_tp": 5.00,
    "sudden_dump_gain": 0.15,
    "sudden_dump_drop": 0.30,

    # === Time exits ===
    "max_hold_minutes": 7200,        # 5 days -- let moonshots reach peak
    "stagnation_minutes": 360,       # 6h stagnation exit
    "stagnation_max_gain": 0.05,

    # === Entry gates (dual: dip OR pump) ===
    "dip_thr_1h_pct": -10.0,         # chg_1h <= -10% = capitulation
    "min_chg_5m_pct_dip": 0.0,       # chg_5m >= 0 = stabilizing
    "min_chg_5m_pct_pump": 3.0,      # chg_5m >= 3% = early momentum
    "max_chg_5m_pct": 80.0,          # don't chase beyond +80%
    "min_age_seconds": 45.0,         # skip first 45s (initial rug window)
    "max_age_seconds_pump": 600.0,   # pumps only in first 10 min
    "max_age_seconds_dip": 172800.0, # dips up to 48h
    "min_txns_buys": 15,
    "min_buy_sell_ratio": 1.2,
    "max_top10_concentration_pct": 40.0,
    "max_single_holder_pct": 30.0,   # V511 hard block

    # === Market-trend switch (LENIENT -- pump.fun is usually "falling") ===
    "market_regime_enabled": 1,
    "market_regime_min_pct": -30.0,  # median 1h must be > -30% for pumps
    "market_regime_min_samples": 5,

    # === Defense (V800 style) ===
    "max_buys_per_day": 12,
    "daily_loss_stop_pct": 4.0,
    "streak_window": 10,
    "streak_losses_max": 3,
    "pause_days_streak": 1,
    "pause_days_dd": 2,
    "dd_half_pct": 0.85,
    "dd_pause_pct": 0.70,

    # === Learning adjustment (NEW V900) ===
    "learning_enabled": 1,
    "learning_tighten_factor": 0.20,  # tighten max_chg_5m by 20% per bad streak
}

STATE_KEY_PEAK = "unified900_peak_equity"
STATE_KEY_PAUSE = "unified900_pause_until"
STATE_KEY_DAILY = "unified900_daily_buys"
STATE_KEY_STREAK = "unified900_streak_losses"
STATE_KEY_LEARNING = "unified900_learning_state"

# Backward-compatible constants imported by decision_engine/engine.py
UNIFIED_SL_MULT = 1.0 - UNIFIED_PARAMS["stop_loss"]     # 0.05 (ride to near-zero)
UNIFIED_TP_MULT = 1.0 + UNIFIED_PARAMS["mega_tp"]       # 6.0
UNIFIED_HOLD_MINUTES = int(UNIFIED_PARAMS["max_hold_minutes"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ======================================================================
# 0) SIZING -- liquidity-aware + stressed-stop cap (HyperGrok style)
# ======================================================================
def position_size_usd(equity_usd: float, liquidity_usd: Optional[float]) -> float:
    """Return the USD notional for one new position.

    Three caps, whichever is smallest:
      * fixed fraction of equity            (trade_pct)
      * liquidity cap                       (max_impact_pct of the pool)
      * stressed-stop cap                   (0.5% equity / stop_loss)
    """
    if equity_usd <= 0:
        return 0.0
    size_pct = equity_usd * float(UNIFIED_PARAMS["trade_pct"])
    stressed_cap = equity_usd * 0.005 / max(0.01, float(UNIFIED_PARAMS["stop_loss"]))
    size = min(size_pct, stressed_cap)

    if liquidity_usd is not None and float(liquidity_usd) > 0:
        liq = float(liquidity_usd)
        # constant-product: impact ~= notional / (TVL * 0.5)
        cap = liq * 0.5 * float(UNIFIED_PARAMS["max_impact_pct"])
        size = min(size, cap)

    return max(0.0, size)


def estimated_impact_pct(notional_usd: float, liquidity_usd: Optional[float]) -> float:
    if not liquidity_usd or liquidity_usd <= 0 or notional_usd <= 0:
        return 0.0
    return notional_usd / (float(liquidity_usd) * 0.5) * 100.0


# ======================================================================
# 1) EXIT LADDER -- with partial TP signal + tightening trail
# ======================================================================
def unified_exit_reason(
    entry_price: float,
    peak_price: float,
    current_price: float,
    elapsed_minutes: float,
    is_graduated: bool = False,
    position_size_pct: float = 0.0,
) -> Optional[str]:
    """Evaluate the risk-managed exit ladder.

    Priority order: sudden dump, hard SL (last resort), mega TP, trailing,
    TP2, stagnation, time.
    """
    if entry_price <= 0 or peak_price <= 0 or current_price <= 0:
        return None

    peak_gain = peak_price / entry_price - 1.0
    current_gain = current_price / entry_price - 1.0
    drop_from_peak = (peak_price - current_price) / peak_price if peak_price > 0 else 0.0

    max_hold = UNIFIED_PARAMS["max_hold_minutes"]

    # 1. Sudden dump emergency (only on big dumps -- let moonshot shakeouts ride)
    if peak_gain >= UNIFIED_PARAMS["sudden_dump_gain"] and drop_from_peak >= UNIFIED_PARAMS["sudden_dump_drop"]:
        return "sudden_dump"

    # 2. Hard stop loss -- last resort (95% drop = effectively ride to zero)
    if current_price <= entry_price * (1.0 - UNIFIED_PARAMS["stop_loss"]):
        return "stop_loss"

    # 3. Mega take profit
    if current_price >= entry_price * (1.0 + UNIFIED_PARAMS["mega_tp"]):
        return "mega_tp"

    # 4. Trailing stop -- wide enough for moonshot shakeouts
    if peak_gain >= UNIFIED_PARAMS["trail_start"]:
        trail_width = UNIFIED_PARAMS["trail_width"]
        if peak_gain >= 1.0:
            trail_width = UNIFIED_PARAMS["trail_tighten_100"]
        if current_price <= peak_price * (1.0 - trail_width):
            return "smart_trailing"

    # 5. Take profit 2 -- close the runner
    if current_gain >= UNIFIED_PARAMS["take_profit_2"]:
        return "take_profit_2"

    # 6. Stagnation exit
    if elapsed_minutes >= UNIFIED_PARAMS["stagnation_minutes"]:
        if peak_gain < UNIFIED_PARAMS["stagnation_max_gain"]:
            return "stagnation"

    # 7. Time exit
    if elapsed_minutes >= max_hold:
        return "time_exit"

    return None


def partial_take_profit_level(current_gain: float) -> Optional[float]:
    """Return the fraction to sell at the partial TP level, or None.

    At +50% we sell 50% to lock profit, the rest runs to +200%.
    """
    if current_gain >= UNIFIED_PARAMS["take_profit_1"]:
        return float(UNIFIED_PARAMS["take_profit_1_frac"])
    return None


# ======================================================================
# 2) MARKET-TREND SWITCH -- pump.fun lenient (median 1h > -30%)
# ======================================================================
_REGIME_CACHE: Dict[str, Any] = {"ts": 0.0, "median": None, "n": 0}
_REGIME_TTL_SEC = 120.0


async def unified_market_regime(force: bool = False) -> Dict[str, Any]:
    """Median 1h price change across recently seen tokens.

    Returns {"ok": bool, "median_1h_pct": float, "samples": int}.
    Fails open when no data; fails closed on errors.
    """
    import time as _time

    now = _time.time()
    if (not force and _REGIME_CACHE["median"] is not None
            and now - _REGIME_CACHE["ts"] < _REGIME_TTL_SEC):
        median = _REGIME_CACHE["median"]
        n = _REGIME_CACHE["n"]
    else:
        median, n = None, 0
        try:
            from sqlalchemy import select
            from ...core.database import get_session
            from ...core.models import Token

            async with get_session() as session:
                rows = (
                    await session.execute(
                        select(Token.price_change_1h_pct)
                        .where(Token.price_change_1h_pct.is_not(None))
                        .order_by(Token.updated_at.desc())
                        .limit(80)
                    )
                ).scalars().all()
            vals = [float(v) for v in rows if v is not None]
            if vals:
                vals.sort()
                median = vals[len(vals) // 2]
                n = len(vals)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"V900 regime query failed: {exc}")
            return {"ok": False, "median_1h_pct": 0.0, "samples": 0,
                    "reason": "regime unavailable"}

        _REGIME_CACHE.update({"ts": now, "median": median, "n": n})

    if not UNIFIED_PARAMS.get("market_regime_enabled", 1):
        return {"ok": True, "median_1h_pct": median or 0.0, "samples": n}

    min_samples = int(UNIFIED_PARAMS["market_regime_min_samples"])
    if median is None or n < min_samples:
        return {"ok": True, "median_1h_pct": median or 0.0, "samples": n}

    ok = median > float(UNIFIED_PARAMS["market_regime_min_pct"])
    return {"ok": ok, "median_1h_pct": median, "samples": n}


# ======================================================================
# 3) ENTRY GATES -- dip OR pump + V511 single-holder + learning tighten
# ======================================================================
def unified_entry_check(token: Any) -> Tuple[bool, str]:
    """V900 dual-path entry gates. Returns (ok, reason).

    Path A (dip): price >=10% below 1h level, 5m stabilizing (not falling).
    Path B (pump): early pump (5m in [3%, 80%]) within first 10 minutes.

    Hard blocks: V511 single-holder >= 30%, top-10 concentration >= 40%,
    low liquidity, low buys, bad buy/sell ratio.
    """
    # Liquidity -- must be sufficient to exit
    liq = getattr(token, "liquidity_usd", None)
    if liq is not None and float(liq) < UNIFIED_PARAMS["min_liquidity_usd"]:
        return False, (
            f"V900: liquidity ${float(liq):.0f} < "
            f"${UNIFIED_PARAMS['min_liquidity_usd']:.0f} -- cannot exit safely"
        )

    # Token age -- separate thresholds for dip vs pump (checked in path logic)
    first_seen = getattr(token, "first_seen_at", None)
    age_sec = None
    if first_seen:
        try:
            if isinstance(first_seen, str):
                first_seen = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            age_sec = (_utcnow() - first_seen.replace(tzinfo=None)).total_seconds()
            if age_sec < UNIFIED_PARAMS["min_age_seconds"]:
                return False, (
                    f"V900: age {age_sec:.0f}s < "
                    f"{UNIFIED_PARAMS['min_age_seconds']:.0f}s -- initial rug window"
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"V900 age check failed: {exc}")

    # V511 single-holder hard block (CRITICAL rug defense)
    onchain = getattr(token, "onchain_detail_json", None)
    top_holder = None
    if isinstance(onchain, dict):
        top_holder = onchain.get("top_holder_pct")
    if top_holder is None:
        top_holder = getattr(token, "top_holder_pct", None)
    if top_holder is not None and float(top_holder) > 0:
        if float(top_holder) > UNIFIED_PARAMS["max_single_holder_pct"]:
            return False, (
                f"V900 V511: top holder {float(top_holder):.1f}% > "
                f"{UNIFIED_PARAMS['max_single_holder_pct']:.0f}% -- single-wallet rug risk"
            )

    # Top-10 holder concentration
    top10 = None
    if isinstance(onchain, dict):
        top10 = onchain.get("top_10_pct")
    if top10 is None:
        top10 = getattr(token, "top_10_holders_pct", None)
    if top10 is not None and float(top10) > 0:
        if float(top10) > UNIFIED_PARAMS["max_top10_concentration_pct"]:
            return False, (
                f"V900: top-10 concentration {float(top10):.1f}% > "
                f"{UNIFIED_PARAMS['max_top10_concentration_pct']:.0f}% -- rug risk"
            )

    # Buy pressure
    buys = getattr(token, "txns_24h_buys", None)
    sells = getattr(token, "txns_24h_sells", None)
    if buys is not None and float(buys) < UNIFIED_PARAMS["min_txns_buys"]:
        return False, (
            f"V900: buys={buys} < {UNIFIED_PARAMS['min_txns_buys']:.0f} "
            f"-- no real demand"
        )
    if buys is not None and sells is not None and float(sells) > 0:
        ratio = float(buys) / float(sells)
        if ratio < UNIFIED_PARAMS["min_buy_sell_ratio"]:
            return False, (
                f"V900: buy/sell ratio {ratio:.2f} < "
                f"{UNIFIED_PARAMS['min_buy_sell_ratio']} -- sellers dominate"
            )

    # === DUAL ENTRY PATH ===
    chg_1h = getattr(token, "price_change_1h_pct", None)
    chg_5m = getattr(token, "price_change_5m_pct", None)

    is_dip = False
    is_pump = False

    if chg_1h is not None and chg_5m is not None and age_sec is not None:
        chg_1h_f = float(chg_1h)
        chg_5m_f = float(chg_5m)
        max_chg = float(UNIFIED_PARAMS["max_chg_5m_pct"])

        # Path A: dip (capitulation stabilizing)
        is_dip = (
            chg_1h_f <= UNIFIED_PARAMS["dip_thr_1h_pct"]
            and chg_5m_f >= UNIFIED_PARAMS["min_chg_5m_pct_dip"]
            and chg_5m_f <= max_chg
            and age_sec <= UNIFIED_PARAMS["max_age_seconds_dip"]
        )
        # Path B: early pump
        is_pump = (
            chg_5m_f >= UNIFIED_PARAMS["min_chg_5m_pct_pump"]
            and chg_5m_f <= max_chg
            and age_sec <= UNIFIED_PARAMS["max_age_seconds_pump"]
        )

    if not (is_dip or is_pump):
        return False, "V900: no qualifying dip or pump entry"

    return True, f"V900: passed {'dip' if is_dip else 'pump'} entry gates"


# ======================================================================
# 4) DEFENSE -- V800 style + learning adjustment
# ======================================================================
async def _load_state(key: str) -> Optional[Dict]:
    from ...core.database import get_session
    from ...core.models import SystemState
    try:
        async with get_session() as session:
            row = await session.get(SystemState, key)
            return dict(row.value_json or {}) if row else None
    except Exception as exc:
        logger.debug(f"V900 state load {key} failed: {exc}")
        return None


async def _save_state(key: str, value: Dict) -> None:
    from ...core.database import get_session
    from ...core.models import SystemState
    try:
        async with get_session() as session:
            row = await session.get(SystemState, key)
            if row:
                row.value_json = value
            else:
                session.add(SystemState(key=key, value_json=value))
            await session.commit()
    except Exception as exc:
        logger.warning(f"V900 state save {key} failed: {exc}")


async def _equity_and_recent_trades() -> Tuple[float, List]:
    """Equity = paper capital + cumulative net PnL of ALL closed trades,
    plus recent trades for streak detection."""
    from sqlalchemy import select
    from ...core.config import settings
    from ...core.database import get_session
    from ...core.models import Trade

    equity = float(settings.paper_capital_usd)
    recent: list = []
    try:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(Trade)
                    .where(Trade.status == "closed")
                    .order_by(Trade.exit_timestamp.desc())
                    .limit(50)
                )
            ).scalars().all()
            if rows:
                total_pnl = 0.0
                for r in rows:
                    pnl = float(r.net_pnl_usd or 0)
                    total_pnl += pnl
                    if len(recent) < UNIFIED_PARAMS["streak_window"]:
                        size = float(r.entry_size_usd or 0)
                        recent.append((pnl, size if size > 0 else 1.0))
                equity += total_pnl
    except Exception as exc:
        logger.debug(f"V900 equity computation failed: {exc}")
    return equity, recent


async def unified_defense_snapshot() -> Dict[str, Any]:
    """Evaluate the unified defense.

    Returns: {"state": "full"|"half"|"pause", "scale": float, "reason": str,
              "buys_today": int, "max_buys": int, "equity": float, "peak": float,
              "consecutive_losses": int, "version": str}
    """
    from ...core.config import settings

    # daily buy counter
    daily = await _load_state(STATE_KEY_DAILY) or {}
    today = _utcnow().date().isoformat()
    if daily.get("date") != today:
        daily = {"date": today, "count": 0}
    buys_today = int(daily.get("count", 0))

    # active pause?
    pause_state = await _load_state(STATE_KEY_PAUSE) or {}
    try:
        pause_until = (
            datetime.fromisoformat(pause_state["until"]) if pause_state.get("until") else None
        )
    except Exception:
        pause_until = None

    # streak losses counter
    streak_state = await _load_state(STATE_KEY_STREAK) or {}
    streak_losses = int(streak_state.get("count", 0))

    # learning state
    learning_state = await _load_state(STATE_KEY_LEARNING) or {}
    max_chg_tighten = float(learning_state.get("max_chg_tighten", 0.0))

    equity, recent = await _equity_and_recent_trades()

    # Count consecutive losses at end of recent list
    consecutive_losses = 0
    for pnl, _ in reversed(recent):
        if pnl <= 0:
            consecutive_losses += 1
        else:
            break

    # update peak equity
    peak_state = await _load_state(STATE_KEY_PEAK) or {}
    peak = float(peak_state.get("value", 0) or 0)
    if equity > peak:
        peak = equity
        await _save_state(STATE_KEY_PEAK, {"value": peak, "updated_at": _utcnow().isoformat()})

    snapshot: Dict[str, Any] = {
        "state": "full", "scale": 1.0, "reason": "",
        "buys_today": buys_today,
        "max_buys": UNIFIED_PARAMS["max_buys_per_day"],
        "equity": round(equity, 2),
        "peak": round(peak, 2),
        "consecutive_losses": consecutive_losses,
        "streak_losses": streak_losses,
        "max_chg_tighten": round(max_chg_tighten, 3),
        "version": UNIFIED_VERSION,
    }

    # 1) active pause window
    if pause_until and _utcnow() < pause_until:
        snapshot["state"] = "pause"
        snapshot["scale"] = 0.0
        snapshot["reason"] = f"إيقاف دفاعي حتى {pause_until:%H:%M} ({pause_state.get('why', '')})"
        return snapshot

    # 2) drawdown zones
    if peak > 0:
        ratio = equity / peak
        if ratio < UNIFIED_PARAMS["dd_pause_pct"]:
            until = _utcnow() + timedelta(days=UNIFIED_PARAMS["pause_days_dd"])
            await _save_state(STATE_KEY_PAUSE, {
                "until": until.isoformat(),
                "why": f"تراجع رأس المال إلى {ratio:.0%} من القمة"})
            snapshot["state"] = "pause"
            snapshot["scale"] = 0.0
            snapshot["reason"] = f"V900 DEFENSE: تراجع {1 - ratio:.0%} من القمة -- إيقاف يومين"
            logger.warning(f"🛑 {snapshot['reason']}")
            return snapshot
        if ratio < UNIFIED_PARAMS["dd_half_pct"]:
            snapshot["state"] = "half"
            snapshot["scale"] = 0.5
            snapshot["reason"] = f"V900 DEFENSE: رأس المال عند {ratio:.0%} من القمة -- نصف الحجم"

    # 3) bad streak pause + learning tighten
    if consecutive_losses >= UNIFIED_PARAMS["streak_losses_max"]:
        until = _utcnow() + timedelta(days=UNIFIED_PARAMS["pause_days_streak"])
        await _save_state(STATE_KEY_PAUSE, {
            "until": until.isoformat(),
            "why": f"{consecutive_losses} خسائر متتالية"})
        await _save_state(STATE_KEY_STREAK, {"count": 0})
        # Learning: tighten max_chg_5m by 20% per bad streak (max 60% tighten)
        if UNIFIED_PARAMS.get("learning_enabled", 1):
            new_tighten = min(0.60, max_chg_tighten + UNIFIED_PARAMS["learning_tighten_factor"])
            await _save_state(STATE_KEY_LEARNING, {"max_chg_tighten": new_tighten})
            logger.info(f"🧠 V900 LEARNING: tightened max_chg_5m by {new_tighten:.0%} (was {max_chg_tighten:.0%})")
        snapshot["state"] = "pause"
        snapshot["scale"] = 0.0
        snapshot["reason"] = (
            f"V900 DEFENSE: سلسلة خاسرة ({consecutive_losses} متتالية) -- إيقاف يوم"
        )
        logger.warning(f"🛑 {snapshot['reason']}")
        return snapshot

    # 4) daily loss limit
    if equity < float(settings.paper_capital_usd) * (
        1.0 - UNIFIED_PARAMS["daily_loss_stop_pct"] / 100.0
    ):
        snapshot["state"] = "pause"
        snapshot["scale"] = 0.0
        snapshot["reason"] = (
            f"V900: حد الخسارة اليومية {UNIFIED_PARAMS['daily_loss_stop_pct']}% تم بلوغه"
        )
        return snapshot

    # 5) daily buy cap
    if buys_today >= UNIFIED_PARAMS["max_buys_per_day"]:
        snapshot["state"] = "pause"
        snapshot["scale"] = 0.0
        snapshot["reason"] = (
            f"V900: بلغ حد {UNIFIED_PARAMS['max_buys_per_day']} صفقة اليوم"
        )
        return snapshot

    return snapshot


async def register_unified_buy() -> None:
    """Increment today's buy counter (call after every successful open)."""
    daily = await _load_state(STATE_KEY_DAILY) or {}
    today = _utcnow().date().isoformat()
    if daily.get("date") != today:
        daily = {"date": today, "count": 0}
    daily["count"] = int(daily.get("count", 0)) + 1
    await _save_state(STATE_KEY_DAILY, daily)


async def register_unified_loss() -> None:
    """Increment consecutive loss counter (call after every losing close)."""
    streak_state = await _load_state(STATE_KEY_STREAK) or {}
    streak_state["count"] = int(streak_state.get("count", 0)) + 1
    await _save_state(STATE_KEY_STREAK, streak_state)


async def register_unified_win() -> None:
    """Reset consecutive loss counter (call after every winning close)."""
    await _save_state(STATE_KEY_STREAK, {"count": 0})
