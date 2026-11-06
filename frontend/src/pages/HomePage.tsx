import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  Play, Square, Wallet, Zap, Activity, ShieldCheck, AlertTriangle,
  TrendingUp, Cpu, Flame, Star,
} from 'lucide-react'
import { api, endpoints } from '../lib/api'
import { fmtUsd, fmtPct } from '../lib/format'

export default function HomePage() {
  const qc = useQueryClient()

  const { data: status } = useQuery({
    queryKey: ['system-status'],
    queryFn: () => api.get(endpoints.systemStatus).then(r => r.data),
    refetchInterval: 1_500,
  })

  const { data: tradingState } = useQuery({
    queryKey: ['trading-state'],
    queryFn: () => api.get(endpoints.tradingState).then(r => r.data),
    refetchInterval: 1_500,
  })

  const { data: wallet } = useQuery({
    queryKey: ['pumpfun-wallet-home'],
    queryFn: () => api.get(endpoints.pumpfunWalletBalance).then(r => r.data),
    refetchInterval: 1_500,
  })

  const { data: live } = useQuery({
    queryKey: ['live-status-home'],
    queryFn: () => api.get(endpoints.liveStatus).then(r => r.data),
    refetchInterval: 1_500,
  })

  const { data: liveTokens } = useQuery({
    queryKey: ['live-tokens-home'],
    queryFn: () => api.get(endpoints.marketLiveTokens, { params: { limit: 50 } }).then(r => r.data?.tokens || []),
    refetchInterval: 30_000, // V403: 30s (was 1.5s — caused tokens to flicker)
  })

  const { data: bestTrades } = useQuery({
    queryKey: ['best-trades-home'],
    queryFn: () => api.get(endpoints.trades, { params: { limit: 10, status: 'closed' } }).then(r => r.data as any[]),
    refetchInterval: 10_000, // V403: 10s (was 1.5s)
  })

  const topTrades = (bestTrades || [])
    .filter((t: any) => (t.pnl_pct ?? 0) > 0)
    .sort((a: any, b: any) => (b.pnl_pct || 0) - (a.pnl_pct || 0))
    .slice(0, 5)

  const startMutation = useMutation({
    mutationFn: () => api.post(endpoints.tradingStart).then(r => r.data),
    onSuccess: () => {
      toast.success('✅ تم بدء التداول')
      qc.invalidateQueries({ queryKey: ['trading-state'] })
      qc.invalidateQueries({ queryKey: ['system-status'] })
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'فشل بدء التداول'),
  })

  const stopMutation = useMutation({
    mutationFn: () => api.post(endpoints.tradingStop).then(r => r.data),
    onSuccess: () => {
      toast.success('⏸️ تم إيقاف التداول')
      qc.invalidateQueries({ queryKey: ['trading-state'] })
      qc.invalidateQueries({ queryKey: ['system-status'] })
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'فشل إيقاف التداول'),
  })

  const isTrading = tradingState?.is_active ?? false
  const isLive = tradingState?.is_live_trading ?? false
  const risk = status?.risk_state || {}
  const solBalance = Number(wallet?.sol_balance || 0)
  const usdValue = Number(wallet?.usd_value || 0)
  const missing: string[] = tradingState?.live_requirements_missing || []
  const daily = live?.daily || {}

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">لوحة التحكم</h1>
          <p className="text-xs text-gray-500 mt-1">
            آخر تحديث: {new Date().toLocaleTimeString('en-GB')}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {risk?.circuit_breaker_active && (
            <span className="badge badge-danger">🚨 Circuit Breaker</span>
          )}
          {risk?.cooldown_active && (
            <span className="badge badge-warning">⏸️ Cooldown</span>
          )}
          <span className="badge badge-danger">🔴 LIVE</span>
          {isTrading ? (
            <button onClick={() => stopMutation.mutate()} disabled={stopMutation.isPending} className="btn-danger">
              <Square size={16} className="inline ml-2" />
              إيقاف التداول
            </button>
          ) : (
            <button onClick={() => startMutation.mutate()} disabled={startMutation.isPending} className="btn-success">
              <Play size={16} className="inline ml-2" />
              بدء التداول
            </button>
          )}
          <span className={`badge ${isTrading ? 'badge-success' : 'badge-neutral'}`}>
            {isTrading ? '● يعمل' : '● متوقف'}
          </span>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          icon={<Wallet size={20} />}
          label="رصيد المحفظة"
          value={`${solBalance.toFixed(4)} SOL`}
          sub={usdValue ? `≈ ${fmtUsd(usdValue)}` : 'لا يوجد رصيد على السلسلة'}
          subColor={solBalance > 0 ? 'text-emerald-400' : 'text-red-400'}
        />
        <KpiCard
          icon={<Activity size={20} />}
          label="صفقات مفتوحة"
          value={status?.open_trades || 0}
          sub={`${status?.open_live_trades || 0} حقيقية`}
        />
        <KpiCard
          icon={<TrendingUp size={20} />}
          label="PnL اليوم"
          value={fmtPct(risk?.daily_pnl_pct || 0)}
          sub={fmtUsd(risk?.daily_pnl_usd || 0)}
          subColor={(risk?.daily_pnl_pct || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}
        />
        <KpiCard
          icon={<Zap size={20} />}
          label="خسائر متتالية"
          value={risk?.consecutive_losses || 0}
          subColor={(risk?.consecutive_losses || 0) >= 2 ? 'text-red-400' : 'text-gray-400'}
        />
      </div>

      {/* Live limits */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <MiniStat label="حد الصفقة" value={`${tradingState?.max_trade_sol ?? 2.0} SOL`} />
        <MiniStat label="الحد اليومي" value={`${daily.sol_spent ?? 0} / ${tradingState?.daily_cap_sol ?? 5.0} SOL`} />
        <MiniStat label="حد الخسارة اليومي" value={`${tradingState?.daily_loss_limit_sol ?? 1.0} SOL`} danger />
        <MiniStat label="Max Drawdown" value={fmtPct(risk?.max_drawdown_pct || 0)} danger />
      </div>

      {/* Risk + Live status */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">حالة المخاطر</h3>
          <div className="space-y-3 text-sm">
            <Row label="الخسارة اليومية" value={fmtPct(risk?.daily_pnl_pct || 0)} />
            <Row label="الخسارة الأسبوعية" value={fmtPct(risk?.weekly_pnl_pct || 0)} />
            <Row label="Max Drawdown" value={fmtPct(risk?.max_drawdown_pct || 0)} />
            <Row label="Cooldown" value={risk?.cooldown_active ? '🔴 مفعّل' : '🟢 غير مفعّل'} />
            <Row label="Circuit Breaker" value={risk?.circuit_breaker_active ? '🔴 مفعّل' : '🟢 غير مفعّل'} />
            {!risk?.can_open && risk?.reason && (
              <div className="mt-3 p-2 rounded-lg bg-red-500/10 border border-red-500/30 text-xs text-red-400">
                ⚠️ {risk.reason}
              </div>
            )}
          </div>
        </div>

        <div className="card p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">حالة التداول الحقيقي</h3>
          <div className="space-y-3 text-sm">
            <Row label="الوضع" value="🔴 حقيقي (SOL)" />
            <Row label="المحفظة" value={wallet?.wallet_configured ? '✅ مربوطة' : '❌ غير مربوطة'} />
            <Row label="عنوان المحفظة" value={
              <span className="font-mono text-xs">
                {wallet?.wallet_address ? `${wallet.wallet_address.slice(0, 8)}...${wallet.wallet_address.slice(-6)}` : '—'}
              </span>
            } />
            <Row label="PumpPortal API" value={live?.configured ? '✅ جاهز' : '⚠️ غير مكتمل'} />
            <Row label="صفقات حقيقية مفتوحة" value={`${live?.open_live_positions ?? 0} / ${live?.max_open_positions ?? 1}`} />
            <Row label="PnL اليوم (SOL)" value={(daily.sol_pnl ?? 0).toFixed(4)} />
            <Row label="Orchestrator" value={status?.orchestrator_enabled ? '🟢 مفعّل' : '🔴 معطّل'} />
          </div>
        </div>
      </div>

      {/* Live tokens table */}
      <div className="card p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3 flex items-center gap-2">
          <Flame size={16} className="text-orange-400" /> 🔥 كل التوكنات الحية ({(liveTokens || []).length})
        </h3>
        <div className="overflow-x-auto scrollbar">
          <table className="w-full text-xs text-left">
            <thead>
              <tr className="text-gray-500 border-b border-bg-border">
                <th className="p-2">#</th>
                <th className="p-2">الرمز</th>
                <th className="p-2">السعر</th>
                <th className="p-2">Market Cap</th>
                <th className="p-2">السيولة</th>
                <th className="p-2">5m %</th>
                <th className="p-2">1h %</th>
                <th className="p-2">24h %</th>
                <th className="p-2">حجم 24h</th>
              </tr>
            </thead>
            <tbody>
              {(liveTokens || []).map((t: any, i: number) => (
                <tr key={t.address || i} className="border-b border-bg-border hover:bg-bg-soft">
                  <td className="p-2">{i + 1}</td>
                  <td className="p-2 font-bold">{t.symbol || '?'}</td>
                  <td className="p-2 font-mono">{t.price_usd ? `$${Number(t.price_usd).toFixed(8)}` : '—'}</td>
                  <td className="p-2 font-mono">${Number(t.market_cap_usd || 0).toLocaleString('en-US', { maximumFractionDigits: 0 })}</td>
                  <td className="p-2 font-mono">${Number(t.liquidity_usd || 0).toLocaleString('en-US', { maximumFractionDigits: 0 })}</td>
                  <td className={`p-2 font-mono ${Number(t.price_change_5m || t.price_change_5m_pct || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {Number(t.price_change_5m || t.price_change_5m_pct || 0).toFixed(1)}%
                  </td>
                  <td className={`p-2 font-mono ${Number(t.price_change_1h || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {Number(t.price_change_1h || 0).toFixed(1)}%
                  </td>
                  <td className={`p-2 font-mono ${Number(t.price_change_24h || t.price_change_24h_pct || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {Number(t.price_change_24h || t.price_change_24h_pct || 0).toFixed(1)}%
                  </td>
                  <td className="p-2 font-mono">${Number(t.volume_24h_usd || 0).toLocaleString('en-US', { maximumFractionDigits: 0 })}</td>
                </tr>
              ))}
              {!liveTokens || liveTokens.length === 0 ? (
                <tr><td colSpan={9} className="p-4 text-center text-gray-500">لا توجد توكنات حية الآن</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>

      {/* Best trades */}
      <div className="card p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3 flex items-center gap-2">
          <Star size={16} className="text-yellow-400" /> ⭐ أفضل الصفقات (حسب PnL %)
        </h3>
        <div className="overflow-x-auto scrollbar">
          <table className="w-full text-xs text-left">
            <thead>
              <tr className="text-gray-500 border-b border-bg-border">
                <th className="p-2">#</th>
                <th className="p-2">العملة</th>
                <th className="p-2">النوع</th>
                <th className="p-2">PnL %</th>
                <th className="p-2">PnL $</th>
                <th className="p-2">السبب</th>
              </tr>
            </thead>
            <tbody>
              {topTrades.map((t: any, i: number) => (
                <tr key={t.id || i} className="border-b border-bg-border hover:bg-bg-soft">
                  <td className="p-2">{i + 1}</td>
                  <td className="p-2 font-bold">{t.token_symbol || '—'}</td>
                  <td className="p-2">
                    {(t.strategy_id || '').startsWith('live') ? (
                      <span className="badge badge-danger text-[10px]">🔴 حقيقية</span>
                    ) : null}
                  </td>
                  <td className="p-2 font-mono text-emerald-400">+{Number(t.pnl_pct || 0).toFixed(2)}%</td>
                  <td className="p-2 font-mono text-emerald-400">+{Number(t.net_pnl_usd || 0).toFixed(2)}</td>
                  <td className="p-2 text-gray-400 truncate max-w-[240px]">{t.exit_reason || '—'}</td>
                </tr>
              ))}
              {topTrades.length === 0 && (
                <tr><td colSpan={6} className="p-4 text-center text-gray-500">لا توجد صفقات رابحة مغلقة بعد</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* AI agents */}
      <div className="card p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3 flex items-center gap-2">
          <Cpu size={16} /> وكلاء الذكاء الاصطناعي
        </h3>
        <div className="flex flex-wrap gap-2">
          {Object.entries(status?.ai_agents_enabled || {}).map(([name, enabled]) => (
            <span key={name} className={`badge ${enabled ? 'badge-success' : 'badge-neutral'}`}>
              {enabled ? <ShieldCheck size={12} /> : null} {name}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}

function KpiCard({ icon, label, value, sub, subColor }: { icon: React.ReactNode; label: string; value: any; sub?: string; subColor?: string }) {
  return (
    <div className="card flex items-start gap-3">
      <div className="w-10 h-10 rounded-lg bg-bg-soft flex items-center justify-center text-accent-blue">
        {icon}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-xs text-gray-500 mb-1">{label}</p>
        <p className="text-lg font-bold truncate">{value}</p>
        {sub && <p className={`text-xs ${subColor || 'text-gray-500'} truncate`}>{sub}</p>}
      </div>
    </div>
  )
}

function MiniStat({ label, value, danger }: { label: string; value: any; danger?: boolean }) {
  return (
    <div className="card py-3">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={`font-bold ${danger ? 'text-amber-400' : 'text-gray-200'}`}>{value}</p>
    </div>
  )
}

function Row({ label, value }: { label: string; value: any }) {
  return (
    <div className="flex justify-between items-center gap-3">
      <span className="text-gray-400">{label}</span>
      <span className="font-medium text-right">{value}</span>
    </div>
  )
}
