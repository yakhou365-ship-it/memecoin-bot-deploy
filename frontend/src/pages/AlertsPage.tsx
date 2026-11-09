import { useQuery } from '@tanstack/react-query'
import { Bell, AlertTriangle, AlertCircle, Info } from 'lucide-react'
import { api, endpoints } from '../lib/api'
import { fmtTime } from '../lib/format'

const SEVERITY_CONFIG = {
  critical: { icon: AlertCircle, color: 'text-red-400', bg: 'bg-red-500/10', border: 'border-red-500/30' },
  warning: { icon: AlertTriangle, color: 'text-amber-400', bg: 'bg-amber-500/10', border: 'border-amber-500/30' },
  info: { icon: Info, color: 'text-blue-400', bg: 'bg-blue-500/10', border: 'border-blue-500/30' },
}

const ALERT_TYPE_AR: Record<string, string> = {
  new_token_discovered: 'عملة جديدة',
  opportunity_qualifying: 'فرصة قيد التأهيل',
  opportunity_qualified: 'فرصة مؤهلة',
  opportunity_rejected: 'فرصة مرفوضة',
  trade_opened: 'فتح صفقة',
  trade_closed: 'إغلاق صفقة',
  profit_realized: 'تحقيق ربح',
  loss_realized: 'تحقيق خسارة',
  scam_detected: 'اكتشاف احتيال',
  market_regime_changed: 'تغير حالة السوق',
  mistake_pattern_detected: 'نمط خطأ مكتشف',
  strategy_performance_change: 'تغير أداء الاستراتيجية',
  circuit_breaker_triggered: 'تفعيل قاطع الدائرة',
}

export default function AlertsPage() {
  const { data: alerts } = useQuery({ queryKey: ['alerts'], queryFn: () => api.get(endpoints.alerts, { params: { limit: 100 } }).then(r => r.data) })

  return (
    <div className="p-6 space-y-4">
      <h1 className="text-2xl font-bold flex items-center gap-2">
        <Bell size={24} className="text-accent-yellow" />
        التنبيهات
      </h1>

      <div className="space-y-2">
        {alerts?.length === 0 && <p className="text-gray-500 text-sm">لا توجد تنبيهات بعد</p>}
        {alerts?.map((a: any) => {
          const cfg = SEVERITY_CONFIG[a.severity as keyof typeof SEVERITY_CONFIG] || SEVERITY_CONFIG.info
          const Icon = cfg.icon
          return (
            <div key={a.id} className={`card ${cfg.bg} ${cfg.border}`}>
              <div className="flex items-start gap-3">
                <Icon size={18} className={`${cfg.color} mt-0.5`} />
                <div className="flex-1">
                  <div className="flex items-start justify-between">
                    <div>
                      <p className="font-medium">{a.title}</p>
                      <p className="text-xs text-gray-500 mt-0.5">
                        {ALERT_TYPE_AR[a.alert_type] || a.alert_type}
                      </p>
                    </div>
                    <span className="text-xs text-gray-500">{fmtTime(a.created_at)}</span>
                  </div>
                  <p className="text-sm text-gray-300 mt-2 whitespace-pre-wrap">{a.message}</p>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
