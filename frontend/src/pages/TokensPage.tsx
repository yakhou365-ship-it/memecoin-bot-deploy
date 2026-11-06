import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { RefreshCw, Search, Rocket, Flame, GraduationCap, Wallet } from 'lucide-react'
import { api, endpoints } from '../lib/api'
import { fmtPrice, fmtCompact, fmtPct, fmtUsd, scoreColor, scoreBarColor } from '../lib/format'

export default function TokensPage() {
  const [search, setSearch] = useState('')
  const [tab, setTab] = useState<'all' | 'new' | 'trending' | 'graduated'>('all')
  const [selected, setSelected] = useState<string | null>(null)
  const qc = useQueryClient()

  // Fetch Pump.fun tokens (DexScreener aggregated feed - always live)
  const { data: pumpfunTokens, isLoading: pumpLoading } = useQuery({
    queryKey: ['pumpfun-tokens', tab],
    queryFn: async () => {
      if (tab === 'graduated') {
        return (await api.get(endpoints.pumpfunTokensGraduated, { params: { limit: 100 } })).data?.tokens || []
      }
      // V403: fetch 100 tokens (was 50) + 30s refresh (was 10s)
      return (await api.get(endpoints.marketLiveTokens, { params: { limit: 100 } })).data?.tokens || []
    },
    refetchInterval: 30000, // V403: 30s (was 10s — caused flickering)
  })

  // Fetch wallet balance
  const { data: wallet } = useQuery({
    queryKey: ['pumpfun-wallet'],
    queryFn: () => api.get(endpoints.pumpfunWalletBalance).then(r => r.data),
    refetchInterval: 5000,
  })

  const { data: pumpfunStatus } = useQuery({
    queryKey: ['pumpfun-status-tokens'],
    queryFn: () => api.get(endpoints.pumpfunStatus).then(r => r.data),
    refetchInterval: 5000,
  })

  // Buy mutation
  const buyMutation = useMutation({
    mutationFn: (data: { mint_address: string; amount_sol: number }) =>
      api.post(endpoints.pumpfunTradeBuy, data).then(r => r.data),
    onSuccess: (data) => {
      if (data.success) {
        toast.success(`✅ تم الشراء! TX: ${data.tx_signature?.slice(0, 12)}...`)
      } else {
        toast.error(`فشل الشراء: ${data.error || 'سبب غير معروف'}`)
      }
      qc.invalidateQueries({ queryKey: ['pumpfun-wallet'] })
    },
    onError: (e: any) => {
      // V508: distinguish 401 from 400/500 so the user knows what's wrong.
      const status = e?.response?.status
      const detail = e?.response?.data?.detail || e?.message || 'خطأ غير معروف'
      if (status === 401) {
        toast.error('🔒 مفتاح API خاطئ. افتح الرابط مع #resetkey لإدخال المفتاح الصحيح.')
      } else if (status === 400) {
        toast.error(`فشل الشراء: ${detail}`)
      } else {
        toast.error(`خطأ في الشراء (${status || '؟'}): ${detail}`)
      }
    },
  })

  const filtered = (pumpfunTokens || []).filter((t: any) => {
    if (tab === 'new' && t.is_graduated) return false
    if (tab === 'graduated' && !t.is_graduated) return false
    return (
      !search ||
      t.symbol?.toLowerCase().includes(search.toLowerCase()) ||
      t.address?.toLowerCase().includes(search.toLowerCase())
    )
  })

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">عملات Pump.fun</h1>
        <div className="flex items-center gap-2">
          <div className="card px-4 py-2">
            <span className="text-xs text-gray-500">رصيد المحفظة</span>
            <p className="font-bold text-accent-blue">{Number(wallet?.sol_balance || 0).toFixed(4)} SOL</p>
            {wallet?.usd_value ? (
              <p className="text-xs text-gray-500">≈ {fmtUsd(wallet.usd_value)}</p>
            ) : null}
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-2">
        <TabButton active={tab === 'all'} onClick={() => setTab('all')} icon={<Flame size={14} />} label="الكل" />
        <TabButton active={tab === 'new'} onClick={() => setTab('new')} icon={<Rocket size={14} />} label="جديدة" />
        <TabButton active={tab === 'trending'} onClick={() => setTab('trending')} icon={<Flame size={14} />} label="رائجة" />
        <TabButton active={tab === 'graduated'} onClick={() => setTab('graduated')} icon={<GraduationCap size={14} />} label="متخرجة" />
      </div>

      {/* Search */}
      <div className="flex-1 relative">
        <Search size={16} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500" />
        <input
          type="text"
          placeholder="بحث بالرمز أو العنوان..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full bg-bg-soft border border-bg-border rounded-lg px-10 py-2 text-sm focus:outline-none focus:border-accent-blue"
        />
      </div>

      {/* Tokens grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {pumpLoading && (
          <div className="col-span-full text-center py-8 text-gray-500">جاري التحميل...</div>
        )}
        {!pumpLoading && filtered.length === 0 && (
          <div className="col-span-full text-center py-8 text-gray-500">لا توجد عملات حالياً</div>
        )}
        {filtered.map((t: any) => (
          <TokenCard
            key={t.address || t.symbol}
            token={t}
            onBuy={(mint, sol) => buyMutation.mutate({ mint_address: mint, amount_sol: sol })}
            buying={buyMutation.isPending}
            canBuy={!!pumpfunStatus?.live_trading_enabled}
            maxSol={pumpfunStatus?.max_trade_sol || 2.0}
          />
        ))}
      </div>
    </div>
  )
}

function TabButton({ active, onClick, icon, label }: { active: boolean; onClick: () => void; icon: React.ReactNode; label: string }) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
        active ? 'bg-accent-blue text-white' : 'bg-bg-soft text-gray-400 hover:text-white'
      }`}
    >
      {icon}
      {label}
    </button>
  )
}

function TokenCard({ token, onBuy, buying, canBuy, maxSol }: { token: any; onBuy: (mint: string, sol: number) => void; buying: boolean; canBuy: boolean; maxSol: number }) {
  const [amount, setAmount] = useState(0.05)
  const progress: number | null = token.bonding_curve_progress ?? null
  const hasCurveData = progress != null
  const isGraduated = token.is_graduated || (progress ?? 0) >= 100

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="font-bold text-lg">{token.symbol || '?'}</h3>
          <p className="text-xs text-gray-500">{token.name || token.address?.slice(0, 8) + '...'}</p>
        </div>
        {isGraduated ? (
          <span className="badge badge-success text-xs">✅ متخرجة</span>
        ) : (
          <span className="badge badge-info text-xs">🔥 على المنحنى</span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-2 text-sm">
        <div>
          <p className="text-xs text-gray-500">السعر</p>
          <p className="font-mono">{fmtPrice(token.price_usd)}</p>
        </div>
        <div>
          <p className="text-xs text-gray-500">القيمة السوقية</p>
          <p className="font-mono">{fmtCompact(token.market_cap_usd)}</p>
        </div>
        <div>
          <p className="text-xs text-gray-500">الحجم 24h</p>
          <p className="font-mono">{fmtCompact(token.volume_24h_usd)}</p>
        </div>
        <div>
          <p className="text-xs text-gray-500">التغير 24h</p>
          {(() => {
            const ch = Number(token.price_change_24h_pct ?? token.price_change_24h ?? 0)
            return (
              <p className={`font-mono ${ch > 0 ? 'text-emerald-400' : ch < 0 ? 'text-red-400' : 'text-gray-400'}`}>
                {ch > 0 ? '+' : ''}{ch.toFixed(1)}%
              </p>
            )
          })()}
        </div>
        <div>
          <p className="text-xs text-gray-500">الحاملون</p>
          <p className="font-mono">{token.holder_count || '—'}</p>
        </div>
      </div>

      {/* Bonding curve progress */}
      {!isGraduated && (
        <div>
          <div className="flex justify-between text-xs mb-1">
            <span className="text-gray-500">تقدم المنحنى</span>
            {hasCurveData ? (
              <span className="font-medium">{progress.toFixed(1)}%</span>
            ) : (
              <span className="font-medium text-gray-400">غير متاح</span>
            )}
          </div>
          <div className="score-bar w-full">
            <div className={`score-bar-fill ${hasCurveData ? (progress >= 70 ? 'bg-emerald-500' : progress >= 40 ? 'bg-amber-500' : 'bg-blue-500') : 'bg-gray-500'}`} style={{ width: `${hasCurveData ? progress : 0}%` }} />
          </div>
        </div>
      )}

      {/* Buy form */}
      <div className="flex gap-2 pt-2 border-t border-bg-border">
        <input
          type="number"
          value={amount}
          onChange={(e) => setAmount(parseFloat(e.target.value) || 0)}
          step="0.01"
          min="0.01"
          max={maxSol}
          className="flex-1 bg-bg-soft border border-bg-border rounded-lg px-3 py-1.5 text-sm font-mono"
          placeholder="SOL"
        />
        <button
          onClick={() => token.address && onBuy(token.address, Math.min(amount, maxSol))}
          disabled={buying || !token.address || !canBuy}
          title={canBuy ? `الحد الأقصى ${maxSol} SOL` : 'التداول الحقيقي غير مفعّل'}
          className="btn-primary px-4 py-1.5 text-sm disabled:opacity-50"
        >
          {buying ? '⏳' : canBuy ? '🚀 شراء' : '🔒 شراء'}
        </button>
      </div>
    </div>
  )
}
