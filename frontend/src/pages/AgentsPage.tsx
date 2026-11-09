import { useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Bot, Shield, AlertTriangle, CheckCircle2, XCircle, Play, Loader2 } from 'lucide-react'
import { api, endpoints } from '../lib/api'

export default function AgentsPage() {
  const [tokenId, setTokenId] = useState('')

  const { data: agentsData, isLoading, isError: agentsErr } = useQuery({
    queryKey: ['agents-list'],
    queryFn: () => api.get(endpoints.agentsList).then(r => r.data),
    refetchInterval: 5000,
    retry: false,
  })

  const { data: status, isError: statusErr } = useQuery({
    queryKey: ['agents-status'],
    queryFn: () => api.get(endpoints.agentsStatus).then(r => r.data),
    refetchInterval: 5000,
    retry: false,
  })

  // DB tokens (persisted by the scanner)
  const { data: dbTokens, isError: dbTokensErr } = useQuery({
    queryKey: ['agents-tokens'],
    queryFn: () => api.get(endpoints.tokens, { params: { limit: 50 } }).then(r => r.data as any[]),
    refetchInterval: 15000,
    retry: false,
  })

  // Live tokens from DexScreener (not always persisted yet)
  const { data: liveTokens, isError: liveTokensErr } = useQuery({
    queryKey: ['agents-live-tokens'],
    queryFn: () => api.get(endpoints.marketLiveTokens, { params: { limit: 50 } }).then(r => r.data?.tokens || []),
    refetchInterval: 15000,
    retry: false,
  })

  // Unified dropdown: live tokens first (by mint), then any DB extras
  const seenMints = new Set<string>()
  const options: any[] = []
  for (const t of ((liveTokens as any[]) || [])) {
    const mint = t?.address
    if (mint && !seenMints.has(mint)) {
      seenMints.add(mint)
      options.push({ kind: 'mint', value: mint, label: `${t.symbol} (${mint.slice(0, 6)}…)` })
    }
  }
  for (const t of ((dbTokens as any[]) || [])) {
    const mint = t?.address
    if (mint && !seenMints.has(mint)) {
      seenMints.add(mint)
      options.push({ kind: 'id', value: t.id, label: `${t.symbol} (${mint.slice(0, 6)}…)` })
    }
  }

  const runMutation = useMutation({
    mutationFn: (opt: { kind: string; value: string }) =>
      (opt.kind === 'mint'
        ? api.post(endpoints.runAgentsByMint, { mint_address: opt.value })
        : api.post(endpoints.runAgents, { token_id: opt.value })
      ).then(r => r.data),
    onSuccess: (data: any) => {
      const n = data?.approvals_count ?? 0
      const total = data?.total_agents ?? 5
      if (data?.approved) toast.success(`✅ إجماع: ${n}/${total} وكيل وافقوا`)
      else if (data?.error) toast.error(`فشل: ${data.error}`)
      else toast.warning(`⏸️ لا إجماع: ${n}/${total} فقط`)
    },
    onError: (e: any) => {
      const status = e?.response?.status
      const detail = e?.response?.data?.detail || e?.message || 'خطأ غير معروف'
      if (status === 401) {
        toast.error('🔒 مفتاح API خاطئ. افتح الرابط مع #resetkey لإدخال المفتاح الصحيح.')
      } else if (status === 404) {
        toast.error('العملة غير موجودة في قاعدة البيانات. اختر عملة أخرى.')
      } else {
        toast.error(`فشل تشغيل الوكلاء (${status || '؟'}): ${detail}`)
      }
    },
  })

  const agents = agentsData?.agents || []
  const result = runMutation.data
  const selectedOption = options.find((o: any) => `${o.kind}:${o.value}` === tokenId)

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

        {/* Ultimate-bot: show auth error if queries fail */}
        {(agentsErr || dbTokensErr || liveTokensErr) && (
          <div className="mb-3 p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-sm text-red-400">
            <AlertTriangle size={16} className="inline ml-2" />
            فشل تحميل البيانات. تأكد من أن مفتاح API صحيح.
            افتح الرابط مع <span className="font-mono">#resetkey</span> لإدخال المفتاح الصحيح من Railway Variables.
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          <select
            value={tokenId}
            onChange={(e) => setTokenId(e.target.value)}
            className="flex-1 min-w-[220px] bg-bg-soft border border-bg-border rounded-lg px-3 py-2 text-sm"
          >
            <option value="">— اختر عملة —</option>
            {options.map((o: any) => (
              <option key={`${o.kind}:${o.value}`} value={`${o.kind}:${o.value}`}>
                {o.label}
              </option>
            ))}
          </select>
          <button
            onClick={() => selectedOption && runMutation.mutate(selectedOption)}
            disabled={!selectedOption || runMutation.isPending}
            className="btn-primary text-sm disabled:opacity-50"
          >
            {runMutation.isPending
              ? <Loader2 size={14} className="animate-spin inline ml-1" />
              : <Play size={14} className="inline ml-1" />}
            تشغيل الوكلاء
          </button>
        </div>
        {(options.length === 0) && (
          <p className="text-xs text-gray-500 mt-2">
            لا توجد عملات بعد. شغّل الماسح أولًا من صفحة العملات أو انتظر دورة المسح.
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
