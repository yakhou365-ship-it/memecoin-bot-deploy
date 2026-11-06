"""
FOMO Trading App API Client.
=============================

Provides access to FOMO (fomo.family) - the social-first crypto trading app
with $4B+ volume and 625k+ users across Solana, Base, BNB, and Monad chains.

KEY FINDING (Bitquery, Sept 2026):
- FOMO does NOT publish a public REST API on fomo.family
- All FOMO trades on Solana go through a SINGLE signer wallet:
    Signer/Fee Payer: AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51
- FOMO fees collected in USDC at: HrTf9CzXR1dRH4Sof5QrpmGWwpwAf3qZzwCsEjQpXcSq
- Most trades (~85%) go through the DFlow aggregator
- 925k-1.21M FOMO trades/day on Solana (Sept 2026)

This client reads FOMO trades directly from the Solana blockchain via Bitquery
GraphQL API. We can:
1. Get latest FOMO trades (real-time memecoin activity)
2. Get top tokens being bought on FOMO (trending memecoins)
3. Get most active wallets on FOMO (potential copy-trading targets)
4. Get one wallet's FOMO trade history (copy-trade analysis)
5. Track fees paid to FOMO (volume proxy)

To execute copy trades, the bot uses its own Solana wallet to mirror
top traders' positions on Pump.fun / Raydium / Jupiter.

AUTH:
- Bitquery API key required (https://account.bitquery.io)
- Set as env: BITQUERY_API_KEY

NO MEMECOIN BOT WALLET NEEDED for read-only operations.
For trade execution (copy trading), use PumpPortalClient or JupiterClient.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import httpx

from ...core.config import settings
from ...core.logger import logger


# FOMO on-chain addresses (identified by Bitquery, Sept 2026)
FOMO_SIGNER_WALLET = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
FOMO_FEE_ACCOUNT_USDC = "HrTf9CzXR1dRH4Sof5QrpmGWwpwAf3qZzwCsEjQpXcSq"
FOMO_FEE_ACCOUNT_OWNER = "R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX"

# Bitquery GraphQL endpoint
BITQUERY_GRAPHQL_URL = "https://graphql.bitquery.io"

# Common tokens to exclude from "top tokens" queries
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_MINT = "So11111111111111111111111111111111111111112"


# ============================================================
# Data classes
# ============================================================
@dataclass
class FomoTrade:
    """A single FOMO trade executed on Solana."""
    signature: str
    block_time: int  # unix timestamp
    buyer_wallet: str
    buy_currency_symbol: str
    buy_currency_mint: str
    buy_amount: float
    buy_amount_usd: float
    sell_currency_symbol: str
    sell_currency_mint: str
    sell_amount: float
    sell_amount_usd: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FomoTopToken:
    """A token being bought heavily on FOMO."""
    symbol: str
    name: str
    mint_address: str
    buys_count: int
    buyers_count: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FomoActiveWallet:
    """A wallet that's trading actively on FOMO (potential copy-trade target)."""
    wallet_address: str
    trades_count: int
    tokens_traded: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FomoStats:
    """FOMO platform stats summary."""
    trades_last_hour: int = 0
    traders_last_hour: int = 0
    fee_payments_last_hour: int = 0
    paying_wallets_last_hour: int = 0
    usdc_fees_last_hour: float = 0.0
    trades_last_24h: int = 0
    tokens_last_24h: int = 0
    is_signer_active: bool = False
    last_signer_activity: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# GraphQL queries (from official Bitquery docs)
# ============================================================
LATEST_TRADES_QUERY_TEMPLATE = """
query FomoLatestTrades {
  Solana {
    DEXTrades(
      limit: {count: %d}
      orderBy: {descending: Block_Time}
      where: {Transaction: {Signer: {is: "%s"}}, Trade: {Dex: {ProtocolName: {is: "dflow"}}}}
    ) {
      Block { Time }
      Transaction { Signature }
      Trade {
        Buy {
          Amount
          AmountInUSD
          Account { Owner }
          Currency { Symbol MintAddress }
        }
        Sell {
          Amount
          AmountInUSD
          Currency { Symbol MintAddress }
        }
      }
    }
  }
}
"""

WALLET_TRADES_QUERY_TEMPLATE = """
query FomoWalletTrades($wallet: String) {
  Solana {
    DEXTrades(
      limit: {count: %d}
      orderBy: {descending: Block_Time}
      where: {Transaction: {Signer: {is: "%s"}}, Trade: {Dex: {ProtocolName: {is: "dflow"}}, Buy: {Account: {Owner: {is: $wallet, not: ""}}}}}
    ) {
      Block { Time }
      Transaction { Signature }
      Trade {
        Buy {
          Amount
          Currency { Symbol MintAddress }
        }
        Sell {
          Amount
          Currency { Symbol MintAddress }
        }
      }
    }
  }
}
"""

