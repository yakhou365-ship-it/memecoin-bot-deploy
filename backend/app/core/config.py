"""
Centralized configuration loader.
All modules MUST read configuration from this object, never from os.environ directly.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_log_level: str = "INFO"
    app_secret_key: str = "change-me"

    # --- Security ---
    # When empty, authentication is disabled (local development only).
    # In production set a strong random value; the dashboard receives it
    # automatically through window.__API_KEY__ injection.
    api_auth_key: str = ""
    cors_allowed_origins: str = "*"

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./data/memecoin_bot.db"
    data_dir: str = "./data"

    # --- Redis ---
    redis_url: str = "redis://redis:6379/0"
    use_redis: bool = False

    # --- Paper Trading ---
    paper_capital_usd: float = 10000.0
    paper_default_position_pct: float = 5.0
    paper_max_open_positions: int = 10
    paper_fee_pct: float = 0.25
    paper_slippage_pct: float = 0.5

    # --- Risk Management ---
    risk_daily_loss_limit_pct: float = 5.0
    risk_weekly_loss_limit_pct: float = 10.0
    risk_max_drawdown_pct: float = 20.0
    risk_cooldown_after_losses: int = 3
    risk_max_risk_per_trade_pct: float = 2.0

    # --- Qualification ---
    # V31.2: Lowered for Pump.fun (was 0.70 — too strict for memecoins)
    min_win_probability: float = 0.55
    min_confidence_interval: float = 0.50
    min_liquidity_usd: float = 5000.0
    min_volume_24h_usd: float = 10000.0
    min_data_age_seconds: int = 120
    max_risk_score: int = 70

    # --- Market Scanner ---
    scan_interval_seconds: int = 10
    scan_max_tokens: int = 200
    scan_networks: str = "solana"

    # --- Data Sources ---
    dexscreener_api_url: str = "https://api.dexscreener.com"
    geckoterminal_api_url: str = "https://api.geckoterminal.com/api/v2"
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_rpc_backup_1: str = "https://rpc.ankr.com/solana"
    solana_rpc_backup_2: str = "https://solana-rpc.publicnode.com"

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- ML ---
    ml_model_dir: str = "./data/models"
    ml_retrain_interval_hours: int = 24
    ml_min_train_samples: int = 30
    ml_calibration_method: str = "isotonic"

    # --- On-chain ---
    onchain_top_holders: int = 20
    onchain_min_holder_balance_pct: float = 1.0

    # --- Pump.fun / PumpPortal ---
    # Accept both spellings: the historical typo PUMPORTAL_API_KEY and the
    # correct PUMPPORTAL_API_KEY. Railway currently uses the correct one.
    pumportal_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("PUMPPORTAL_API_KEY", "PUMPORTAL_API_KEY", "pumportal_api_key"),
    )
    # Solana wallet private key (base58 encoded)
    solana_private_key: str = ""
    solana_wallet_address: str = ""
    # Slippage in basis points (500 = 5%)
    pump_slippage_bps: int = 500
    # Priority fee in SOL (higher = faster execution)
    pump_priority_fee_sol: float = 0.0001
    # Max SOL per trade on Pump.fun (hard safety limit)
    pump_max_trade_sol: float = 2.0
    # Pool to trade on (pump, raydium, pump-amm, launchlab, raydium-cpmm, bonk, auto)
    pump_default_pool: str = "auto"
    # Skip preflight simulation for faster execution (PumpPortal Lightning)
    pump_skip_preflight: bool = True

    # --- LIVE TRADING SAFETY ---
    # A live trade can only happen when ALL of these are true:
    #   trading_mode == "live"
    #   pumpportal_api_key is set
    #   solana_private_key is set
    #   live_trading_confirm == "I_UNDERSTAND_THE_RISK"
    trading_mode: str = "paper"  # paper | live
    live_trading_confirm: str = ""
    live_max_open_positions: int = 5
    live_daily_cap_sol: float = 5.0
    live_daily_loss_limit_sol: float = 1.0
    live_cycle_timeout_seconds: int = 300

    # --- Orchestrator ---
    enable_orchestrator: bool = True
    auto_start_trading: bool = True
    forward_test_enabled: bool = False

    # --- FOMO Trading App (Bitquery API) ---
    bitquery_api_key: str = ""
    bitquery_graphql_url: str = "https://graphql.bitquery.io"

    # --- Sentiment ---
    twitter_bearer_token: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""

    # --- Backtesting ---
    backtest_fee_pct: float = 0.25
    backtest_slippage_pct: float = 0.5
    backtest_lookback_days: int = 90

    # --- Strategy V2 (improved rule-based engine) ---
    strategy_v2_enabled: bool = True
    strategy_v2_strict: bool = False
    strategy_v2_risk_per_trade: float = 0.0075
    strategy_v2_enable_short: bool = False
    strategy_v2_min_tp_to_cost: float = 3.0

    # --- RedotPay ---
    redotpay_api_token: str = ""
    redotpay_account_email: str = ""
    redotpay_auto_reminder_threshold_usd: float = 1000.0

    # --- AI Agents ---
    ai_agent_model: str = "deepseek-ai/deepseek-v4.1-flash"
    ai_agent_temperature: float = 0.3
    ai_agent_max_tokens: int = 800
    ai_agent_timeout_seconds: int = 8
    ai_agent_technical_enabled: bool = True
    ai_agent_security_enabled: bool = True
    ai_agent_liquidity_enabled: bool = True
    ai_agent_sentiment_enabled: bool = True
    ai_agent_risk_enabled: bool = True
    ai_agent_require_unanimity: bool = False

    # --- Jupiter API (Solana DEX aggregator) ---
    jupiter_api_key: str = ""
    jupiter_api_url: str = "https://lite-api.jup.ag"

    # --- LLM API Keys (any one will work) ---
    gemini_api_key: str = ""
    groq_api_key: str = ""
    openrouter_api_key: str = ""
    together_api_key: str = ""
    nvidia_api_key: str = ""
    nvidia_api_key_2: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    # Model used on OpenRouter / NVIDIA / Zhipu (provider-specific fallbacks apply)
    llm_model: str = "openai/gpt-4o-mini"
    llm_provider: str = "auto"
    llm_timeout_seconds: float = 8.0
    llm_cache_seconds: int = 60

    # --- Computed ---
    @property
    def networks_list(self) -> List[str]:
        return [n.strip() for n in self.scan_networks.split(",") if n.strip()]

    @property
    def cors_origins_list(self) -> List[str]:
        if self.cors_allowed_origins.strip() in ("", "*"):
            return ["*"]
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def pumpportal_api_key(self) -> str:
        """Correctly-spelled alias used by new code."""
        return self.pumportal_api_key

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def live_requirements(self) -> List[str]:
        """Return the list of missing requirements for live trading."""
        missing = []
        if self.trading_mode.strip().lower() != "live":
            missing.append("TRADING_MODE=live")
        if not self.pumportal_api_key.strip():
            missing.append("PUMPPORTAL_API_KEY")
        if not self.solana_private_key.strip():
            missing.append("SOLANA_PRIVATE_KEY")
        if not self.solana_wallet_address.strip():
            missing.append("SOLANA_WALLET_ADDRESS")
        if self.live_trading_confirm.strip() != "I_UNDERSTAND_THE_RISK":
            missing.append("LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISK")
        return missing

    @property
    def is_live_trading(self) -> bool:
        """True only when every live-trading safety requirement is satisfied."""
        return not self.live_requirements

    @field_validator("min_win_probability", "min_confidence_interval")
    @classmethod
    def _prob_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("probability must be in [0,1]")
        return v

    @field_validator("trading_mode")
    @classmethod
    def _normalize_mode(cls, v: str) -> str:
        return (v or "paper").strip().lower()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()


# Convenience module-level instance
settings = get_settings()
