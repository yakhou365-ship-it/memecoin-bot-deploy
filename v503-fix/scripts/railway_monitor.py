#!/usr/bin/env python
"""Railway paper-mode monitor (V505).

Polls the deployed bot every N minutes and writes a structured report.
Run with the API key for your bot in the API_AUTH_KEY env var:

    API_AUTH_KEY=xxxx python scripts/railway_monitor.py --interval 60 --hours 24

The report is written to scripts/railway_monitor_report.json and printed
to stdout in a compact line per poll.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

DEFAULT_URL = "https://memecoin-bot-deploy-production.up.railway.app"
REPORT_PATH = Path(__file__).resolve().parent / "railway_monitor_report.json"


async def fetch_json(client: httpx.AsyncClient, api_key: str, path: str) -> dict | None:
    """GET path with API key, return JSON or None on error.

    Skips the auth headers entirely when ``api_key`` is empty (so the
    request doesn't crash httpx with an illegal "Bearer " header value).
    """
    headers: dict = {}
    if api_key:
        headers["X-API-Key"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = await client.get(path, headers=headers, timeout=15.0)
        if r.status_code == 401:
            # Retry without auth — maybe auth is disabled (paper mode local).
            r = await client.get(path, timeout=15.0)
        if r.status_code == 200:
            return r.json()
        return {"_http_error": r.status_code, "_body": r.text[:200]}
    except Exception as exc:
        return {"_exception": str(exc)[:200]}


async def poll_once(client: httpx.AsyncClient, api_key: str, base_url: str) -> dict:
    """One poll cycle — fetch all the relevant endpoints."""
    ts = datetime.now(timezone.utc).isoformat()
    out: dict = {"ts": ts}

    # /health (no auth)
    h = await fetch_json(client, "", f"{base_url}/health")
    out["bot_status"] = h.get("status") if h else "down"
    out["trading_mode"] = h.get("trading_mode") if h else None
    out["live_trading"] = h.get("live_trading") if h else None

    # /api/trading/state
    ts_resp = await fetch_json(client, api_key, f"{base_url}/api/trading/state")
    if ts_resp and "_http_error" not in ts_resp:
        out["trading_active"] = ts_resp.get("is_active")
    else:
        out["trading_active"] = None

    # /api/risk/state
    rs = await fetch_json(client, api_key, f"{base_url}/api/risk/state")
    if rs and "_http_error" not in rs:
        out["open_positions_count"] = rs.get("open_positions_count")
        out["daily_pnl_pct"] = round(rs.get("daily_pnl_pct", 0) or 0, 4)
        out["weekly_pnl_pct"] = round(rs.get("weekly_pnl_pct", 0) or 0, 4)
        out["max_drawdown_pct"] = round(rs.get("max_drawdown_pct", 0) or 0, 4)
        out["circuit_breaker_active"] = rs.get("circuit_breaker_active")
        out["cooldown_active"] = rs.get("cooldown_active")
        out["consecutive_losses"] = rs.get("consecutive_losses")
    else:
        out["risk_state_error"] = rs

    # /api/tokens (count)
    tk = await fetch_json(client, api_key, f"{base_url}/api/tokens?limit=200")
    if tk and "_http_error" not in tk:
        if isinstance(tk, dict):
            out["tokens_discovered"] = len(tk.get("tokens", []))
        elif isinstance(tk, list):
            out["tokens_discovered"] = len(tk)
    else:
        out["tokens_discovered"] = None

    # /api/trades (count)
    tr = await fetch_json(client, api_key, f"{base_url}/api/trades?limit=200")
    if tr and "_http_error" not in tr:
        if isinstance(tr, dict):
            trades = tr.get("trades", [])
        elif isinstance(tr, list):
            trades = tr
        else:
            trades = []
        out["trades_total"] = len(trades)
        open_n = sum(1 for t in trades if t.get("status") == "open")
        closed_n = sum(1 for t in trades if t.get("status") == "closed")
        out["trades_open"] = open_n
        out["trades_closed"] = closed_n
    else:
        out["trades_total"] = None

    # /api/pumpfun/wallet/balance
    wb = await fetch_json(client, api_key, f"{base_url}/api/pumpfun/wallet/balance")
    if wb and "_http_error" not in wb:
        out["wallet_sol"] = wb.get("wallet_sol")
        out["sol_price_usd"] = wb.get("sol_price_usd")
    else:
        out["wallet_sol"] = None
        out["sol_price_usd"] = None

    return out


def print_compact(poll: dict) -> None:
    """One-line summary of the poll for live monitoring."""
    ts = poll["ts"][11:19]
    status = poll.get("bot_status", "?")
    mode = poll.get("trading_mode", "?")
    active = "▶" if poll.get("trading_active") else "⏸"
    pos = poll.get("open_positions_count", "?")
    pnl_d = poll.get("daily_pnl_pct", "?")
    pnl_w = poll.get("weekly_pnl_pct", "?")
    dd = poll.get("max_drawdown_pct", "?")
    cb = "🛑" if poll.get("circuit_breaker_active") else "✅"
    cd = "❄" if poll.get("cooldown_active") else ""
    losses = poll.get("consecutive_losses", 0)
    tokens = poll.get("tokens_discovered", "?")
    trades = poll.get("trades_total", "?")
    sol = poll.get("sol_price_usd") or "—"
    sol_str = f"${sol:.2f}" if isinstance(sol, (int, float)) else str(sol)

    print(
        f"[{ts}] {status} mode={mode} {active} "
        f"pos={pos} tokens={tokens} trades={trades} "
        f"daily={pnl_d}% wk={pnl_w}% dd={dd}% "
        f"CB={cb} {cd} losses={losses} SOL={sol_str}",
        flush=True,
    )


async def main():
    parser = argparse.ArgumentParser(description="Railway bot monitor")
    parser.add_argument("--url", default=DEFAULT_URL, help="Base URL of the bot")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds")
    parser.add_argument("--hours", type=float, default=24.0, help="Total monitoring duration")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("API_AUTH_KEY", ""),
        help="API auth key (or set API_AUTH_KEY env var)",
    )
    args = parser.parse_args()

    if not args.api_key:
        print("⚠️  No API_AUTH_KEY provided — auth-protected endpoints will 401.", file=sys.stderr)
        print("    Set it via --api-key or API_AUTH_KEY env var.", file=sys.stderr)

    print(f"Railway monitor — polling {args.url} every {args.interval}s for {args.hours}h")
    print(f"Report will be saved to: {REPORT_PATH}")
    print(f"{'='*80}")

    polls: list[dict] = []
    end_time = time.time() + args.hours * 3600

    async with httpx.AsyncClient() as client:
        n = 0
        while time.time() < end_time:
            n += 1
            try:
                poll = await poll_once(client, args.api_key, args.url)
                polls.append(poll)
                print_compact(poll)
            except Exception as exc:
                polls.append({"ts": datetime.now(timezone.utc).isoformat(), "_error": str(exc)[:200]})
                print(f"[{n}] poll error: {exc}", flush=True)
            REPORT_PATH.write_text(json.dumps(polls, indent=2, default=str))
            if time.time() >= end_time:
                break
            await asyncio.sleep(args.interval)

    print(f"\n{'='*80}")
    print(f"Done. {len(polls)} polls saved to {REPORT_PATH}")

    if polls:
        print(f"\n--- Summary ---")
        ok_polls = [p for p in polls if p.get("bot_status") == "ok"]
        print(f"Bot OK polls: {len(ok_polls)}/{len(polls)}")
        if ok_polls:
            last = ok_polls[-1]
            print(f"Last state: trading_active={last.get('trading_active')}, "
                  f"open_positions={last.get('open_positions_count')}, "
                  f"tokens_discovered={last.get('tokens_discovered')}, "
                  f"trades_total={last.get('trades_total')}")
            print(f"Risk: daily_pnl={last.get('daily_pnl_pct')}%, "
                  f"weekly_pnl={last.get('weekly_pnl_pct')}%, "
                  f"max_drawdown={last.get('max_drawdown_pct')}%, "
                  f"circuit_breaker={last.get('circuit_breaker_active')}, "
                  f"cooldown={last.get('cooldown_active')}, "
                  f"losses={last.get('consecutive_losses')}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Report saved.")