TOP_TOKENS_BOUGHT_QUERY_TEMPLATE = """
query FomoTopTokensBought {
  Solana {
    DEXTradeByTokens(
      limit: {count: %d}
      orderBy: {descendingByField: "buyers"}
      where: {
        Transaction: {Signer: {is: "%s"}},
        Trade: {
          Dex: {ProtocolName: {is: "dflow"}},
          Side: {Type: {is: buy}},
          Currency: {MintAddress: {notIn: ["%s", "%s"]}}
        },
        Block: {Time: {since_relative: {hours_ago: %d}}}
      }
    ) {
      Trade {
        Currency { Symbol Name MintAddress }
      }
      buys: count(distinct: Transaction_Signature)
      buyers: count(distinct: Trade_Account_Owner)
    }
  }
}
"""

MOST_ACTIVE_WALLETS_QUERY_TEMPLATE = """
query FomoMostActiveWallets {
  Solana {
    DEXTradeByTokens(
      limit: {count: %d}
      orderBy: {descendingByField: "trades"}
      where: {
        Transaction: {Signer: {is: "%s"}},
        Trade: {Dex: {ProtocolName: {is: "dflow"}}},
        Block: {Time: {since_relative: {hours_ago: %d}}}
      }
    ) {
      Trade { Account { Owner } }
      trades: count(distinct: Transaction_Signature)
      tokens: count(distinct: Trade_Currency_MintAddress)
    }
  }
}
"""

# Signer check query (no parameters needed)
SIGNER_CHECK_QUERY = """
query FomoSignerCheck {
  Solana {
    Transactions(
      limit: {count: 1}
      orderBy: {descending: Block_Time}
      where: {Transaction: {Signer: {is: "%s"}}}
    ) {
      Block { Time }
      Transaction { Signature }
    }
  }
}
""" % FOMO_SIGNER_WALLET


