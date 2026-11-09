import axios from 'axios'

declare global {
  interface Window {
    __API_KEY__?: string
  }
}

export const api = axios.create({
  baseURL: '/api',
  timeout: 20000,
})

// V537: the backend injects the access code "052307" into window.__API_KEY__.
// The frontend reads it automatically — no prompt needed.
api.interceptors.request.use((config) => {
  const key = typeof window !== 'undefined'
    ? (window.__API_KEY__ || localStorage.getItem('bot_api_key') || '052307')
    : '052307'
  if (key) {
    config.headers = config.headers || {}
    config.headers['X-API-Key'] = key
    config.headers['Authorization'] = `Bearer ${key}`
  }
  return config
})

export const endpoints = {
  // Core
  account: '/account',
  tokens: '/tokens',
  tokenDetail: (id: string) => `/tokens/${id}`,
  trades: '/trades',
  closeTrade: (id: string) => `/trades/${id}/close`,
  exitAll: '/trades/exit-all',  // V500: Emergency exit button
  systemStatus: '/system/status',
  
  // Learning
  mistakes: '/learning/mistakes',
  learning: '/learning/analysis',
  learnings: '/learning/learnings',
  
  // Risk
  riskState: '/risk/state',
  resetCircuitBreaker: '/risk/reset-circuit-breaker',
  
  // Alerts
  alerts: '/alerts',
  
  // Scanner
  runScan: '/scanner/run',
  scanRuns: '/scanner/runs',
  
  // Strategies & Backtest
  strategies: '/strategies',
  backtest: '/backtest/run',
  research: (id: string) => `/research/${id}`,
  
  // 18-Agent System
  agentsStatus: '/agents/status',
  agentsList: '/agents/list',
  runAgents: '/agents/run',
  runAgentsByMint: '/agents/run-by-mint',
  analyzeTokenFull: (tokenId: string) => `/agents/analyze/${tokenId}`,
  
  // Chat AI
  chatSend: '/chat/send',
  chatHistory: '/chat/history',
  chatClear: '/chat/clear',
  
  // Pump.fun
  pumpfunStatus: '/pumpfun/status',
  pumpfunTokensNew: '/pumpfun/tokens/new',
  pumpfunTokensTrending: '/pumpfun/tokens/trending',
  pumpfunTokensGraduated: '/pumpfun/tokens/graduated',
  pumpfunTokenInfo: (mint: string) => `/pumpfun/token/${mint}`,
  pumpfunWalletBalance: '/pumpfun/wallet/balance',
  pumpfunTradeBuy: '/pumpfun/trade/buy',
  pumpfunTradeSell: '/pumpfun/trade/sell',
  
  // FOMO
  fomoStatus: '/fomo/status',
  fomoTrending: '/fomo/trending',
  fomoTopTraders: '/fomo/top-traders',
  fomoLatestTrades: '/fomo/latest-trades',
  fomoStats: '/fomo/stats',
  
  // Strategy V2
  strategyV2Status: '/strategy-v2/status',
  strategyV2Backtest: '/strategy-v2/backtest',
  
  // Budget
  budgetSettings: '/budget/settings',
  budgetDailyRecord: '/budget/daily-record',
  budgetCanTrade: '/budget/can-trade',
  budgetStop: '/budget/stop',
  budgetStart: '/budget/start',
  budgetDailyReport: '/budget/daily-report',
  budgetOpportunities: '/budget/opportunities',
  
  // Trading control
  tradingState: '/trading/state',
  tradingStart: '/trading/start',
  tradingStop: '/trading/stop',
  
  // Health Monitor
  healthMonitor: '/health-monitor',
  healthMonitorLatest: '/health-monitor/latest',
  
  // Independent Auditor
  audit: '/audit',
  auditLatest: '/audit/latest',
  
  // Counterfactual
  counterfactualReport: '/counterfactual/report',
  
  // Monte Carlo
  monteCarloSimulate: '/monte-carlo/simulate',
  
  // Strategy Lab
  strategyLabExperiments: '/strategy-lab/experiments',
  strategyLabTest: '/strategy-lab/test',
  
  // Market Regime Memory
  regimeMemorySummary: '/regime-memory/summary',

  // Live Tokens (DexScreener aggregated)
  marketLiveTokens: '/market/live-tokens',

  // ML
  mlModels: '/ml/models',
  mlTrain: '/ml/train',

  // Live trading
  liveStatus: '/live/status',
}
