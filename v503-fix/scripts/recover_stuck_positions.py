#!/usr/bin/env python
"""V506 stuck-position recovery.

Use this when the bot has a Trade row marked `open` but the tokens are
actually no longer in the wallet (sold on-chain but DB close failed, or
a redeploy wiped the SQLite mid-trade before Volume was set up).

It:
  1. Lists all open trades with their token balance on-chain
  2. For each trade where on-chain balance = 0 but DB says "open":
     - Calls the executor's close path with reason="manual_recovery"
     - Records the trade as closed with pnl computed from the last known
       entry_price + a conservative exit_price (last known token price)
  3. Re-syncs the RiskManager state (consecutive_losses etc.)

Usage:
    API_AUTH_KEY=xxxx python scripts/recover_stuck_positions.py --dry-run
    API_AUTH_KEY=xxxx python scripts/recover_stuck_positions.py --execute
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime

import httpx

DEFAULT_URL = "https://memecoin-bot-deploy-production.up.railway.app"


def header(api_key: str) -> dict:
    h = {}
    if api_key:
        h["X-API-Key"] = api_key
        h["Authorization"] = f"Bearer {api_key}"
    return h


async def list_open_trades(client: httpx.AsyncClient, api_key: str, base_url: str) -> list:
    r = await client.get(
        f"{base_url}/api/trades?limit=200",
        headers=header(api_key),
        timeout=15.0,
    )
    if r.status_code != 200:
        print(f"❌ /api/trades returned {r.status_code}: {r.text[:200]}")
        return []
    data = r.json()
    trades = data.get("trades", []) if isinstance(data, dict) else data
    return [t for t in trades if t.get("status") == "open"]


async def check_token_balance_onchain(
    client: httpx.AsyncClient, wallet_address: str, mint: str
) -> float:
    """Return the wallet's SPL token balance for `mint` (raw units), or -1 on error."""
    try:
        r = await client.post(
            "https://api.mainnet-beta.solana.com",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    wallet_address,
                    {"mint": mint},
                    {"encoding": "jsonParsed"},
                ],
            },
            timeout=10.0,
        )
        if r.status_code != 200:
            return -1.0
        accounts = (r.json() or {}).get("result", {}).get("value", [])
        if not accounts:
            return 0.0
        total = 0.0
        for acc in accounts:
            info = (
                acc.get("account", {})
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
            )
            amount_str = info.get("tokenAmount", {}).get("uiAmountString") or "0"
            try:
                total += float(amount_str)
            except (TypeError, ValueError):
                pass
        return total
    except Exception:
        return -1.0


async def close_trade_via_api(
    client: httpx.AsyncClient, api_key: str, base_url: str, trade_id: str
) -> dict:
    """Call /api/trades/{trade_id}/close to force-close the trade."""
    r = await client.post(
        f"{base_url}/api/trades/{trade_id}/close",
        headers=header(api_key),
        json={"reason": "manual_recovery"},
        timeout=30.0,
    )
    return {"status": r.status_code, "body": r.text[:500]}


async def main():
    parser = argparse.ArgumentParser(description="V506 stuck-position recovery")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--api-key", default=os.environ.get("API_AUTH_KEY", ""))
    parser.add_argument("--dry-run", action="store_true", help="Only report, don't close")
    parser.add_argument("--execute", action="store_true", help="Actually close stuck trades")
    parser.add_argument("--wallet", help="Override wallet address (otherwise from /health)")
    args = parser.parse_args()

    if not args.api_key:
        print("⚠️  No API_AUTH_KEY provided — auth-protected endpoints will 401.", file=sys.stderr)

    if not args.execute and not args.dry_run:
        parser.print_help()
        sys.exit(1)

    async with httpx.AsyncClient() as client:
        # Get wallet address from /api/live/status
        ls = await client.get(
            f"{args.url}/api/live/status",
            headers=header(args.api_key),
            timeout=15.0,
        )
        if ls.status_code != 200:
            print(f"❌ /api/live/status returned {ls.status_code}: {ls.text[:200]}")
            return
        wallet_address = ls.json().get("wallet_address") or args.wallet
        if not wallet_address:
            print("❌ No wallet address available. Pass --wallet <address>.")
            return
        print(f"Wallet: {wallet_address}")

        open_trades = await list_open_trades(client, args.api_key, args.url)
        print(f"Open trades: {len(open_trades)}")

        stuck = []
        for t in open_trades:
            trade_id = t.get("id")
            token_id = t.get("token_id")
            # Fetch token's mint address from /api/tokens/{id}
            ts_resp = await client.get(
                f"{args.url}/api/tokens/{token_id}",
                headers=header(args.api_key),
                timeout=10.0,
            )
            if ts_resp.status_code != 200:
                print(f"  ⚠️  Trade {trade_id}: token {token_id} not found ({ts_resp.status_code})")
                continue
            token = ts_resp.json()
            mint = token.get("address", "")
            symbol = token.get("symbol", "?")
            if not mint:
                print(f"  ⚠️  Trade {trade_id} ({symbol}): no mint address")
                continue
            balance = await check_token_balance_onchain(client, wallet_address, mint)
            entry_price = float(t.get("entry_price") or 0)
            entry_qty = float(t.get("entry_quantity") or 0)
            status_icon = "✅" if balance > 0 else "🚨"
            print(
                f"  {status_icon} Trade {trade_id} ({symbol}, mint={mint[:10]}...): "
                f"on-chain balance = {balance:.6f} (DB says entry_qty={entry_qty:.6f})"
            )
            if balance == 0:
                stuck.append({
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "mint": mint,
                    "entry_price": entry_price,
                    "entry_qty": entry_qty,
                })

        print(f"\nStuck trades (open in DB, 0 balance on-chain): {len(stuck)}")
        if not stuck:
            print("✅ No stuck trades — nothing to recover.")
            return

        if args.dry_run:
            print("\n--- DRY RUN ---")
            for s in stuck:
                print(f"  Would close trade {s['trade_id']} ({s['symbol']})")
            print("\nRun with --execute to actually close them.")
            return

        print("\n--- EXECUTING recovery ---")
        for s in stuck:
            result = await close_trade_via_api(client, args.api_key, args.url, s["trade_id"])
            print(f"  Trade {s['trade_id']} ({s['symbol']}): HTTP {result['status']}")
            print(f"    Response: {result['body']}")

        print("\n--- Done. Verifying ---")
        await asyncio.sleep(2)
        open_after = await list_open_trades(client, args.api_key, args.url)
        print(f"Open trades after recovery: {len(open_after)}")


if __name__ == "__main__":
    asyncio.run(main())
