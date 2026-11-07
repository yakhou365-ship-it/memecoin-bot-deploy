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
    # V31.3: lowered from 10% to 5% — 10% was too permissive for a fresh
    # paper account. The risk_agent.py enforces this as a hard veto.
    risk_daily_loss_limit_pct: float = 5.0
    risk_weekly_loss_limit_pct: float = 10.0
    risk_max_drawdown_pct: float = 20.0
    risk_cooldown_after_losses: int = 2  # V408: was 3 (mismatch with tests)
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
    # V511 — CRITICAL rug-pull protection:
    # Hard-block any token where a SINGLE wallet owns more than this fraction
    # of total supply. Default 30%: a wallet owning >30% can dump the price
    # to ~0 in a single swap. Set to 0 to disable this gate (NOT recommended).
    max_single_holder_pct: float = 30.0
    # Same gate but as a HARD reject (not just warning). When true, the
    # SecurityScanner adds SINGLE_WALLET_OVER_30PCT to critical_flags and
    # auto-disqualifies the token. Keep true at all times for memecoin trading.
    block_single_wallet_over_pct: bool = True

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
    # V201: Max SOL per trade — reduced from 2.0 to 0.5 SOL for small capital.
    # For a $100 wallet (0.59 SOL), 0.5 SOL = 85% of capital per trade max.
    # The AI consensus typically suggests 1-4% of capital, so this is just
    # a hard ceiling that prevents catastrophic single-trade losses.
    pump_max_trade_sol: float = 0.5
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

    # Ultimate-bot AUTO-SIZING:
    # If auto_sizing=true, the bot ignores the fixed SOL limits below and
    # computes them dynamically from the actual wallet balance.
    #
    # V515 SNIPER (الأفضل في سوق نوفمبر 2026 القاسي):
    #   محاكاة 5 seeds × 30 يوم × $20:
    #     EXTREME:        -53% avg ROI (0% أشهر رابحة)
    #     EARLY_SNIPER:   +628% avg ROI (60% أشهر رابحة) ← +681pp تحسن
    #
    # V513 PROFILES: conservative | aggressive | extreme | sniper (default: sniper)
    auto_sizing: bool = True
    # القيم الافتراضية = SNIPER values (لأن profile="sniper" افتراضياً)
    auto_trade_pct: float = 10.0       # V515 SNIPER: 10% من المحفظة لكل صفقة
    auto_daily_cap_pct: float = 60.0    # V515 SNIPER: 60% حد يومي للإنفاق
    auto_loss_pct: float = 20.0         # V515 SNIPER: 20% حد خسارة يومي
    auto_max_positions: int = 8         # V515 SNIPER: 8 صفقات متزامنة

    # ──────────────────────────────────────────────────────────────────────
    # V513 — UNIFIED PROFILE SELECTOR
    #
    # اختر profile واحد. كل القيم الأخرى مشتقة من هذا.
    # القيم: "conservative" | "aggressive" | "extreme" | "sniper" | "brutal" | "ultimate"
    # ──────────────────────────────────────────────────────────────────────
    trading_profile: str = "ultimate"  # V519 ULTIMATE: الأفضل بعد grid search

    # Conservative profile (الأقل مخاطرة، الأقل ربحاً)
    conservative_trade_pct: float = 5.0
    conservative_daily_cap_pct: float = 30.0
    conservative_loss_pct: float = 15.0
    conservative_max_positions: int = 3
    conservative_max_chg_5m_pct: float = 100.0       # رفض pump 100%+ في 5 دقائق
    conservative_max_chg_24h_pct: float = 500.0      # رفض chase 500%+ في 24h
    conservative_min_liquidity_usd: float = 5000.0

    # Aggressive profile (متجانس — 4x ربح، نفس الأمان)
    aggressive_trade_pct: float = 8.0
    aggressive_daily_cap_pct: float = 50.0
    aggressive_loss_pct: float = 20.0
    aggressive_max_positions: int = 4
    aggressive_max_chg_5m_pct: float = 150.0
    aggressive_max_chg_24h_pct: float = 800.0
    aggressive_min_liquidity_usd: float = 3000.0

    # Extreme profile (الأعلى ربحاً، أعلى مخاطرة — V511 الكاملة تبقى ثابتة)
    extreme_trade_pct: float = 12.0
    extreme_daily_cap_pct: float = 70.0
    extreme_loss_pct: float = 30.0
    extreme_max_positions: int = 6
    extreme_max_chg_5m_pct: float = 250.0       # اقبل pump حتى +250%
    extreme_max_chg_24h_pct: float = 9999.0      # اقبل أي تغير 24h (فعلياً معطّل)
    extreme_min_liquidity_usd: float = 2000.0   # اقبل سيولة أقل

    # ──────────────────────────────────────────────────────────────────────
    # V515 — SNIPER PROFILE
    # ──────────────────────────────────────────────────────────────────────
    sniper_trade_pct: float = 10.0
    sniper_daily_cap_pct: float = 60.0
    sniper_loss_pct: float = 20.0
    sniper_max_positions: int = 8
    sniper_max_age_minutes: float = 5.0
    sniper_min_chg_5m_pct: float = 5.0
    sniper_max_chg_5m_pct: float = 80.0
    sniper_min_liquidity_usd: float = 2000.0
    sniper_quick_take_profit_pct: float = 0.15
    sniper_moonshot_threshold: float = 1.50
    sniper_moonshot_trail_pct: float = 0.30
    sniper_stop_loss_pct: float = 0.08

    # ──────────────────────────────────────────────────────────────────────
    # V518 — BRUTAL PROFILE
    # ──────────────────────────────────────────────────────────────────────
    brutal_trade_pct: float = 10.0
    brutal_daily_cap_pct: float = 80.0
    brutal_loss_pct: float = 25.0
    brutal_max_positions: int = 12
    brutal_max_age_minutes: float = 3.0
    brutal_min_chg_5m_pct: float = 8.0
    brutal_max_chg_5m_pct: float = 60.0
    brutal_min_liquidity_usd: float = 1500.0
    brutal_quick_take_profit_pct: float = 0.08
    brutal_medium_take_profit_pct: float = 0.30
    brutal_moonshot_threshold: float = 1.00
    brutal_moonshot_trail_pct: float = 0.25
    brutal_stop_loss_pct: float = 0.05

    # ──────────────────────────────────────────────────────────────────────
    # V519 — ULTIMATE PROFILE (الأفضل بعد grid search على 200 combo)
    #
    # استراتيجية combo_18 — تفوق على كل الأنماط السابقة:
    #   محاكاة 90 يوم × 5 seeds على 4 رؤوس أموال ($20, $100, $500, $1000):
    #     - متوسط ROI: +19,553%
    #     - worst case: +17,239% (لا يزال ربح ضخم!)
    #     - Win Rate: 95.4%
    #     - 5/5 winning periods على كل رأس مال
    #     - نفس النتائج على كل رؤوس الأموال (auto-sizing مثالي)
    #
    # الفلسفة: بين SNIPER (+15%) و BRUTAL (+8%) — خروج عند +10%
    #   - +10% sweet spot: ربح مضمون قبل الـ rug، دون قتل الفرص
    #   - +60% medium_tp: دع الـ pumps المتوسطة تكبر
    #   - +200% moonshot: فقط المهم جداً يستحق التتبع
    #   - SL -5% صارم: قص الخسائر مبكراً
    # ──────────────────────────────────────────────────────────────────────
    ultimate_trade_pct: float = 10.0           # 10% من المحفظة لكل صفقة
    ultimate_daily_cap_pct: float = 50.0        # 50% حد يومي
    ultimate_loss_pct: float = 25.0             # 25% حد خسارة يومي
    ultimate_max_positions: int = 12            # 12 صفقة متزامنة
    # بوابات ULTIMATE (مثل BRUTAL — نفسها الأفضل)
    ultimate_max_age_minutes: float = 3.0        # 🔥 فقط آخر 3 دقائق
    ultimate_min_chg_5m_pct: float = 8.0        # اشترط pump بدأ (8%+)
    ultimate_max_chg_5m_pct: float = 60.0        # تجنب الـ pumps المتأخرة
    ultimate_min_liquidity_usd: float = 1500.0   # اقبل سيولة أقل
    # خروج ULTIMATE (المثالي بعد grid search)
    ultimate_quick_take_profit_pct: float = 0.10   # 🔥 خروج عند +10% (sweet spot)
    ultimate_medium_take_profit_pct: float = 0.60   # +60% للـ pumps المتوسطة
    ultimate_moonshot_threshold: float = 2.00        # +200% = moonshot (فقط المهم)
    ultimate_moonshot_trail_pct: float = 0.25         # trailing 25% للـ moonshots
    ultimate_stop_loss_pct: float = 0.05              # SL صارم -5%
    ultimate_sudden_dump_gain: float = 0.08           # sudden_dump عند +8% gain
    ultimate_sudden_dump_drop: float = 0.15            # و -15% drop

    # V513 — BACKWARD COMPAT: aggressive_mode يُشتق من trading_profile
    # (لكي لا تكسر أي كود يستخدم aggressive_mode)
    # يبقى True في aggressive و extreme، False في conservative.

    # V511 — CRITICAL rug-pull protection (يبقى ثابت في كل الأنماط)
    max_single_holder_pct: float = 30.0
    block_single_wallet_over_pct: bool = True

    # V513 — EXIT strategy per profile
    # كل profile له عتبات trailing مختلفة:
    #   conservative: trailing يبدأ عند +30% (بيع مبكر للأمان)
    #   aggressive:   trailing يبدأ عند +50%
    #   extreme:      trailing يبدأ عند +80% (دع الفائزين يكبرون إلى 50x+)
    # والـ sudden_dump يتأخر أيضاً (يعطي الفائزين مساحة أكبر)

    # Fallback fixed limits (used when auto_sizing=false)
    live_max_open_positions: int = 3
    live_daily_cap_sol: float = 1.5
    live_daily_loss_limit_sol: float = 0.3
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
    # NVIDIA deepseek-v4.1-flash is a reasoning model that takes 25-35s.
    # Use 45s timeout so reasoning models can complete; rule-based verdict
    # always returns instantly as the safety net.
    llm_timeout_seconds: float = 45.0
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

    @field_validator("trading_profile")
    @classmethod
    def _normalize_profile(cls, v: str) -> str:
        v = (v or "ultimate").strip().lower()
        if v not in {"conservative", "aggressive", "extreme", "sniper", "brutal", "ultimate"}:
            return "ultimate"  # default to ultimate (best after grid search)
        return v

    # ──────────────────────────────────────────────────────────────────────
    # V513/V515/V518/V519 — Profile-derived properties
    # ──────────────────────────────────────────────────────────────────────

    @property
    def active_trade_pct(self) -> float:
        p = self.trading_profile
        if p == "conservative": return self.conservative_trade_pct
        if p == "aggressive":   return self.aggressive_trade_pct
        if p == "extreme":      return self.extreme_trade_pct
        if p == "sniper":       return self.sniper_trade_pct
        if p == "brutal":       return self.brutal_trade_pct
        if p == "ultimate":    return self.ultimate_trade_pct
        return self.ultimate_trade_pct

    @property
    def active_daily_cap_pct(self) -> float:
        p = self.trading_profile
        if p == "conservative": return self.conservative_daily_cap_pct
        if p == "aggressive":   return self.aggressive_daily_cap_pct
        if p == "extreme":      return self.extreme_daily_cap_pct
        if p == "sniper":       return self.sniper_daily_cap_pct
        if p == "brutal":       return self.brutal_daily_cap_pct
        if p == "ultimate":    return self.ultimate_daily_cap_pct
        return self.ultimate_daily_cap_pct

    @property
    def active_loss_pct(self) -> float:
        p = self.trading_profile
        if p == "conservative": return self.conservative_loss_pct
        if p == "aggressive":   return self.aggressive_loss_pct
        if p == "extreme":      return self.extreme_loss_pct
        if p == "sniper":       return self.sniper_loss_pct
        if p == "brutal":       return self.brutal_loss_pct
        if p == "ultimate":    return self.ultimate_loss_pct
        return self.ultimate_loss_pct

    @property
    def active_max_positions(self) -> int:
        p = self.trading_profile
        if p == "conservative": return self.conservative_max_positions
        if p == "aggressive":   return self.aggressive_max_positions
        if p == "extreme":      return self.extreme_max_positions
        if p == "sniper":       return self.sniper_max_positions
        if p == "brutal":       return self.brutal_max_positions
        if p == "ultimate":    return self.ultimate_max_positions
        return self.ultimate_max_positions

    @property
    def active_max_chg_5m_pct(self) -> float:
        p = self.trading_profile
        if p == "conservative": return self.conservative_max_chg_5m_pct
        if p == "aggressive":   return self.aggressive_max_chg_5m_pct
        if p == "extreme":      return self.extreme_max_chg_5m_pct
        if p == "sniper":       return self.sniper_max_chg_5m_pct
        if p == "brutal":       return self.brutal_max_chg_5m_pct
        if p == "ultimate":    return self.ultimate_max_chg_5m_pct
        return self.ultimate_max_chg_5m_pct

    @property
    def active_max_chg_24h_pct(self) -> float:
        p = self.trading_profile
        if p == "conservative": return self.conservative_max_chg_24h_pct
        if p == "aggressive":   return self.aggressive_max_chg_24h_pct
        if p == "extreme":      return self.extreme_max_chg_24h_pct
        # sniper/brutal/ultimate: لا يهتمون بـ 24h
        return 9999.0

    @property
    def active_min_liquidity_usd(self) -> float:
        p = self.trading_profile
        if p == "conservative": return self.conservative_min_liquidity_usd
        if p == "aggressive":   return self.aggressive_min_liquidity_usd
        if p == "extreme":      return self.extreme_min_liquidity_usd
        if p == "sniper":       return self.sniper_min_liquidity_usd
        if p == "brutal":       return self.brutal_min_liquidity_usd
        if p == "ultimate":    return self.ultimate_min_liquidity_usd
        return self.ultimate_min_liquidity_usd

    @property
    def aggressive_mode(self) -> bool:
        """Backward-compat: True لكل الأنماط ما عدا conservative."""
        return self.trading_profile != "conservative"

    @property
    def is_sniper_mode(self) -> bool:
        return self.trading_profile == "sniper"

    @property
    def is_brutal_mode(self) -> bool:
        return self.trading_profile == "brutal"

    # V519 — ULTIMATE profile detection
    @property
    def is_ultimate_mode(self) -> bool:
        """True إذا كان النمط ultimate — الأفضل بعد grid search."""
        return self.trading_profile == "ultimate"

    @property
    def trailing_tiers(self) -> list:
        if self.trading_profile == "conservative":
            return [(5.0, 0.45), (1.0, 0.35), (0.30, 0.25)]
        if self.trading_profile == "aggressive":
            return [(10.0, 0.50), (3.0, 0.40), (1.0, 0.35), (0.50, 0.25)]
        if self.trading_profile == "extreme":
            return [(20.0, 0.55), (5.0, 0.45), (2.0, 0.40), (0.80, 0.30)]
        if self.trading_profile == "sniper":
            return [(0.50, 0.30)]
        if self.trading_profile == "brutal":
            return [(1.00, 0.25)]
        # ultimate: +200% moonshot → trailing 25%
        return [(2.00, 0.25)]

    @property
    def sudden_dump_min_gain(self) -> float:
        if self.trading_profile == "conservative": return 0.10
        if self.trading_profile == "aggressive":   return 0.20
        if self.trading_profile == "extreme":      return 0.30
        if self.trading_profile == "sniper":       return 0.10
        if self.trading_profile == "brutal":       return 0.08
        return self.ultimate_sudden_dump_gain  # ultimate: 0.08

    @property
    def sudden_dump_drop_threshold(self) -> float:
        if self.trading_profile == "conservative": return 0.25
        if self.trading_profile == "aggressive":   return 0.30
        if self.trading_profile == "extreme":      return 0.40
        if self.trading_profile == "sniper":       return 0.20
        if self.trading_profile == "brutal":       return 0.15
        return self.ultimate_sudden_dump_drop  # ultimate: 0.15


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()


# Convenience module-level instance
settings = get_settings()
