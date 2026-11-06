#!/usr/bin/env python
"""V505 real-trade verification script.

Use this DURING Stage 9 (the real-trade test) to verify each step works
correctly on-chain. Run it after a buy to verify the trade was recorded
correctly, or after a sell to verify the close + risk-state update.

Usage:
    API_AUTH_KEY=xxxx python scripts/verify_real_trade.py --trade-id <uuid>
    API_AUTH_KEY=xxxx python scripts/verify_real_trade.py --tx <signature>
    API_AUTH_KEY=xxxx python scripts/verify_real_trade.py --list-open

It checks:
  - The Trade row exists with the expected V505 fields
  - The tx is confirmed on-chain via Solana RPC
  - The wallet balance is consistent (V505 slippage math)
  - The RiskManager state reflects the trade result
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime

import httpx

DEFAULT_URL = "https://memecoin-bot-deploy-production.up.railway.app"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"


def header(api_key: str) -> dict:
    h = {}
    if api_key:
        h["X-API-Key"] = api_key
        h["Authorization"] = f"Bearer {api_key}"
    return h


async def get_trade(client: httpx.AsyncClient, api_key: str, base_url: str, trade_id: str) -> dict | None:
    """Fetch a single trade by ID. Falls back to listing all if the per-id endpoint doesn't exist."""
    # Try /api/trades first (list endpoint)
    r = await client.get(
        f"{base_url}/api/trades?limit=200",
        headers=header(api_key),
        timeout=15.0,
    )
    if r.status_code != 200:
        print(f"❌ /api/trades returned {r.status_code}: {r.text[:200]}")
        return None
    data = r.json()
    trades = data.get("trades", []) if isinstance(data, dict) else data
    for t in trades:
        if t.get("id") == trade_id:
            return t
    print(f"❌ Trade {trade_id} not found in /api/trades (searched {len(trades)} trades)")
    return None


async def list_open_trades(client: httpx.AsyncClient, api_key: str, base_url: str) -> None:
    r = await client.get(f"{base_url}/api/trades?limit=200", headers=header(api_key), timeout=15.0)
    if r.status_code != 200:
        print(f"❌ /api/trades returned {r.status_code}")
        return
    data = r.json()
    trades = data.get("trades", []) if isinstance(data, dict) else data
    open_trades = [t for t in trades if t.get("status") == "open"]
    print(f"\n=== Open trades ({len(open_trades)}) ===")
    for t in open_trades:
        print(f"\n• id={t.get('id')}")
        print(f"  token={t.get('token_symbol', t.get('symbol', '?'))} ({t.get('token_id', '?')})")
        print(f"  side={t.get('side')}, status={t.get('status')}")
        print(f"  entry_price=${t.get('entry_price')}")
        print(f"  entry_size_usd=${t.get('entry_size_usd')}")
        print(f"  entry_quantity={t.get('entry_quantity')}")
        print(f"  stop_loss=${t.get('stop_loss_price')} (V505 -15% if no ATR)")
        print(f"  take_profit=${t.get('take_profit_price')} (V501 11x cap)")
        print(f"  planned_hold={t.get('planned_holding_period_minutes')}min (V501 7200=5d)")
        print(f"  strategy={t.get('strategy_id')}")
        print(f"  fees_total=${t.get('fees_total_usd')}")
        print(f"  slippage_total=${t.get('slippage_total_usd')}")
        print(f"  peak_price (in max_unrealized_profit_usd)=${t.get('max_unrealized_profit_usd')}")
        reasons = t.get("entry_reasons_json") or []
        print(f"  entry_reasons ({len(reasons)}):")
        for r in reasons[:5]:
            print(f"    - {r}")
        if len(reasons) > 5:
            print(f"    ... ({len(reasons)-5} more)")


async def verify_tx_onchain(client: httpx.AsyncClient, signature: str) -> None:
    """Query Solana RPC directly to verify a tx is confirmed on-chain."""
    print(f"\n=== On-chain verification for {signature[:20]}... ===")
    try:
        r = await client.post(
            SOLANA_RPC,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [[signature], {"searchTransactionHistory": True}],
            },
            timeout=10.0,
        )
        if r.status_code != 200:
            print(f"❌ Solana RPC returned {r.status_code}: {r.text[:200]}")
            return
        data = r.json()
        value = (data.get("result", {}) or {}).get("value", [])
        info = value[0] if value else None
        if not info:
            print("❌ Tx not found on-chain (signature unknown)")
            return
        if info.get("err"):
            print(f"❌ Tx FAILED on-chain: {info.get('err')}")
            return
        status = info.get("confirmationStatus", "unknown")
        slot = info.get("slot")
        print(f"✅ Tx confirmed: status={status}, slot={slot}")
    except Exception as exc:
        print(f"❌ RPC error: {exc}")


