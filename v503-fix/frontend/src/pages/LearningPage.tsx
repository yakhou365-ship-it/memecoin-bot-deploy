import { useQuery } from '@tanstack/react-query'
import { Brain, AlertTriangle, Lightbulb, CheckCircle2, XCircle } from 'lucide-react'
import { api, endpoints } from '../lib/api'
import { fmtTime } from '../lib/format'

const MISTAKE_TYPE_AR: Record<string, string> = {
  late_entry: 'دخول متأخر',
  fake_breakout: 'اختراق وهمي',
  weak_liquidity: 'سيولة ضعيفة',
  ignored_indicator: 'تجاهل مؤشر',
  stale_data: 'بيانات قديمة',
  bad_market_regime: 'ظروف سوق غير مناسبة',
  bad_stop_loss: 'Stop Loss غير مناسب',
  overconfidence: 'توقعات مبالغ فيها',
  suspicious_activity: 'نشاط مشبوه',
  model_overestimate: 'النموذج بالغ في التقدير',
  high_volatility: 'تقلب عالٍ',
  momentum_reversal: 'انعكاس الزخم',
  unknown: 'غير معروف',
}

export default function LearningPage() {
  const { data: mistakes } = useQuery({ queryKey: ['mistakes'], queryFn: () => api.get(endpoints.mistakes).then(r => r.data) })
  const { data: learnings } = useQuery({ queryKey: ['learnings'], queryFn: () => api.get(endpoints.learnings).then(r => r.data) })
  const { data: analysis } = useQuery({ queryKey: ['learning-analysis'], queryFn: () => api.get(endpoints.learning).then(r => r.data) })

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-2xl font-bold flex items-center gap-2">
        <Brain size={24} className="text-accent-purple" />
        نظام التعلّم الذاتي
      </h1>

      {/* Improvement Analysis */}
      <div className="card">
        <h2 className="text-lg font-medium mb-3">تحليل التحسين المستمر (آخر 7 أيام)</h2>
        {analysis?.status === 'insufficient_data' && (
          <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-4 text-amber-400">
            {analysis.message}
          </div>
        )}
        {analysis?.status === 'ok' && (
          <div className="space-y-4">
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              <Stat label="الصفقات" value={analysis.trades_analyzed} />
              <Stat label="Win Rate" value={`${analysis.win_rate_pct.toFixed(1)}%`} />
              <Stat label="Profit Factor" value={analysis.profit_factor.toFixed(2)} />
              <Stat label="متوسط الربح" value={`$${analysis.avg_win_usd.toFixed(2)}`} />
              <Stat label="متوسط الخسارة" value={`$${analysis.avg_loss_usd.toFixed(2)}`} />
            </div>

            {analysis.exit_reason_breakdown && (
              <div>
                <p className="text-sm text-gray-400 mb-2">أداء أسباب الخروج</p>
                <div className="space-y-2">
                  {Object.entries(analysis.exit_reason_breakdown).map(([reason, stats]: [string, any]) => (
                    <div key={reason} className="flex items-center justify-between bg-bg-soft rounded p-2">
                      <span className="text-sm">{reason}</span>
                      <div className="flex gap-3 text-xs">
                        <span className="text-gray-400">{stats.count} صفقة</span>
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

            {analysis.recommendations?.length > 0 && (
              <div>
                <p className="text-sm text-gray-400 mb-2">التوصيات</p>
                <ul className="space-y-1">
                  {analysis.recommendations.map((rec: string, i: number) => (
                    <li key={i} className="text-sm flex items-start gap-2">
                      <Lightbulb size={14} className="text-accent-yellow mt-0.5" />
                      <span>{rec}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Mistakes Database */}
      <div className="card">
        <h2 className="text-lg font-medium mb-3 flex items-center gap-2">
          <AlertTriangle size={18} className="text-accent-red" />
          ذاكرة الأخطاء ({mistakes?.length || 0})
        </h2>
        <div className="space-y-3 max-h-96 overflow-auto scrollbar">
          {mistakes?.length === 0 && <p className="text-gray-500 text-sm">لا توجد أخطاء مسجلة بعد</p>}
          {mistakes?.map((m: any) => (
            <div key={m.id} className={`bg-bg-soft rounded-lg p-3 ${m.active_block ? 'border-l-2 border-red-500' : ''}`}>
              <div className="flex items-start justify-between mb-2">
                <div>
                  <span className="badge-warning">{MISTAKE_TYPE_AR[m.mistake_type] || m.mistake_type}</span>
                  {m.active_block && <span className="badge-danger mr-2">🚫 منع نشط</span>}
                </div>
                <span className="text-xs text-gray-500">{fmtTime(m.created_at)}</span>
              </div>
              <p className="text-sm text-gray-300 mb-2">{m.description}</p>
              {m.evidence?.length > 0 && (
                <div className="mb-2">
                  <p className="text-xs text-gray-500 mb-1">الأدلة:</p>
                  <ul className="text-xs space-y-1">
                    {m.evidence.map((e: string, i: number) => (
                      <li key={i} className="text-gray-400">• {e}</li>
                    ))}
                  </ul>
                </div>
              )}
              {Object.keys(m.suggested_fix || {}).length > 0 && (
                <div className="bg-bg-card rounded p-2">
                  <p className="text-xs text-accent-yellow mb-1">الإصلاح المقترح:</p>
                  <ul className="text-xs space-y-1">
                    {Object.entries(m.suggested_fix).map(([k, v]: [string, any]) => (
                      <li key={k}>• {v}</li>
                    ))}
                  </ul>
                  <div className="mt-2 flex gap-2 text-xs">
                    {m.fix_tested ? (
                      m.fix_test_result === 'improved' ? (
                        <span className="badge-success"><CheckCircle2 size={12} /> تم الاعتماد</span>
                      ) : m.fix_test_result === 'worse' ? (
                        <span className="badge-danger"><XCircle size={12} /> تم الرفض</span>
                      ) : (
                        <span className="badge-neutral">محايد</span>
                      )
                    ) : (
                      <span className="badge-warning">في انتظار الاختبار</span>
                    )}
                  </div>
                </div>
              )}
              {m.occurrence_count > 1 && (
                <p className="text-xs text-amber-400 mt-2">⚠️ تكرر {m.occurrence_count} مرة</p>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Learnings */}
      <div className="card">
        <h2 className="text-lg font-medium mb-3">الدروس المستفادة ({learnings?.length || 0})</h2>
        <div className="space-y-2">
          {learnings?.length === 0 && <p className="text-gray-500 text-sm">لا توجد دروس بعد</p>}
          {learnings?.map((l: any) => (
            <div key={l.id} className="bg-bg-soft rounded-lg p-3">
              <div className="flex items-start justify-between mb-2">
                <span className={`badge ${l.learning_type === 'success_factor' ? 'badge-success' : 'badge-warning'}`}>
                  {l.learning_type === 'success_factor' ? 'عامل نجاح' : 'نمط'}
                </span>
                <span className="text-xs text-gray-500">الثقة: {(l.confidence * 100).toFixed(0)}%</span>
              </div>
              <p className="font-medium text-sm mb-1">{l.pattern_name}</p>
              <p className="text-sm text-gray-400">{l.description}</p>
              <div className="text-xs text-gray-500 mt-2">
                {l.supporting_trades} صفقة مؤيدة · {l.contradicting_trades} معارضة
              </div>
            </div>
          ))}
        </div>
      </div>
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
