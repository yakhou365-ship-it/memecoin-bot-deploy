import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Brain, TrendingUp, Activity, Play, Loader2, AlertTriangle } from 'lucide-react'
import { api, endpoints } from '../lib/api'
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
  RadarChart, PolarGrid, PolarAngleAxis, PolarRadiusAxis, Radar,
  PieChart, Pie, Cell, Legend,
} from 'recharts'

export default function AnalyticsPage() {
  const qc = useQueryClient()
  const { data: trades } = useQuery({
    queryKey: ['all-trades'],
    queryFn: () => api.get(endpoints.trades, { params: { limit: 500, status: 'closed' } }).then(r => r.data as any[]),
  })
  const { data: mlModels } = useQuery({ queryKey: ['ml-models'], queryFn: () => api.get(endpoints.mlModels).then(r => r.data) })
  const { data: analysis } = useQuery({ queryKey: ['learning-analysis'], queryFn: () => api.get(endpoints.learning).then(r => r.data) })

  const trainMutation = useMutation({
    mutationFn: () => api.post(endpoints.mlTrain).then(r => r.data),
    onSuccess: (data: any) => {
      if (data.status === 'success') {
        toast.success(`تم التدريب على ${data.labelled_samples} صفقة حقيقية`)
        qc.invalidateQueries({ queryKey: ['ml-models'] })
      } else if (data.status === 'insufficient_data') {
        toast.warning(data.message)
      } else {
        toast.info(data.message || data.status)
      }
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'فشل التدريب'),
  })

  // Compute exit-reason distribution
  const exitReasonData = (() => {
    if (!trades || trades.length === 0) return []
    const counts: Record<string, number> = {}
    trades.forEach((t) => {
      const r = t.exit_reason || 'unknown'
      counts[r] = (counts[r] || 0) + 1
    })
    return Object.entries(counts).map(([name, value]) => ({ name, value }))
  })()

  // PnL distribution
  const pnlDistribution = (() => {
    if (!trades || trades.length === 0) return []
    const buckets = [
      { name: '<-10%', min: -Infinity, max: -10, count: 0 },
      { name: '-10% to -5%', min: -10, max: -5, count: 0 },
      { name: '-5% to 0%', min: -5, max: 0, count: 0 },
      { name: '0% to 5%', min: 0, max: 5, count: 0 },
      { name: '5% to 10%', min: 5, max: 10, count: 0 },
      { name: '>10%', min: 10, max: Infinity, count: 0 },
    ]
    trades.forEach((t) => {
      const pnl = t.pnl_pct || 0
      buckets.forEach((b) => {
        if (pnl >= b.min && pnl < b.max) b.count++
      })
    })
    return buckets.map(({ name, count }) => ({ name, count }))
  })()

  // Average score per trade outcome (radar)
  const scoreComparison = (() => {
    if (!trades || trades.length === 0) return []
    const winners = trades.filter((t) => (t.net_pnl_usd || 0) > 0)
    const losers = trades.filter((t) => (t.net_pnl_usd || 0) <= 0)
    // We'd need snapshots, but for now show trade-level stats
    return [
      { metric: 'Win Rate', winners: 100, losers: 0 },
      { metric: 'Avg PnL', winners: winners.reduce((s, t) => s + (t.pnl_pct || 0), 0) / Math.max(winners.length, 1), losers: losers.reduce((s, t) => s + (t.pnl_pct || 0), 0) / Math.max(losers.length, 1) },
    ]
  })()

  const COLORS = ['#10b981', '#ef4444', '#f59e0b', '#3b82f6', '#8b5cf6', '#ec4899']

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-2xl font-bold">التحليلات</h1>

      {/* Charts grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* PnL Distribution */}
        <div className="card">
          <h3 className="text-sm font-medium text-gray-400 mb-3">توزيع الأرباح والخسائر</h3>
          {pnlDistribution.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={pnlDistribution}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a3245" />
                <XAxis dataKey="name" stroke="#64748b" fontSize={10} />
                <YAxis stroke="#64748b" fontSize={11} />
                <Tooltip
                  contentStyle={{ backgroundColor: '#1a2033', border: '1px solid #2a3245', borderRadius: 8 }}
                />
                <Bar dataKey="count" name="عدد الصفقات" radius={[4, 4, 0, 0]}>
                  {pnlDistribution.map((_, i) => (
                    <Cell key={i} fill={i < 3 ? '#ef4444' : '#10b981'} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-gray-500 text-sm">لا توجد بيانات بعد</p>
          )}
        </div>

        {/* Exit reasons */}
        <div className="card">
          <h3 className="text-sm font-medium text-gray-400 mb-3">توزيع أسباب الخروج</h3>
          {exitReasonData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <PieChart>
                <Pie
                  data={exitReasonData}
                  cx="50%"
                  cy="50%"
                  outerRadius={80}
                  dataKey="value"
                  label={(e: any) => `${e.name}: ${e.value}`}
                  labelLine={false}
                >
                  {exitReasonData.map((_, i) => (
                    <Cell key={i} fill={COLORS[i % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip
                  contentStyle={{ backgroundColor: '#1a2033', border: '1px solid #2a3245', borderRadius: 8 }}
                />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-gray-500 text-sm">لا توجد بيانات بعد</p>
          )}
        </div>
      </div>

      {/* ML Models */}
      <div className="card">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium text-gray-400 flex items-center gap-2">
            <Brain size={16} className="text-accent-purple" />
            نماذج تعلم الآلة ({mlModels?.length || 0})
          </h3>
          <button
            onClick={() => trainMutation.mutate()}
            disabled={trainMutation.isPending}
            className="btn-primary text-sm"
            title="يتدرّب فقط على الصفقات المغلقة الحقيقية (30+ صفقة)"
          >
            {trainMutation.isPending
              ? <Loader2 size={14} className="animate-spin inline ml-1" />
              : <Play size={14} className="inline ml-1" />}
            تدريب النموذج
          </button>
        </div>

        {trainMutation.data?.status === 'insufficient_data' && (
          <div className="mb-3 p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 text-xs text-amber-300 flex items-start gap-2">
            <AlertTriangle size={14} className="mt-0.5" />
            <span>
              {trainMutation.data.message}
              <br />
              العينات المتاحة: {trainMutation.data.labelled_samples} / المطلوب: {trainMutation.data.required_samples}
            </span>
          </div>
        )}
        {trainMutation.data?.status === 'success' && (
          <div className="mb-3 p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-xs text-emerald-300">
            تم التدريب على {trainMutation.data.labelled_samples} صفقة حقيقية.
            الدقة: {trainMutation.data.metrics?.accuracy} · F1: {trainMutation.data.metrics?.f1} · ROC-AUC: {trainMutation.data.metrics?.roc_auc}
          </div>
        )}

        <div className="overflow-x-auto scrollbar">
          <table className="w-full text-sm">
            <thead className="text-gray-400">
              <tr>
                <th className="text-right p-2">الإصدار</th>
                <th className="text-right p-2">النوع</th>
                <th className="text-right p-2">Train</th>
                <th className="text-right p-2">Test</th>
                <th className="text-right p-2">Precision</th>
                <th className="text-right p-2">Recall</th>
                <th className="text-right p-2">F1</th>
                <th className="text-right p-2">ROC-AUC</th>
                <th className="text-right p-2">Brier Score</th>
                <th className="text-right p-2">Calibration Err</th>
                <th className="text-right p-2">الحالة</th>
              </tr>
            </thead>
            <tbody>
              {mlModels?.length === 0 && (
                <tr><td colSpan={11} className="text-center p-4 text-gray-500">لا توجد نماذج مدربة بعد</td></tr>
              )}
              {mlModels?.map((m: any) => (
                <tr key={m.id} className="border-t border-bg-border">
                  <td className="p-2">v{m.version}</td>
                  <td className="p-2">{m.model_type}</td>
                  <td className="p-2">{m.train_samples}</td>
                  <td className="p-2">{m.test_samples}</td>
                  <td className="p-2 text-emerald-400">{m.precision?.toFixed(2) || '—'}</td>
                  <td className="p-2">{m.recall?.toFixed(2) || '—'}</td>
                  <td className="p-2">{m.f1?.toFixed(2) || '—'}</td>
                  <td className="p-2">{m.roc_auc?.toFixed(3) || '—'}</td>
                  <td className="p-2">{m.brier_score?.toFixed(3) || '—'}</td>
                  <td className="p-2 text-amber-400">{m.calibration_error?.toFixed(3) || '—'}</td>
                  <td className="p-2">
                    <span className={`badge ${
                      m.status === 'adopted' ? 'badge-success' :
                      m.status === 'validated' ? 'badge-info' :
                      m.status === 'trained' ? 'badge-warning' :
                      'badge-neutral'
                    }`}>
                      {m.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Performance summary */}
      {analysis?.status === 'ok' && (
        <div className="card">
          <h3 className="text-sm font-medium text-gray-400 mb-3 flex items-center gap-2">
            <Activity size={16} className="text-accent-blue" />
            ملخص الأداء
          </h3>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            <Stat label="إجمالي الصفقات" value={analysis.trades_analyzed} />
            <Stat label="Win Rate" value={`${analysis.win_rate_pct.toFixed(1)}%`} />
            <Stat label="Profit Factor" value={analysis.profit_factor.toFixed(2)} />
            <Stat label="متوسط الربح" value={`$${analysis.avg_win_usd.toFixed(2)}`} />
            <Stat label="متوسط الخسارة" value={`$${analysis.avg_loss_usd.toFixed(2)}`} />
          </div>

          {analysis.strategy_performance && Object.keys(analysis.strategy_performance).length > 0 && (
            <div className="mt-4">
              <p className="text-sm text-gray-400 mb-2">أداء الاستراتيجيات</p>
              <div className="space-y-2">
                {Object.entries(analysis.strategy_performance).map(([name, stats]: [string, any]) => (
                  <div key={name} className="bg-bg-soft rounded p-2 flex justify-between text-sm">
                    <span>{name}</span>
                    <div className="flex gap-3 text-xs">
                      <span>{stats.count} صفقة</span>
                      <span className="text-emerald-400">{((stats.wins / stats.count) * 100).toFixed(0)}% نجاح</span>
                      <span className={stats.total_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                        ${stats.total_pnl.toFixed(2)}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: any }) {
  return (
    <div className="bg-bg-soft rounded-lg p-3">
      <p className="text-xs text-gray-500">{label}</p>
      <p className="font-bold text-lg">{value}</p>
    </div>
  )
}