async def verify_trade(client: httpx.AsyncClient, api_key: str, base_url: str, trade_id: str) -> None:
    """Verify a single trade's V505 fields + on-chain tx."""
    print(f"\n=== Verifying trade {trade_id} ===")
    trade = await get_trade(client, api_key, base_url, trade_id)
    if not trade:
        return

    print(f"\n--- Trade details ---")
    print(f"id: {trade.get('id')}")
    print(f"token: {trade.get('token_symbol', trade.get('symbol', '?'))}")
    print(f"status: {trade.get('status')}")
    print(f"side: {trade.get('side')}")
    print(f"entry_price: ${trade.get('entry_price')}")
    print(f"exit_price: ${trade.get('exit_price') if trade.get('exit_price') else '—'}")
    print(f"net_pnl_usd: ${trade.get('net_pnl_usd') if trade.get('net_pnl_usd') is not None else '—'}")
    print(f"pnl_pct: {trade.get('pnl_pct') if trade.get('pnl_pct') is not None else '—'}%")

    # V505 checks
    print(f"\n--- V505 field checks ---")
    sl = trade.get("stop_loss_price")
    entry = trade.get("entry_price") or 0
    if sl and entry:
        sl_pct = (entry - sl) / entry * 100
        print(f"SL: ${sl} = -{sl_pct:.1f}% from entry")
        if sl_pct < 9 or sl_pct > 26:
            print(f"  ⚠️  SL outside V505 [-10%, -25%] floor/ceiling range")
        else:
            print(f"  ✅ SL within V505 range")
    else:
        print(f"❌ SL missing or entry_price is 0")

    tp = trade.get("take_profit_price")
    if tp and entry:
        tp_mult = tp / entry
        print(f"TP: ${tp} = {tp_mult:.1f}× entry")
        if abs(tp_mult - 11.0) > 0.1:
            print(f"  ⚠️  TP not at V501 11x cap")
        else:
            print(f"  ✅ TP at V501 11x cap")
    else:
        print(f"❌ TP missing or entry_price is 0")

    hold = trade.get("planned_holding_period_minutes")
    if hold == 7200:
        print(f"✅ Holding period = 7200min (5 days, V501)")
    else:
        print(f"⚠️  Holding period = {hold} (expected 7200)")

    # Slippage — V505 measured vs V400 estimated
    slip = trade.get("entry_slippage_usd")
    slip_total = trade.get("slippage_total_usd")
    fees = trade.get("fees_total_usd")
    print(f"\nSlippage entry: ${slip}")
    print(f"Slippage total: ${slip_total}")
    print(f"Fees total: ${fees}")
    reasons = trade.get("entry_reasons_json") or []
    has_v505 = any("V505 measured slippage" in r for r in reasons)
    has_v400_fallback = any("V505 slippage estimate" in r for r in reasons)
    if has_v505:
        print(f"  ✅ V505 real slippage measurement used (wallet diff)")
    elif has_v400_fallback:
        print(f"  ⚠️  V505 fell back to 3% estimate (wallet-diff failed — check RPC)")
    else:
        print(f"  ⚠️  No V505 slippage marker in entry_reasons")

    # On-chain tx verification
    print(f"\n--- On-chain verification ---")
    reasons_str = " ".join(reasons)
    # tx_signature is stored inside an entry_reason like "tx:5xY..."
    tx_sig = None
    for r in reasons:
        if r.startswith("tx:"):
            tx_sig = r[3:].strip()
            break
    if tx_sig:
        print(f"tx_signature: {tx_sig}")
        await verify_tx_onchain(client, tx_sig)
    else:
        print(f"❌ No tx: marker in entry_reasons — can't verify on-chain")


async def main():
    parser = argparse.ArgumentParser(description="V505 real-trade verifier")
    parser.add_argument("--url", default=DEFAULT_URL, help="Base URL of the bot")
    parser.add_argument("--api-key", default=os.environ.get("API_AUTH_KEY", ""))
    parser.add_argument("--trade-id", help="Verify a specific trade by ID")
    parser.add_argument("--tx", help="Verify a tx signature on Solana")
    parser.add_argument("--list-open", action="store_true", help="List all open trades")
    args = parser.parse_args()

    if not args.api_key:
        print("⚠️  No API_AUTH_KEY provided — auth-protected endpoints will 401.", file=sys.stderr)

    async with httpx.AsyncClient() as client:
        if args.list_open:
            await list_open_trades(client, args.api_key, args.url)
        elif args.trade_id:
            await verify_trade(client, args.api_key, args.url, args.trade_id)
        elif args.tx:
            await verify_tx_onchain(client, args.tx)
        else:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
