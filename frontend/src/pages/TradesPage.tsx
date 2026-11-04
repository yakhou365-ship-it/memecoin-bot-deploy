import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { api, endpoints } from '../lib/api'
import { fmtUsd, fmtPrice, fmtPct, fmtTime } from '../lib/format'
import type { Trade } from '../types'

export default function TradesPage() {
  const [filter, setFilter] = useState<'all' | 'open' | 'closed'>('all')
  const qc = useQueryClient()

  const { data: trades, isLoading } = useQuery({
    queryKey: ['trades', filter],
    queryFn: () => api.get(endpoints.trades, { params: { limit: 200, status: filter === 'all' ? undefined : filter } }).then(r => r.data as Trade[]),
  })

  const closeMutation = useMutation({
    mutationFn: (id: string) => api.post(endpoints.closeTrade(id), { exit_reason: 'manual' }).then(r => r.data),
    onSuccess: (data: any) => {
      toast.success(data?.live ? `تم البيع على السلسلة: ${data.tx_signature?.slice(0, 10)}...` : 'تم إغلاق الصفقة')
      qc.invalidateQueries({ queryKey: ['trades'] })
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'فشل الإغلاق'),
  })

  return (
    <div className="p-6 space-y-4">
      <h1 className="text-2xl font-bold">الصفقات</h1>

      <div className="flex gap-2">
        {(['all', 'open', 'closed'] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`btn ${filter === f ? 'bg-accent-blue text-white' : 'bg-bg-soft text-gray-400'}`}
          >
            {f === 'all' ? 'الكل' : f === 'open' ? 'مفتوحة' : 'مغلقة'}
          </button>
        ))}
      </div>

      <div className="card p-0 overflow-hidden">
        <div className="overflow-x-auto scrollbar">
          <table className="w-full text-sm">
            <thead className="bg-bg-soft text-gray-400">
              <tr>
                <th className="text-right p-3">العملة</th>
                <th className="text-right p-3">الحالة</th>
                <th className="text-right p-3">الدخول</th>
                <th className="text-right p-3">الحجم</th>
                <th className="text-right p-3">Stop Loss</th>
                <th className="text-right p-3">Take Profit</th>
                <th className="text-right p-3">الخروج</th>
                <th className="text-right p-3">صافي PnL</th>
                <th className="text-right p-3">%</th>
                <th className="text-right p-3">الرسوم</th>
                <th className="text-right p-3">الانزلاق</th>
                <th className="text-right p-3">سبب الخروج</th>
                <th className="text-right p-3">وقت الدخول</th>
                <th className="text-right p-3"></th>
              </tr>
            </thead>
            <tbody>
              {isLoading && (
                <tr><td colSpan={14} className="text-center p-8 text-gray-500">جاري التحميل...</td></tr>
              )}
              {!isLoading && trades?.length === 0 && (
                <tr><td colSpan={14} className="text-center p-8 text-gray-500">لا توجد صفقات</td></tr>
              )}
              {trades?.map((t) => (
                <tr key={t.id} className="border-t border-bg-border hover:bg-bg-soft">
                  <td className="p-3">
                    <div className="font-medium">{t.token_symbol || '—'}</div>
                    <div className="text-xs text-gray-500">{t.token_chain}</div>
                    {(t.strategy_id || '').startsWith('live') && (
                      <span className="badge badge-danger text-[10px] mt-1">🔴 حقيقية</span>
                    )}
                  </td>
                  <td className="p-3">
                    {t.status === 'open' ? (
                      <span className="badge-info">مفتوحة</span>
                    ) : t.status === 'closed' ? (
                      <span className="badge-neutral">مغلقة</span>
                    ) : (
                      <span className="badge-warning">{t.status}</span>
                    )}
                  </td>
                  <td className="p-3 font-mono text-xs">{fmtPrice(t.entry_price)}</td>
                  <td className="p-3">{fmtUsd(t.entry_size_usd, 0)}</td>
                  <td className="p-3 font-mono text-xs text-red-400">{t.stop_loss_price ? fmtPrice(t.stop_loss_price) : '—'}</td>
                  <td className="p-3 font-mono text-xs text-emerald-400">{t.take_profit_price ? fmtPrice(t.take_profit_price) : '—'}</td>
                  <td className="p-3 font-mono text-xs">{t.exit_price ? fmtPrice(t.exit_price) : '—'}</td>
                  <td className={`p-3 font-medium ${(t.net_pnl_usd || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {t.net_pnl_usd != null ? fmtUsd(t.net_pnl_usd) : '—'}
                  </td>
                  <td className={`p-3 ${(t.pnl_pct || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {fmtPct(t.pnl_pct)}
                  </td>
                  <td className="p-3 text-xs text-gray-500">{fmtUsd(t.fees_total_usd)}</td>
                  <td className="p-3 text-xs text-gray-500">{fmtUsd(t.slippage_total_usd)}</td>
                  <td className="p-3 text-xs">
                    {t.exit_reason && (
                      <span className={`badge ${
                        t.exit_reason === 'take_profit' ? 'badge-success' :
                        t.exit_reason === 'stop_loss' ? 'badge-danger' :
                        t.exit_reason === 'circuit_breaker' ? 'badge-danger' :
                        'badge-neutral'
                      }`}>
                        {exitReasonAr(t.exit_reason)}
                      </span>
                    )}
                  </td>
                  <td className="p-3 text-xs text-gray-500">{fmtTime(t.entry_timestamp)}</td>
                  <td className="p-3">
                    {t.status === 'open' && (
                      <button
                        onClick={() => closeMutation.mutate(t.id)}
                        className="btn-danger text-xs py-1 px-2"
                      >
                        إغلاق
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function exitReasonAr(r: string): string {
  return ({
    take_profit: 'Take Profit',
    stop_loss: 'Stop Loss',
    time_exit: 'انتهاء الوقت',
    circuit_breaker: 'Circuit Breaker',
    manual: 'يدوي',
  } as Record<string, string>)[r] || r
}
