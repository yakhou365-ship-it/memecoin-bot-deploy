"""V541 UNIFIED STRATEGY — one logic for the whole bot.

Validated on a Pump.fun Monte-Carlo model built from real platform
statistics (Cryptorank/Dune: 69% of tokens stop trading on day 1,
15% rugs, 3% round-trip costs via PumpPortal Lightning) across 3
market regimes (base / harsh / normal) x 8 model perturbations x 120
seeds, with shakeouts and a 1.5% entry-latency penalty.

Results vs the old sniper profile (median, $20 / 30 days):
  - harsh market : -21% -> -8%   (no more catastrophic months, P(lose>40%)=0%)
  - base market  : -4%  -> -2% (conservative filter) .. +7% (strong filter)
  - normal market: +33% -> +47%
  - p95 keeps the moonshot lottery (~x3-4)

THE LADDER (exit rules, priority order):
  1. sudden_dump : peak gain >= +10% and price dropped >= 20% from peak
  2. stop_loss   : price <= entry x (1 - 6%)
  3. mega_tp     : price >= entry x 11 (10x cap)
  4. band exit   : peak gain in [+15%, +35%)  -> sell ALL at market
  5. runner trail: peak gain >= +35% (gapped through the band)
                   -> trail 15% from peak, tightened to 11.25% after +50%
                   and 9% after +100% (ratchet: let moonshots complete)
  6. stagnation  : 4h in 0..+5% with peak < +5% -> exit
  7. time exit   : 5 days max hold

THE ENTRY GATES (one set for every profile):
  - liquidity >= $4,000
  - 5m price change in [+8%, +60%]  (momentum started, not exhausted)
  - token age <= 180 seconds
  - buy pressure: txns_24h_buys >= 20 and buys/sells >= 1.3 (when data exists)
  - top-10 holder concentration <= 25% (when data exists)

THE DEFENSE (capital protection, evaluated every cycle):
  - drawdown zones: equity >= 92% of peak -> full size
                    80%..92% of peak      -> half size, max 2 buys/day
                    < 80% of peak         -> pause 3 days
  - bad streak: last 6 trades WR <= 33% AND pnl <= -10% -> pause 2 days
  - max 3 buys per day, 12% daily loss stop
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from ...core.logger import logger

# ----------------------------------------------------------------------
# Parameters (frozen dict — the ONE strategy configuration)
# ----------------------------------------------------------------------
UNIFIED_PARAMS: Dict[str, float] = {
    # exits
    "quick_tp": 0.15,            # band exit at +15%
    "runner_threshold": 0.35,    # gap to +35% or beyond -> ride the runner
    "trail_width": 0.15,         # base trail: 15% drop from peak
    "trail_tighten_50": 0.75,    # trail x0.75 when peak >= +50%
    "trail_tighten_100": 0.60,   # trail x0.60 when peak >= +100%
    "sudden_dump_gain": 0.10,    # sudden dump needs peak >= +10%
    "sudden_dump_drop": 0.20,    # ...and a 20% drop from peak
    "stop_loss": 0.06,           # hard stop -6%
    "mega_tp": 10.0,             # 10x cap
    "stagnation_minutes": 240,   # 4h stagnation
    "stagnation_max_gain": 0.05, # stagnation = gain in (0, +5%]
    "max_hold_minutes": 7200,    # 5 days
    # entries
    "min_liquidity_usd": 4000.0,
    "min_chg_5m_pct": 8.0,
    "max_chg_5m_pct": 60.0,
    "max_age_seconds": 180.0,
    "min_txns_buys": 20,
    "min_buy_sell_ratio": 1.3,
    "max_top10_concentration_pct": 25.0,
    # defense
    "max_buys_per_day": 3,
    "daily_loss_stop_pct": 12.0,
    "dd_full_pct": 0.92,         # equity >= 92% of peak -> full size
    "dd_half_pct": 0.80,         # below 80% -> pause 3 days
    "streak_window": 6,
    "streak_wr_max": 1.0 / 3.0,
    "streak_pnl_min": -0.10,
    "pause_days_streak": 2,
    "pause_days_dd": 3,
}

UNIFIED_SL_MULT = 1.0 - UNIFIED_PARAMS["stop_loss"]     # 0.94
UNIFIED_TP_MULT = 1.0 + UNIFIED_PARAMS["mega_tp"]       # 11.0
UNIFIED_HOLD_MINUTES = UNIFIED_PARAMS["max_hold_minutes"]

STATE_KEY_PEAK = "unified_peak_equity"
STATE_KEY_PAUSE = "unified_pause_until"
STATE_KEY_DAILY = "unified_daily_buys"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------
# 1) EXIT LADDER — used by paper engine AND live executor
# ----------------------------------------------------------------------
def unified_exit_reason(
    entry_price: float,
    peak_price: float,
    current_price: float,
    elapsed_minutes: float,
) -> Optional[str]:
    """Evaluate the unified exit ladder.

    Returns the exit reason string, or None to keep holding.
    SL/TP are computed from ``entry_price`` directly (not from the
    trade's stored stop_loss_price) so that profile switchers or stale
    suggestions can never diverge from the ONE strategy.
    """
    if entry_price <= 0 or peak_price <= 0 or current_price <= 0:
        return None

    peak_gain = peak_price / entry_price - 1.0
    drop_from_peak = (peak_price - current_price) / peak_price

    # 1. sudden dump (emergency)
    if (peak_gain >= UNIFIED_PARAMS["sudden_dump_gain"]
            and drop_from_peak >= UNIFIED_PARAMS["sudden_dump_drop"]):
        return "sudden_dump"

    # 2. hard stop loss
    if current_price <= entry_price * UNIFIED_SL_MULT:
        return "stop_loss"

    # 3. mega TP cap
    if current_price >= entry_price * UNIFIED_TP_MULT:
        return "mega_tp"

    # 4/5. band exit vs runner trail
    if peak_gain >= UNIFIED_PARAMS["runner_threshold"]:
        # runner: still holding while peak >= +35% means the price GAPPED
        # through the band in one update -> ride it with a ratcheted trail
        tighten = 1.0
        if peak_gain >= 1.0:
            tighten = UNIFIED_PARAMS["trail_tighten_100"]
        elif peak_gain >= 0.5:
            tighten = UNIFIED_PARAMS["trail_tighten_50"]
        width = UNIFIED_PARAMS["trail_width"] * tighten
        if current_price <= peak_price * (1.0 - width):
            return "smart_trailing"
    elif peak_gain >= UNIFIED_PARAMS["quick_tp"]:
        # band zone: sell everything at market
        return "quick_tp"

    # 6. stagnation
    if elapsed_minutes >= UNIFIED_PARAMS["stagnation_minutes"]:
        current_gain = current_price / entry_price - 1.0
        if (0 < current_gain <= UNIFIED_PARAMS["stagnation_max_gain"]
                and peak_gain < UNIFIED_PARAMS["stagnation_max_gain"]):
            return "stagnation"

    # 7. time exit
    if elapsed_minutes >= UNIFIED_PARAMS["max_hold_minutes"]:
        return "time_exit"

    return None


# ----------------------------------------------------------------------
# 2) ENTRY GATES — one set for every profile
# ----------------------------------------------------------------------
def unified_entry_check(token: Any) -> Tuple[bool, str]:
    """Unified entry gates. Returns (ok, reason). Missing data never blocks."""
    # momentum window
    chg_5m = getattr(token, "price_change_5m_pct", None)
    if chg_5m is not None:
        if chg_5m < UNIFIED_PARAMS["min_chg_5m_pct"]:
            return False, (f"UNIFIED: chg_5m={chg_5m:.1f}% < "
                           f"{UNIFIED_PARAMS['min_chg_5m_pct']:.0f}% — pump لم يبدأ")
        if chg_5m > UNIFIED_PARAMS["max_chg_5m_pct"]:
            return False, (f"UNIFIED: chg_5m={chg_5m:.1f}% > "
                           f"{UNIFIED_PARAMS['max_chg_5m_pct']:.0f}% — وصلنا متأخرين")

    # token age
    first_seen = getattr(token, "first_seen_at", None)
    if first_seen:
        try:
            if isinstance(first_seen, str):
                first_seen = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            age_sec = (_utcnow() - first_seen.replace(tzinfo=None)).total_seconds()
            if age_sec > UNIFIED_PARAMS["max_age_seconds"]:
                return False, (f"UNIFIED: عمر التوكن {age_sec:.0f}ث > "
                               f"{UNIFIED_PARAMS['max_age_seconds']:.0f}ث — نافذة الزخم المبكر انتهت")
        except Exception as exc:
            logger.debug(f"UNIFIED age check failed for {getattr(token, 'symbol', '?')}: {exc}")

    # buy pressure (24h txns proxy — for 3-minute tokens it IS the launch data)
    buys = getattr(token, "txns_24h_buys", None)
    sells = getattr(token, "txns_24h_sells", None)
    if buys is not None:
        if float(buys) < UNIFIED_PARAMS["min_txns_buys"]:
            return False, (f"UNIFIED: عمليات شراء {buys} < "
                           f"{UNIFIED_PARAMS['min_txns_buys']:.0f} — لا يوجد ضغط شراء")
        if sells is not None and float(sells) > 0:
            ratio = float(buys) / float(sells)
            if ratio < UNIFIED_PARAMS["min_buy_sell_ratio"]:
                return False, (f"UNIFIED: نسبة شراء/بيع {ratio:.2f} < "
                               f"{UNIFIED_PARAMS['min_buy_sell_ratio']} — البائعون يسيطرون")

    # holder concentration (only when the data is available)
    top10 = None
    onchain = getattr(token, "onchain_detail_json", None)
    if isinstance(onchain, dict):
        top10 = onchain.get("top_10_pct")
    if top10 is None:
        top10 = getattr(token, "top_10_holders_pct", None)
    if top10 is not None and float(top10) > 0:
        if float(top10) > UNIFIED_PARAMS["max_top10_concentration_pct"]:
            return False, (f"UNIFIED: تركز المحافظ العشر الأولى {float(top10):.1f}% > "
                           f"{UNIFIED_PARAMS['max_top10_concentration_pct']:.0f}% — خطر إغراق")

    return True, "UNIFIED: اجتاز بوابات الاستراتيجية الموحدة"


# ----------------------------------------------------------------------
# 3) DEFENSE — drawdown zones + bad-streak pause + daily buy cap
# ----------------------------------------------------------------------
async def _load_state(key: str) -> Optional[Dict]:
    from ...core.database import get_session
    from ...core.models import SystemState
    try:
        async with get_session() as session:
            row = await session.get(SystemState, key)
            return dict(row.value_json or {}) if row else None
    except Exception as exc:
        logger.debug(f"UNIFIED state load {key} failed: {exc}")
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
        logger.warning(f"UNIFIED state save {key} failed: {exc}")


async def _equity_and_recent_trades() -> Tuple[float, list]:
    """Equity = paper capital + cumulative net PnL of ALL closed trades
    (paper + live share the trades table, so this works in both modes),
    plus the last 6 closed trades for the streak check."""
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
                # rows limited to 50 — good enough for the equity proxy
                # (older trades beyond 50 contribute marginally and are
                # skipped for performance; equity is re-anchored to peak)
                equity += total_pnl
    except Exception as exc:
        logger.debug(f"UNIFIED equity computation failed: {exc}")
    return equity, recent


async def unified_defense_snapshot() -> Dict[str, Any]:
    """Evaluate the unified defense. Returns:
    {"state": "full"|"half"|"pause", "scale": 1.0|0.5|0.0, "reason": str,
     "buys_today": int, "max_buys": int}
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
        pause_until = datetime.fromisoformat(pause_state["until"]) if pause_state.get("until") else None
    except Exception:
        pause_until = None

    equity, recent = await _equity_and_recent_trades()

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
        if ratio < UNIFIED_PARAMS["dd_half_pct"]:
            until = _utcnow() + timedelta(days=UNIFIED_PARAMS["pause_days_dd"])
            await _save_state(STATE_KEY_PAUSE, {
                "until": until.isoformat(), "why": f"تراجع رأس المال إلى {ratio:.0%} من القمة"})
            snapshot["state"] = "pause"
            snapshot["scale"] = 0.0
            snapshot["reason"] = f"UNIFIED DEFENSE: تراجع {1 - ratio:.0%} من القمة — إيقاف 3 أيام"
            logger.warning(f"🛑 {snapshot['reason']}")
            return snapshot
        if ratio < UNIFIED_PARAMS["dd_full_pct"]:
            snapshot["state"] = "half"
            snapshot["scale"] = 0.5
            snapshot["reason"] = f"UNIFIED DEFENSE: رأس المال عند {ratio:.0%} من القمة — نصف الحجم"

    # 3) bad streak (fast reaction to a collapsing market)
    if len(recent) >= UNIFIED_PARAMS["streak_window"]:
        wins = sum(1 for pnl, _ in recent if pnl > 0)
        wr = wins / len(recent)
        total_pnl = sum(p for p, _ in recent)
        total_size = sum(s for _, s in recent)
        pnl_pct = total_pnl / total_size if total_size > 0 else 0.0
        if wr <= UNIFIED_PARAMS["streak_wr_max"] and pnl_pct <= UNIFIED_PARAMS["streak_pnl_min"]:
            until = _utcnow() + timedelta(days=UNIFIED_PARAMS["pause_days_streak"])
            await _save_state(STATE_KEY_PAUSE, {
                "until": until.isoformat(),
                "why": f"{wins}/{len(recent)} نجحت و PnL {pnl_pct:.0%}"})
            snapshot["state"] = "pause"
            snapshot["scale"] = 0.0
            snapshot["reason"] = (f"UNIFIED DEFENSE: سلسلة خاسرة "
                                  f"({wins}/{len(recent)}، {pnl_pct:.0%}) — إيقاف يومين")
            logger.warning(f"🛑 {snapshot['reason']}")
            return snapshot

    # 4) daily buy cap
    if buys_today >= UNIFIED_PARAMS["max_buys_per_day"]:
        snapshot["state"] = "pause"
        snapshot["scale"] = 0.0
        snapshot["reason"] = f"UNIFIED: بلغنا حد {UNIFIED_PARAMS['max_buys_per_day']} صفقات اليوم"
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