# ============================================================
# FOMO Client
# ============================================================
class FomoClient:
    """
    FOMO trading app client.

    Reads FOMO trades from Solana blockchain via Bitquery GraphQL API.
    Provides:
    - Latest FOMO trades (real-time memecoin activity)
    - Top tokens being bought (trending memecoins on FOMO)
    - Most active wallets (potential copy-trading targets)
    - Per-wallet trade history (analyze a trader's behavior)
    - Platform stats (volume proxy via fee payments)

    AUTH:
    - Bitquery API key required
    - Get one free at: https://account.bitquery.io
    - Set as env: BITQUERY_API_KEY=your_key
    """

    def __init__(self):
        self.api_key = getattr(settings, "bitquery_api_key", "") or ""
        self.base_url = BITQUERY_GRAPHQL_URL
        self._client: Optional[httpx.AsyncClient] = None
        self._cached_stats: Optional[FomoStats] = None
        self._stats_last_fetch: float = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if self.api_key:
                headers["X-API-KEY"] = self.api_key
            self._client = httpx.AsyncClient(timeout=20.0, headers=headers)
        return self._client

    async def _run_query(self, query: str, variables: Optional[Dict] = None) -> Optional[Dict]:
        """Execute a GraphQL query against Bitquery API."""
        if not self.api_key:
            logger.warning("BITQUERY_API_KEY not set - FOMO API calls will fail. Get a free key at https://account.bitquery.io")
            # Return empty data so the bot doesn't crash
            return None

        client = await self._get_client()
        try:
            payload = {"query": query}
            if variables:
                payload["variables"] = variables

            resp = await client.post(self.base_url, json=payload)
            if resp.status_code != 200:
                logger.debug(f"Bitquery HTTP {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            if "errors" in data:
                logger.debug(f"Bitquery GraphQL errors: {data['errors'][:1]}")
                return None
            return data.get("data")
        except Exception as exc:
            logger.debug(f"Bitquery query failed: {exc}")
            return None

    # ========================================================
    # Public API
    # ========================================================
    async def is_signer_active(self) -> bool:
        """Check if FOMO's signer wallet is still in use (sanity check)."""
        data = await self._run_query(SIGNER_CHECK_QUERY)
        if not data or not data.get("Solana", {}).get("Transactions"):
            return False
        txs = data["Solana"]["Transactions"]
        if not txs:
            return False
        last_tx = txs[0]
        last_time = last_tx.get("Block", {}).get("Time", "")
        # If the last tx was within the last hour, signer is active
        if last_time:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
                now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.utcnow()
                diff_seconds = (now - dt).total_seconds()
                return diff_seconds < 3600  # Active if last tx < 1 hour ago
            except Exception:
                return True  # Assume active if we can't parse time
        return True

    async def get_latest_trades(self, limit: int = 20) -> List[FomoTrade]:
        """
        Get the latest trades made on FOMO.

        Returns trades with:
        - buyer_wallet (the user, not FOMO's signer)
        - token bought (mint + symbol + amount + USD value)
        - token sold (usually USDC)
        - tx signature
        - block time
        """
        query = LATEST_TRADES_QUERY_TEMPLATE % (min(limit, 100), FOMO_SIGNER_WALLET)
        data = await self._run_query(query)
        if not data:
            return []

        trades = []
        for t in data.get("Solana", {}).get("DEXTrades", []):
            try:
                block_time_str = t.get("Block", {}).get("Time", "")
                block_time = 0
                if block_time_str:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(block_time_str.replace("Z", "+00:00"))
                        block_time = int(dt.timestamp())
                    except Exception:
                        pass

                buy = t.get("Trade", {}).get("Buy", {})
                sell = t.get("Trade", {}).get("Sell", {})
                buy_currency = buy.get("Currency", {}) or {}
                sell_currency = sell.get("Currency", {}) or {}

                trades.append(FomoTrade(
                    signature=t.get("Transaction", {}).get("Signature", ""),
                    block_time=block_time,
                    buyer_wallet=buy.get("Account", {}).get("Owner", ""),
                    buy_currency_symbol=buy_currency.get("Symbol", ""),
                    buy_currency_mint=buy_currency.get("MintAddress", ""),
                    buy_amount=float(buy.get("Amount", 0) or 0),
                    buy_amount_usd=float(buy.get("AmountInUSD", 0) or 0),
                    sell_currency_symbol=sell_currency.get("Symbol", ""),
                    sell_currency_mint=sell_currency.get("MintAddress", ""),
                    sell_amount=float(sell.get("Amount", 0) or 0),
                    sell_amount_usd=float(sell.get("AmountInUSD", 0) or 0),
                ))
            except Exception as exc:
                logger.debug(f"FOMO trade parse failed: {exc}")
                continue

        return trades[:limit]

    async def get_top_tokens_bought(
        self, hours: int = 1, limit: int = 20
    ) -> List[FomoTopToken]:
        """
        Get the top tokens being bought on FOMO in the last N hours.

        Excludes USDC and WSOL (the quote currencies).
        """
        query = TOP_TOKENS_BOUGHT_QUERY_TEMPLATE % (
            min(limit, 100), FOMO_SIGNER_WALLET, USDC_MINT, WSOL_MINT, hours
        )
        data = await self._run_query(query)
        if not data:
            return []

        tokens = []
        for t in data.get("Solana", {}).get("DEXTradeByTokens", []):
            try:
                currency = t.get("Trade", {}).get("Currency", {}) or {}
                tokens.append(FomoTopToken(
                    symbol=currency.get("Symbol", ""),
                    name=currency.get("Name", ""),
                    mint_address=currency.get("MintAddress", ""),
                    buys_count=int(t.get("buys", 0) or 0),
                    buyers_count=int(t.get("buyers", 0) or 0),
                ))
            except Exception:
                continue

        return tokens[:limit]

    async def get_most_active_wallets(
        self, hours: int = 1, limit: int = 20
    ) -> List[FomoActiveWallet]:
        """
        Get the most active wallets on FOMO in the last N hours.
        These are potential copy-trading targets - they're trading frequently.
        """
        query = MOST_ACTIVE_WALLETS_QUERY_TEMPLATE % (
            min(limit, 100), FOMO_SIGNER_WALLET, hours
        )
        data = await self._run_query(query)
        if not data:
            return []

        wallets = []
        for t in data.get("Solana", {}).get("DEXTradeByTokens", []):
            try:
                wallet_addr = t.get("Trade", {}).get("Account", {}).get("Owner", "")
                if not wallet_addr or wallet_addr == FOMO_SIGNER_WALLET:
                    continue
                wallets.append(FomoActiveWallet(
                    wallet_address=wallet_addr,
                    trades_count=int(t.get("trades", 0) or 0),
                    tokens_traded=int(t.get("tokens", 0) or 0),
                ))
            except Exception:
                continue

        # Sort by trades count descending
        wallets.sort(key=lambda w: -w.trades_count)
        return wallets[:limit]

    async def get_wallet_trades(
        self, wallet_address: str, limit: int = 20
    ) -> List[FomoTrade]:
        """
        Get a specific wallet's trade history on FOMO.
        Useful for analyzing a top trader's behavior before copy-trading.
        """
        query = WALLET_TRADES_QUERY_TEMPLATE % (min(limit, 100), FOMO_SIGNER_WALLET)
        data = await self._run_query(
            query,
            variables={"wallet": wallet_address},
        )
        if not data:
            return []

        trades = []
        for t in data.get("Solana", {}).get("DEXTrades", []):
            try:
                block_time_str = t.get("Block", {}).get("Time", "")
                block_time = 0
                if block_time_str:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(block_time_str.replace("Z", "+00:00"))
                        block_time = int(dt.timestamp())
                    except Exception:
                        pass

                buy = t.get("Trade", {}).get("Buy", {})
                sell = t.get("Trade", {}).get("Sell", {})
                buy_currency = buy.get("Currency", {}) or {}
                sell_currency = sell.get("Currency", {}) or {}

                trades.append(FomoTrade(
                    signature=t.get("Transaction", {}).get("Signature", ""),
                    block_time=block_time,
                    buyer_wallet=wallet_address,
                    buy_currency_symbol=buy_currency.get("Symbol", ""),
                    buy_currency_mint=buy_currency.get("MintAddress", ""),
                    buy_amount=float(buy.get("Amount", 0) or 0),
                    buy_amount_usd=0.0,
                    sell_currency_symbol=sell_currency.get("Symbol", ""),
                    sell_currency_mint=sell_currency.get("MintAddress", ""),
                    sell_amount=float(sell.get("Amount", 0) or 0),
                    sell_amount_usd=0.0,
                ))
            except Exception:
                continue

        return trades[:limit]

    async def get_fees_paid(self, hours: int = 1) -> Dict[str, float]:
        """Get fees paid to FOMO in the last N hours (volume proxy)."""
        # Build the query with proper substitution
        query = """
        query FomoFees {
          Solana {
            Transfers(
              where: {
                Transfer: {Receiver: {Address: {is: "%s"}}},
                Block: {Time: {since_relative: {hours_ago: %d}}}
              }
            ) {
              fee_payments: count
              paying_wallets: count(distinct: Transfer_Sender_Owner)
              usdc_total: sum(of: Transfer_Amount)
            }
          }
        }
        """ % (FOMO_FEE_ACCOUNT_USDC, hours)
        data = await self._run_query(query)
        if not data:
            return {"fee_payments": 0, "paying_wallets": 0, "usdc_total": 0}

        transfers = data.get("Solana", {}).get("Transfers", [])
        if not transfers:
            return {"fee_payments": 0, "paying_wallets": 0, "usdc_total": 0}

        t = transfers[0]
        return {
            "fee_payments": int(t.get("fee_payments", 0) or 0),
            "paying_wallets": int(t.get("paying_wallets", 0) or 0),
            "usdc_total": float(t.get("usdc_total", 0) or 0),
        }

    async def get_stats(self) -> FomoStats:
        """Get a comprehensive FOMO platform stats summary."""
        # Cache for 5 minutes
        now = time.time()
        if self._cached_stats and now - self._stats_last_fetch < 300:
            return self._cached_stats

        # Run all queries in parallel
        signer_active, latest_trades, top_tokens, fees = await asyncio.gather(
            self.is_signer_active(),
            self.get_latest_trades(limit=1),
            self.get_top_tokens_bought(hours=1, limit=1),
            self.get_fees_paid(hours=1),
            return_exceptions=True,
        )

        # Handle exceptions gracefully
        if isinstance(signer_active, Exception):
            signer_active = False
        if isinstance(latest_trades, Exception):
            latest_trades = []
        if isinstance(top_tokens, Exception):
            top_tokens = []
        if isinstance(fees, Exception):
            fees = {"fee_payments": 0, "paying_wallets": 0, "usdc_total": 0}

        stats = FomoStats(
            trades_last_hour=len(latest_trades) if isinstance(latest_trades, list) else 0,
            is_signer_active=signer_active if isinstance(signer_active, bool) else False,
            fee_payments_last_hour=fees.get("fee_payments", 0) if isinstance(fees, dict) else 0,
            paying_wallets_last_hour=fees.get("paying_wallets", 0) if isinstance(fees, dict) else 0,
            usdc_fees_last_hour=fees.get("usdc_total", 0) if isinstance(fees, dict) else 0,
        )

        self._cached_stats = stats
        self._stats_last_fetch = now
        return stats

    # ========================================================
    # Copy-trade signal: identifies wallets worth copying
    # ========================================================
    async def get_top_traders_to_copy(
        self,
        min_trades: int = 5,
        min_tokens: int = 3,
        hours: int = 6,
        limit: int = 10,
    ) -> List[FomoActiveWallet]:
        """
        Identify top traders worth copying.

        Filters:
        - At least `min_trades` trades in the period
        - Traded at least `min_tokens` different tokens (diversified)
        - Sorted by trade count (most active first)

        These are traders who are actively engaged with the FOMO platform
        and could be good candidates for copy-trading.
        """
        wallets = await self.get_most_active_wallets(hours=hours, limit=limit * 3)
        # Filter
        filtered = [
            w for w in wallets
            if w.trades_count >= min_trades and w.tokens_traded >= min_tokens
        ]
        return filtered[:limit]

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
