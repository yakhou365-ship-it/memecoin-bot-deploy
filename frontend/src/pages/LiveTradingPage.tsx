import { useQuery } from '@tanstack/react-query'
import { Wallet, Shield, Zap, Rocket, Activity, AlertTriangle, KeyRound } from 'lucide-react'
import { api, endpoints } from '../lib/api'
import { fmtUsd, fmtPct } from '../lib/format'

export default function LiveTradingPage() {
  const { data: pumpfunStatus } = useQuery({
    queryKey: ['pumpfun-status'],
    queryFn: () => api.get(endpoints.pumpfunStatus).then(r => r.data),
    refetchInterval: 5000,
  })

  const { data: live } = useQuery({
    queryKey: ['live-status'],
    queryFn: () => api.get(endpoints.liveStatus).then(r => r.data),
    refetchInterval: 5000,
  })

  const { data: wallet } = useQuery({
    queryKey: ['pumpfun-wallet-live'],
    queryFn: () => api.get(endpoints.pumpfunWalletBalance).then(r => r.data),
    refetchInterval: 5000,
  })

  const { data: tokens } = useQuery({
    queryKey: ['pumpfun-trending-live'],
    queryFn: () => api.get(endpoints.pumpfunTokensTrending, { params: { limit: 5 } }).then(r => r.data?.tokens || []),
    refetchInterval: 10000,
  })

  const { data: fomoStatus } = useQuery({
    queryKey: ['fomo-status'],
    queryFn: () => api.get(endpoints.fomoStatus).then(r => r.data),
    refetchInterval: 10000,
  })

  const daily = live?.daily || {}
  const missing: string[] = live?.missing_requirements || []
  const canTrade = !!live?.can_open

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">التداول الحقيقي على Pump.fun</h1>
        <span className={`badge ${canTrade ? 'badge-success' : 'badge-danger'}`}>
          {canTrade ? '🟢 جاهز للتداول' : '🔴 غير جاهز'}
        </span>
      </div>

      {missing.length > 0 && (
        <div className="card border-red-500/40 bg-red-500/5">
          <div className="flex items-start gap-3">
            <AlertTriangle className="text-red-400 mt-0.5" size={18} />
            <div>
              <p className="font-medium text-red-300">التداول الحقيقي غير مفعّل</p>
              <p className="text-xs text-gray-400 mt-1 font-mono">{missing.join(' · ')}</p>
            </div>
          </div>
        </div>
      )}

      {Number(wallet?.sol_balance || 0) <= 0 && (
        <div className="card border-amber-500/40 bg-amber-500/5">
          <div className="flex items-start gap-3">
            <AlertTriangle className="text-amber-400 mt-0.5" size={18} />
            <div>
              <p className="font-medium text-amber-300">المحفظة فارغة (0 SOL)</p>
              <p className="text-xs text-gray-400 mt-1">
                أرسل SOL إلى <span className="font-mono">{wallet?.wallet_address || pumpfunStatus?.wallet_address || '—'}</span> قبل تشغيل التداول الحقيقي.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Status */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatusCard
          icon={<Wallet size={20} />}
          label="رصيد SOL"
          value={`${Number(wallet?.sol_balance || 0).toFixed(6)} SOL`}
          sub={wallet?.usd_value ? `≈ ${fmtUsd(wallet.usd_value)}` : 'لا يوجد رصيد'}
          color="text-accent-blue"
        />
        <StatusCard
          icon={<Rocket size={20} />}
          label="حالة التداول"
          value={live?.configured ? '🟢 مفعّل' : '🔴 متوقف'}
          sub={live?.trading_mode || 'paper'}
          color={live?.configured ? 'text-emerald-400' : 'text-red-400'}
        />
        <StatusCard
          icon={<Zap size={20} />}
          label="حد الصفقة"
          value={`${live?.max_trade_sol ?? 0.1} SOL`}
          sub={`Slippage: ${(pumpfunStatus?.slippage_bps ?? 500) / 100}%`}
          color="text-amber-400"
        />
        <StatusCard
          icon={<Activity size={20} />}
          label="صفقات مفتوحة"
          value={`${live?.open_live_positions ?? 0} / ${live?.max_open_positions ?? 3}`}
          sub={`PnL اليوم: ${(daily.sol_pnl ?? 0).toFixed(4)} SOL`}
          color="text-accent-purple"
        />
      </div>

      {/* Daily budget */}
      <div className="card p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3 flex items-center gap-2">
          <Shield size={16} /> الحدود اليومية
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <Metric label="المصروف اليوم" value={`${(daily.sol_spent ?? 0).toFixed(4)} / ${live?.daily_cap_sol ?? 0.5} SOL`} />
          <Metric label="PnL اليوم" value={`${(daily.sol_pnl ?? 0).toFixed(4)} SOL`} tone={(daily.sol_pnl ?? 0) >= 0 ? 'good' : 'bad'} />
          <Metric label="حد الخسارة" value={`${live?.daily_loss_limit_sol ?? 0.2} SOL`} />
          <Metric label="صفقات اليوم" value={`${daily.trades_opened ?? 0} مفتوحة / ${daily.trades_closed ?? 0} مغلقة`} />
        </div>
      </div>

      {/* Wallet info */}
      <div className="card p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">معلومات المحفظة والاتصال</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <p className="text-xs text-gray-500 mb-1">عنوان المحفظة</p>
            <p className="font-mono text-sm break-all">{wallet?.wallet_address || 'غير مربوطة'}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500 mb-1 flex items-center gap-1"><KeyRound size={12} /> PumpPortal API Key</p>
            <p className="font-mono text-sm">{pumpfunStatus?.api_key_masked || 'غير مضبوط'}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500 mb-1">PumpPortal</p>
            <p className="text-sm">{pumpfunStatus?.pumportal_base_url || '—'}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500 mb-1">مفتاح المحفظة الخاصة</p>
            <p className="text-sm">{pumpfunStatus?.private_key_configured ? '✅ مضبوط' : '❌ غير مضبوط'}</p>
          </div>
        </div>
      </div>

      {/* Trending tokens */}
      <div className="card p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">🔥 عملات رائجة الآن</h3>
        <div className="space-y-2">
          {(!tokens || tokens.length === 0) && (
            <p className="text-gray-500 text-sm">لا توجد عملات رائجة حالياً</p>
          )}
          {tokens?.map((t: any, i: number) => (
            <div key={t.address || i} className="flex items-center justify-between p-2 rounded-lg hover:bg-bg-soft">
              <div className="flex items-center gap-3">
                <span className="text-lg">#{i + 1}</span>
                <div>
                  <p className="font-medium text-sm">{t.symbol || '?'}</p>
                  <p className="text-xs text-gray-500">{t.name || (t.address ? t.address.slice(0, 8) + '...' : '')}</p>
                </div>
              </div>
              <div className="text-left">
                <p className="font-mono text-sm">{t.price_usd ? `$${t.price_usd.toFixed(8)}` : '—'}</p>
                <p className="text-xs text-gray-500">
                  MC: {t.market_cap_usd ? `$${(t.market_cap_usd / 1000).toFixed(1)}K` : '—'}
                </p>
              </div>
              <div className="text-center">
                {t.bonding_curve_progress != null ? (
                  <>
                    <p className="text-sm font-medium">{t.bonding_curve_progress.toFixed(0)}%</p>
                    <p className="text-xs text-gray-400">منحنى (مقدر)</p>
                  </>
                ) : (
                  <>
                    <p className="text-sm font-medium text-gray-400">—</p>
                    <p className="text-xs text-gray-400">غير متاح</p>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* FOMO */}
      <div className="card p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">📡 حالة FOMO</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div>
            <p className="text-xs text-gray-500">Bitquery</p>
            <p className="font-medium">{fomoStatus?.bitquery_api_configured ? '✅ مفعّل' : '❌ غير مفعّل'}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">الشبكات</p>
            <p className="font-medium">{fomoStatus?.supported_chains?.join(', ') || '—'}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">FOMO Signer</p>
            <p className="font-mono text-xs break-all">{fomoStatus?.fomo_signer_wallet?.slice(0, 12) || '—'}...</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">الحالة</p>
            <p className="font-medium">{fomoStatus?.bitquery_api_configured ? '🟢 جاهز' : '🔴 يحتاج API key'}</p>
          </div>
        </div>
      </div>
    </div>
  )
}

function StatusCard({ icon, label, value, sub, color }: { icon: React.ReactNode; label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="card p-4">
      <div className="flex items-center gap-3 mb-2">
        <div className="w-10 h-10 rounded-lg bg-bg-soft flex items-center justify-center text-accent-blue">
          {icon}
        </div>
        <p className="text-xs text-gray-500">{label}</p>
      </div>
      <p className={`text-lg font-bold ${color || ''}`}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
    </div>
  )
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: 'good' | 'bad' }) {
  return (
    <div>
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={`font-medium ${tone === 'good' ? 'text-emerald-400' : tone === 'bad' ? 'text-red-400' : ''}`}>{value}</p>
    </div>
  )
}
