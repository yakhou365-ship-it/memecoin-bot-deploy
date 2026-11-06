import { useQuery, useMutation } from '@tanstack/react-query'
import { toast } from 'sonner'
import { useState } from 'react'
import { Activity, ShieldCheck, BarChart3, FlaskConical, Eye, Loader2 } from 'lucide-react'
import { api } from '../lib/api'
import { fmtUsd, fmtPct } from '../lib/format'

const TABS = [
  { id: 'health', label: 'الصحة', icon: Activity },
  { id: 'audit', label: 'التدقيق', icon: ShieldCheck },
  { id: 'montecarlo', label: 'Monte Carlo', icon: BarChart3 },
  { id: 'strategylab', label: 'مختبر الاستراتيجيات', icon: FlaskConical },
  { id: 'counterfactual', label: 'الفرص الضائعة', icon: Eye },
]

export default function ResearchPage() {
  const [activeTab, setActiveTab] = useState('health')

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-bold">مركز البحث والتطوير</h1>
        <p className="text-sm text-gray-500 mt-1">Monte Carlo + Independent Auditor + Strategy Lab + Counterfactual Analysis</p>
      </div>

      {/* Tabs */}
      <div className="flex gap-2 flex-wrap">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`btn ${activeTab === tab.id ? 'bg-accent-blue text-white' : 'bg-bg-soft text-gray-400'}`}
          >
            <tab.icon size={16} className="inline ml-2" />
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'health' && <HealthTab />}
      {activeTab === 'audit' && <AuditTab />}
      {activeTab === 'montecarlo' && <MonteCarloTab />}
      {activeTab === 'strategylab' && <StrategyLabTab />}
      {activeTab === 'counterfactual' && <CounterfactualTab />}
    </div>
  )
}

