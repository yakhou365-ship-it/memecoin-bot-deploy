// API types
export interface AccountState {
  starting_capital: number
  cash_balance: number
  equity: number
  open_positions_value: number
  unrealized_pnl: number
  realized_pnl: number
  total_pnl: number
  total_pnl_pct: number
  max_drawdown_pct: number
  open_positions_count: number
  win_rate: number
  winning_trades: number
  losing_trades: number
  closed_trades: number
  total_fees_paid: number
  total_slippage_paid: number
  stats: TradeStats
}

export interface TradeStats {
  total_closed_trades: number
  winning_trades: number
  losing_trades: number
  win_rate_pct: number
  total_net_pnl_usd: number
  avg_pnl_per_trade_usd: number
  profit_factor: number
  open_positions_count: number
  cash_balance: number
  equity: number
  peak_equity: number
  max_drawdown_pct: number
  total_fees_paid: number
  total_slippage_paid: number
}

export interface Token {
  id: string
  address: string
  chain: string
  symbol: string
  name: string | null
  dex: string | null
  price_usd: number | null
  market_cap_usd: number | null
  liquidity_usd: number | null
  volume_24h_usd: number | null
  volume_5m_usd: number | null
  txns_24h_buys: number | null
  txns_24h_sells: number | null
  price_change_24h_pct: number | null
  price_change_5m_pct: number | null
  fdv_usd: number | null
  watch_priority: number
  latest_opportunity_score: number | null
  latest_risk_score: number | null
  latest_win_probability: number | null
  first_seen_at: string | null
  updated_at: string | null
  active: boolean
  blacklisted: boolean
  blacklist_reason: string | null
}

export interface Trade {
  id: string
  token_id: string
  token_symbol: string | null
  token_chain: string | null
  side: string
  status: string
  entry_price: number
  entry_size_usd: number
  entry_quantity: number
  entry_timestamp: string
  exit_price: number | null
  exit_timestamp: string | null
  exit_reason: string | null
  net_pnl_usd: number | null
  pnl_pct: number | null
  fees_total_usd: number
  slippage_total_usd: number
  max_unrealized_profit_usd: number | null
  max_unrealized_loss_usd: number | null
  stop_loss_price: number | null
  take_profit_price: number | null
  strategy_id: string | null
  entry_reasons: string[]
  exit_reasons: string[]
}

export interface Mistake {
  id: string
  mistake_type: string
  description: string
  conditions: Record<string, unknown>
  evidence: string[]
  suggested_fix: Record<string, unknown>
  fix_tested: boolean
  fix_test_result: string | null
  fix_adopted: boolean
  active_block: boolean
  occurrence_count: number
  last_occurred_at: string | null
  created_at: string | null
}

export interface AlertItem {
  id: string
  alert_type: string
  severity: string
  title: string
  message: string
  data: Record<string, unknown>
  created_at: string | null
}

export interface RiskState {
  daily_pnl_usd: number
  weekly_pnl_usd: number
  daily_pnl_pct: number
  weekly_pnl_pct: number
  max_drawdown_pct: number
  open_positions_count: number
  consecutive_losses: number
  cooldown_active: boolean
  cooldown_until: string | null
  circuit_breaker_active: boolean
  circuit_breaker_reason: string
  last_trade_at: string | null
  last_loss_at: string | null
  can_open: boolean
  reason: string
}

export interface MLModel {
  id: string
  model_type: string
  purpose: string
  version: number
  train_samples: number
  validation_samples: number
  test_samples: number
  precision: number | null
  recall: number | null
  f1: number | null
  roc_auc: number | null
  brier_score: number | null
  calibration_error: number | null
  status: string
  created_at: string | null
}

export interface AnalysisSnapshot {
  id: string
  timestamp: string | null
  technical_score: number | null
  liquidity_score: number | null
  onchain_score: number | null
  security_score: number | null
  sentiment_score: number | null
  momentum_score: number | null
  market_conditions_score: number | null
  volatility_score: number | null
  risk_score: number | null
  opportunity_score: number | null
  win_probability: number | null
  win_probability_lower_ci: number | null
  win_probability_upper_ci: number | null
  market_regime: string | null
  qualified: boolean
  rejection_reason: string | null
  reasons: string[]
  reasoning_summary: string | null
  indicators: Record<string, unknown>
  flags: Record<string, unknown>
}
