import { useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Bot, Shield, AlertTriangle, CheckCircle2, XCircle, Play, Loader2 } from 'lucide-react'
import { api, endpoints } from '../lib/api'

export default function AgentsPage() {
  const [tokenId, setTokenId] = useState('')

  const { data: agentsData, isLoading } = useQuery({
    queryKey: ['agents-list'],
    queryFn: () => api.get(endpoints.agentsList).then(r => r.data),
    refetchInterval: 5000,
  })

  const { data: status } = useQuery({
    queryKey: ['agents-status'],
    queryFn: () => api.get(endpoints.agentsStatus).then(r => r.data),
    refetchInterval: 5000,
  })

  const { data: tokens } = useQuery({
    queryKey: ['agents-tokens'],
    queryFn: () => api.get(endpoints.tokens, { params: { limit: 50 } }).then(r => r.data as any[]),
    refetchInterval: 15000,
  })

  const runMutation = useMutation({
    mutationFn: (id: string) => api.post(endpoints.runAgents, { token_id: id }).then(r => r.data),
    onSuccess: (data: any) => {
      const n = data?.approvals_count ?? 0
      const total = data?.total_agents ?? 5
      if (data?.approved) toast.success(`✅ إجماع: ${n}/${total} وكيل وافقوا`)
      else toast.warning(`⏸️ لا إجماع: ${n}/${total} فقط`)
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'فشل تشغيل الوكلاء'),
  })

  const agents = agentsData?.agents || []
  const result = runMutation.data

  return (
    <div className="p-6 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold">وكلاء الذكاء الاصطناعي</h1>
        <div className="flex items-center gap-2">
          <span className="badge badge-success">{agentsData?.total_agents || 0} وكيل</span>
          {agentsData?.require_unanimity && (
            <span className="badge badge-warning">إجماع مطلوب</span>
          )}
        </div>
      </div>

      {/* Run consensus */}
      <div className="card p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">تشغيل إجماع الوكلاء على عملة</h3>
        <div className="flex flex-wrap gap-2">
          <select
            value={tokenId}
            onChange={(e) => setTokenId(e.target.value)}
            className="flex-1 min-w-[220px] bg-bg-soft border border-bg-border rounded-lg px-3 py-2 text-sm"
          >
            <option value="">— اختر عملة —</option>
            {(tokens || []).map((t: any) => (
              <option key={t.id} value={t.id}>
                {t.symbol} {t.address ? `(${t.address.slice(0, 6)}…)` : ''}
              </option>
            ))}
          </select>
          <button
            onClick={() => tokenId && runMutation.mutate(tokenId)}
            disabled={!tokenId || runMutation.isPending}
            className="btn-primary text-sm disabled:opacity-50"
          >
            {runMutation.isPending
              ? <Loader2 size={14} className="animate-spin inline ml-1" />
              : <Play size={14} className="inline ml-1" />}
            تشغيل الوكلاء
          </button>
        </div>
        {(!tokens || tokens.length === 0) && (
          <p className="text-xs text-gray-500 mt-2">
            لا توجد عملات محفوظة بعد. شغّل الماسح أولًا من صفحة العملات أو انتظر دورة المسح.
          </p>
        )}
        {result && (
          <div className={`mt-3 p-3 rounded-lg text-xs border ${result.approved
            ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300'
            : 'bg-amber-500/10 border-amber-500/30 text-amber-300'}`}>
            <p className="font-medium">
              {result.approved ? '✅ تم التوافق' : '⏸️ لا يوجد توافق'} — {result.approvals_count}/{result.total_agents}
            </p>
            {result.final_decision && <p className="mt-1">القرار: {result.final_decision}</p>}
            {result.final_reasoning && <p className="mt-1 text-gray-400">{result.final_reasoning}</p>}
          </div>
        )}
      </div>

      {/* Status banner */}
      {status?.latest_consensus && (
        <div className={`card p-4 ${status.latest_consensus.consensus_reached ? 'border-emerald-500/30' : 'border-amber-500/30'}`}>
          <div className="flex items-center gap-3">
            {status.latest_consensus.consensus_reached ? (
              <CheckCircle2 className="text-emerald-400" size={24} />
            ) : (
              <XCircle className="text-amber-400" size={24} />
            )}
            <div>
              <p className="font-medium">
                {status.latest_consensus.consensus_reached ? 'تم التوصل لإجماع' : 'لم يتم التوصل لإجماع'}
              </p>
              <p className="text-xs text-gray-500">
                {status.latest_consensus.decision || 'في الانتظار'}
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Agents grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {isLoading && (
          <div className="col-span-full text-center py-8 text-gray-500">جاري التحميل...</div>
        )}
        {agents.map((agent: any) => (
          <div key={agent.name} className="card p-4 space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Bot size={18} className="text-accent-blue" />
                <h3 className="font-medium text-sm">{agent.role || agent.name}</h3>
              </div>
              {agent.has_veto ? (
                <span className="badge badge-danger text-xs">⛔ VETO</span>
              ) : (
                <span className="badge badge-success text-xs">✅</span>
              )}
            </div>

            {agent.specialty && (
              <p className="text-xs text-gray-500">{agent.specialty}</p>
            )}

            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-500">الوزن</span>
              <span className="font-medium">{agent.weight?.toFixed(1)}x</span>
            </div>

            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-500">الحالة</span>
              <span className={`badge ${agent.enabled ? 'badge-success' : 'badge-neutral'} text-xs`}>
                {agent.enabled ? 'مفعّل' : 'معطّل'}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