function HealthTab() {
  const { data, isLoading } = useQuery({
    queryKey: ['health-monitor'],
    queryFn: () => api.get('/health-monitor').then(r => r.data),
    refetchInterval: 10000,
  })

  if (isLoading) return <div className="card">جاري التحميل...</div>
  if (!data) return <div className="card">لا توجد بيانات</div>

  return (
    <div className="space-y-4">
      <div className={`card ${data.trading_blocked ? 'bg-red-500/10 border-red-500/30' : 'bg-emerald-500/10 border-emerald-500/30'}`}>
        <h3 className="font-medium mb-2">حالة النظام</h3>
        <p className={`text-lg font-bold ${data.trading_blocked ? 'text-red-400' : 'text-emerald-400'}`}>
          {data.trading_blocked ? '🚫 التداول موقوف' : '✅ النظام يعمل'}
        </p>
        {data.trading_blocked && data.block_reason && (
          <p className="text-sm text-red-400 mt-1">السبب: {data.block_reason}</p>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        {(data.checks || []).map((check: any, i: number) => (
          <div key={i} className={`card py-3 ${check.status === 'fail' ? 'bg-red-500/10' : check.status === 'warn' ? 'bg-amber-500/10' : ''}`}>
            <p className="text-xs text-gray-500">{check.name}</p>
            <p className={`font-bold ${check.status === 'fail' ? 'text-red-400' : check.status === 'warn' ? 'text-amber-400' : 'text-emerald-400'}`}>
              {check.status === 'pass' ? '✅' : check.status === 'warn' ? '⚠️' : '❌'} {check.value || check.status}
            </p>
            {check.message && <p className="text-xs text-gray-500 mt-1">{check.message}</p>}
          </div>
        ))}
      </div>
    </div>
  )
}

function AuditTab() {
  const { data, isLoading } = useQuery({
    queryKey: ['audit'],
    queryFn: () => api.get('/audit').then(r => r.data),
  })

  if (isLoading) return <div className="card">جاري التدقيق...</div>
  if (!data) return <div className="card">لا توجد بيانات</div>

  return (
    <div className="space-y-4">
      <div className={`card ${data.verdict === 'rejected' ? 'bg-red-500/10 border-red-500/30' : data.verdict === 'needs_review' ? 'bg-amber-500/10 border-amber-500/30' : 'bg-emerald-500/10 border-emerald-500/30'}`}>
        <h3 className="font-medium mb-2">نتيجة التدقيق المستقل</h3>
        <p className={`text-xl font-bold ${data.verdict === 'rejected' ? 'text-red-400' : data.verdict === 'needs_review' ? 'text-amber-400' : 'text-emerald-400'}`}>
          {data.verdict === 'approved' ? '✅ مقبول' : data.verdict === 'needs_review' ? '⚠️ يحتاج مراجعة' : '❌ مرفوض'}
        </p>
        {data.summary && <p className="text-sm text-gray-400 mt-2">{data.summary}</p>}
      </div>

      {data.checks && (
        <div className="card">
          <h3 className="font-medium mb-3">فحوصات التدقيق</h3>
          <div className="space-y-2">
            {data.checks.map((check: any, i: number) => (
              <div key={i} className="flex items-center justify-between bg-bg-soft p-2 rounded">
                <span className="text-sm">{check.name}</span>
                <span className={`badge ${check.passed ? 'badge-success' : 'badge-danger'}`}>
                  {check.passed ? '✅ نجح' : '❌ فشل'}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function MonteCarloTab() {
  const mutation = useMutation({
    mutationFn: () => api.post('/monte-carlo/simulate', { num_trades: 100, num_simulations: 500 }).then(r => r.data),
    onSuccess: () => toast.success('اكتملت المحاكاة'),
    onError: () => toast.error('فشل المحاكاة'),
  })

  const data = mutation.data

  return (
    <div className="space-y-4">
      <div className="card">
        <h3 className="font-medium mb-3">محاكاة Monte Carlo</h3>
        <button
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
          className="btn-primary"
        >
          {mutation.isPending ? <Loader2 size={16} className="animate-spin inline ml-2" /> : null}
          تشغيل 500 محاكاة
        </button>
      </div>

      {data && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatCard label="P5 PnL" value={fmtUsd(data.percentiles?.p5)} negative />
          <StatCard label="P25 PnL" value={fmtUsd(data.percentiles?.p25)} />
          <StatCard label="P50 PnL (متوسط)" value={fmtUsd(data.percentiles?.p50)} positive />
          <StatCard label="P75 PnL" value={fmtUsd(data.percentiles?.p75)} positive />
          <StatCard label="P95 PnL (أفضل حالة)" value={fmtUsd(data.percentiles?.p95)} positive />
          <StatCard label="Risk of Ruin" value={`${((data.risk_of_ruin || 0) * 100).toFixed(1)}%`} negative />
          <StatCard label="Max Drawdown (متوسط)" value={fmtPct(data.avg_max_drawdown)} negative />
          <StatCard label="Win Rate (متوسط)" value={`${((data.avg_win_rate || 0) * 100).toFixed(1)}%`} />
        </div>
      )}
    </div>
  )
}

function StrategyLabTab() {
  const { data } = useQuery({
    queryKey: ['strategy-experiments'],
    queryFn: () => api.get('/strategy-lab/experiments').then(r => r.data),
  })

  return (
    <div className="card">
      <h3 className="font-medium mb-3">مختبر الاستراتيجيات (Champion vs Challenger)</h3>
      {data?.champion && (
        <div className="bg-emerald-500/10 border border-emerald-500/30 rounded-lg p-3 mb-4">
          <p className="text-emerald-400 font-medium">🏆 البطل الحالي: {data.champion.name}</p>
          <p className="text-sm text-gray-400 mt-1">
            Win Rate: {((data.champion.win_rate || 0) * 100).toFixed(1)}% | PF: {data.champion.profit_factor?.toFixed(2)}
          </p>
        </div>
      )}
      {data?.experiments && (
        <div className="space-y-2">
          {data.experiments.map((exp: any, i: number) => (
            <div key={i} className="bg-bg-soft p-3 rounded">
              <div className="flex justify-between">
                <span className="text-sm font-medium">{exp.name}</span>
                <span className={`badge ${exp.promoted ? 'badge-success' : exp.rejected ? 'badge-danger' : 'badge-warning'}`}>
                  {exp.promoted ? '✅ تم الترقية' : exp.rejected ? '❌ مرفوض' : '⏳ قيد الاختبار'}
                </span>
              </div>
              <p className="text-xs text-gray-500 mt-1">WR: {((exp.win_rate || 0) * 100).toFixed(1)}% | PF: {exp.profit_factor?.toFixed(2)}</p>
            </div>
          ))}
        </div>
      )}
      {!data?.experiments?.length && <p className="text-gray-500 text-sm">لا توجد تجارب بعد</p>}
    </div>
  )
}

function CounterfactualTab() {
  const { data } = useQuery({
    queryKey: ['counterfactual-report'],
    queryFn: () => api.get('/counterfactual/report').then(r => r.data),
  })

  return (
    <div className="card">
      <h3 className="font-medium mb-3">تحليل الفرص الضائعة (Counterfactual)</h3>
      {data?.summary && (
        <div className="grid grid-cols-3 gap-3 mb-4">
          <StatCard label="فرص مرفوضة" value={data.summary.total_rejected || 0} />
          <StatCard label="رفض صحيح" value={data.summary.correct_rejections || 0} positive />
          <StatCard label="رفض خاطئ" value={data.summary.incorrect_rejections || 0} negative />
        </div>
      )}
      {data?.records && data.records.length > 0 ? (
        <div className="overflow-x-auto scrollbar">
          <table className="w-full text-sm">
            <thead className="text-gray-400 text-xs">
              <tr>
                <th className="text-right p-2">العملة</th>
                <th className="text-right p-2">سبب الرفض</th>
                <th className="text-right p-2">السعر عند الرفض</th>
                <th className="text-right p-2">السعر بعدها</th>
                <th className="text-right p-2">التغير</th>
                <th className="text-right p-2">النتيجة</th>
              </tr>
            </thead>
            <tbody>
              {data.records.slice(0, 15).map((r: any, i: number) => (
                <tr key={i} className="border-t border-bg-border">
                  <td className="p-2">{r.token_symbol}</td>
                  <td className="p-2 text-xs">{r.rejection_reason}</td>
                  <td className="p-2 font-mono text-xs">${r.price_at_rejection?.toFixed(8)}</td>
                  <td className="p-2 font-mono text-xs">${r.price_after?.toFixed(8)}</td>
                  <td className={`p-2 ${(r.price_change_pct || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {(r.price_change_pct || 0) >= 0 ? '+' : ''}{(r.price_change_pct || 0).toFixed(2)}%
                  </td>
                  <td className="p-2">
                    <span className={`badge ${r.correct_rejection ? 'badge-success' : 'badge-danger'}`}>
                      {r.correct_rejection ? '✅ رفض صحيح' : '❌ فرصة ضائعة'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-gray-500 text-sm">لا توجد فرص مرفوضة مسجلة بعد</p>
      )}
    </div>
  )
}

function StatCard({ label, value, positive, negative }: { label: string; value: any; positive?: boolean; negative?: boolean }) {
  return (
    <div className="bg-bg-soft rounded-lg p-3">
      <p className="text-xs text-gray-500">{label}</p>
      <p className={`font-bold text-lg ${positive ? 'text-emerald-400' : negative ? 'text-red-400' : ''}`}>{value}</p>
    </div>
  )
}
