import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Brain, Play, Target, Loader2, TrendingUp, CheckCircle2, XCircle, RefreshCw, Activity } from 'lucide-react'
import { api, endpoints } from '../lib/api'

export default function TrainingPage() {
  const [targetSamples, setTargetSamples] = useState(300)
  const [targetWinRate, setTargetWinRate] = useState(70)
  const qc = useQueryClient()

  const { data: stats } = useQuery({
    queryKey: ['training-stats'],
    queryFn: () => api.get('/ml/training-stats').then(r => r.data),
    refetchInterval: 5000,
  })

  const { data: mlModels } = useQuery({
    queryKey: ['ml-models'],
    queryFn: () => api.get("/ml/models").then(r => r.data),
  })

  const pipelineMutation = useMutation({
    mutationFn: () => api.post('/ml/training-pipeline', {
      target_samples: targetSamples,
      lookback_days: 30,
    }).then(r => r.data),
    onSuccess: (data) => {
      const wr = data?.data_generation?.win_rate || 0
      toast.success(`تدريب مكتمل! Win Rate: ${wr.toFixed(1)}%`)
      qc.invalidateQueries({ queryKey: ['training-stats'] })
      qc.invalidateQueries({ queryKey: ['ml-models'] })
    },
    onError: (err: any) => toast.error(err?.response?.data?.detail || 'فشل التدريب'),
  })

  const repeatedMutation = useMutation({
    mutationFn: () => api.post('/ml/train-until-target', {
      target_win_rate: targetWinRate,
      max_iterations: 3,
      samples_per_iteration: 200,
    }).then(r => r.data),
    onSuccess: (data) => {
      if (data.target_reached) {
        toast.success(`🎯 تم الوصول للهدف! Win Rate: ${data.final_win_rate?.toFixed(1)}%`)
      } else {
        toast.warning(`لم يصل للهدف بعد. Win Rate: ${data.final_win_rate?.toFixed(1)}%`)
      }
      qc.invalidateQueries({ queryKey: ['training-stats'] })
      qc.invalidateQueries({ queryKey: ['ml-models'] })
    },
    onError: (err: any) => toast.error(err?.response?.data?.detail || 'فشل التدريب المتكرر'),
  })

  const winRate = stats?.training_win_rate || 0
  const targetReached = stats?.target_reached || false
  const progressToTarget = Math.min(100, (winRate / 70) * 100)
  const isTraining = pipelineMutation.isPending || repeatedMutation.isPending

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Brain size={24} className="text-accent-purple" />
          تدريب الذكاء الاصطناعي
        </h1>
        <p className="text-sm text-gray-500 mt-1">
          توليد بيانات تدريب من السوق الحقيقي + تدريب نموذج ML لرفع نسبة الفوز فوق 70%
        </p>
      </div>

      {/* Win Rate Progress Card */}
      <div className="card bg-gradient-to-br from-accent-purple/10 to-accent-blue/10 border-accent-purple/30">
        <div className="flex items-start justify-between mb-4">
          <div>
            <p className="text-xs text-gray-500 mb-1">نسبة الفوز الحالية (Win Rate)</p>
            <p className={`text-4xl font-bold ${targetReached ? 'text-emerald-400' : 'text-amber-400'}`}>
              {winRate.toFixed(1)}%
            </p>
            <p className="text-xs text-gray-500 mt-1">الهدف: {targetWinRate}%</p>
          </div>
          <div className="text-right">
            {targetReached ? (
              <div className="flex items-center gap-2 text-emerald-400">
                <CheckCircle2 size={32} />
                <span className="text-sm font-medium">تم الوصول للهدف!</span>
              </div>
            ) : (
              <div className="flex items-center gap-2 text-amber-400">
                <Target size={32} />
                <span className="text-sm font-medium">في التقدّم</span>
              </div>
            )}
          </div>
        </div>

        {/* Progress bar */}
        <div className="w-full bg-bg-soft rounded-full h-3 mb-2">
          <div
            className={`h-full rounded-full transition-all duration-1000 ${
              targetReached ? 'bg-emerald-500' : 'bg-gradient-to-r from-amber-500 to-emerald-500'
            }`}
            style={{ width: `${progressToTarget}%` }}
          />
        </div>
        <div className="flex justify-between text-xs text-gray-500">
          <span>0%</span>
          <span>{progressToTarget.toFixed(0)}% من الهدف</span>
          <span>70%</span>
        </div>
      </div>

      {/* Training Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="عينات التدريب"
          value={stats?.training_samples || 0}
          icon={<Activity size={18} className="text-accent-blue" />}
        />
        <StatCard
          label="صفقات رابحة"
          value={stats?.training_wins || 0}
          icon={<TrendingUp size={18} className="text-emerald-400" />}
        />
        <StatCard
          label="صفقات حقيقية"
          value={stats?.real_trades || 0}
          icon={<Brain size={18} className="text-accent-purple" />}
        />
        <StatCard
          label="نموذج ML الحالي"
          value={stats?.latest_model ? `v${stats.latest_model.version}` : 'لا يوجد'}
          icon={<Target size={18} className="text-amber-400" />}
        />
      </div>

      {/* Training Controls */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Single training run */}
        <div className="card">
          <h3 className="text-sm font-medium text-gray-400 mb-3">تدريب منفرد</h3>
          <p className="text-xs text-gray-500 mb-3">يولّد بيانات جديدة + يدرّب نموذجاً جديداً</p>
          <div className="mb-3">
            <label className="text-xs text-gray-500 mb-1 block">عدد العينات المطلوبة</label>
            <input
              type="number"
              value={targetSamples}
              onChange={(e) => setTargetSamples(parseInt(e.target.value) || 200)}
              min={100}
              max={1000}
              className="w-full bg-bg-soft border border-bg-border rounded px-3 py-2 text-sm"
              disabled={isTraining}
            />
          </div>
          <button
            onClick={() => pipelineMutation.mutate()}
            disabled={isTraining}
            className="btn-primary w-full"
          >
            {pipelineMutation.isPending ? (
              <Loader2 size={16} className="animate-spin inline ml-2" />
            ) : (
              <Play size={16} className="inline ml-2" />
            )}
            {pipelineMutation.isPending ? 'جاري التدريب...' : 'ابدأ التدريب'}
          </button>
          {pipelineMutation.data && (
            <div className="mt-3 p-3 bg-bg-soft rounded text-sm">
              <p className="text-emerald-400 font-medium">✅ مكتمل!</p>
              <p className="text-xs text-gray-400 mt-1">
                Samples: {pipelineMutation.data.data_generation?.samples_generated} |
                Win Rate: {pipelineMutation.data.data_generation?.win_rate?.toFixed(1)}%
              </p>
            </div>
          )}
        </div>

        {/* Repeated training */}
        <div className="card">
          <h3 className="text-sm font-medium text-gray-400 mb-3">تدريب متكرر حتى الهدف</h3>
          <p className="text-xs text-gray-500 mb-3">يكرّر التدريب حتى يصل Win Rate للهدف</p>
          <div className="mb-3">
            <label className="text-xs text-gray-500 mb-1 block">الهدف (%)</label>
            <input
              type="number"
              value={targetWinRate}
              onChange={(e) => setTargetWinRate(parseInt(e.target.value) || 70)}
              min={50}
              max={90}
              className="w-full bg-bg-soft border border-bg-border rounded px-3 py-2 text-sm"
              disabled={isTraining}
            />
          </div>
          <button
            onClick={() => repeatedMutation.mutate()}
            disabled={isTraining}
            className="btn-success w-full"
          >
            {repeatedMutation.isPending ? (
              <Loader2 size={16} className="animate-spin inline ml-2" />
            ) : (
              <Target size={16} className="inline ml-2" />
            )}
            {repeatedMutation.isPending ? 'جاري التدريب المتكرر...' : 'ابدأ حتى الهدف'}
          </button>
          {repeatedMutation.data && (
            <div className="mt-3 p-3 bg-bg-soft rounded text-sm">
              {repeatedMutation.data.target_reached ? (
                <p className="text-emerald-400 font-medium">🎯 تم الوصول للهدف!</p>
              ) : (
                <p className="text-amber-400 font-medium">⏳ لم يصل بعد</p>
              )}
              <p className="text-xs text-gray-400 mt-1">
                Final Win Rate: {repeatedMutation.data.final_win_rate?.toFixed(1)}%
                ({repeatedMutation.data.total_iterations} iterations)
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Latest ML Model details */}
      {stats?.latest_model && (
        <div className="card">
          <h3 className="text-sm font-medium text-gray-400 mb-3">آخر نموذج ML مدرّب</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Metric label="Precision" value={stats.latest_model.precision?.toFixed(3) || '—'} />
            <Metric label="F1 Score" value={stats.latest_model.f1?.toFixed(3) || '—'} />
            <Metric label="Brier Score" value={stats.latest_model.brier_score?.toFixed(4) || '—'} />
            <Metric label="Calibration Error" value={stats.latest_model.calibration_error?.toFixed(4) || '—'} />
          </div>
          <p className="text-xs text-gray-500 mt-3">
            النموذج المُسبق التدريب (precision=0.71) متوفر تلقائياً عند بدء النظام.
          </p>
        </div>
      )}

      {/* ML Models History */}
      <div className="card">
        <h3 className="text-sm font-medium text-gray-400 mb-3">سجل النماذج ({mlModels?.length || 0})</h3>
        <div className="overflow-x-auto scrollbar">
          <table className="w-full text-sm">
            <thead className="text-gray-400 text-xs">
              <tr>
                <th className="text-right p-2">الإصدار</th>
                <th className="text-right p-2">Train</th>
                <th className="text-right p-2">Test</th>
                <th className="text-right p-2">Precision</th>
                <th className="text-right p-2">F1</th>
                <th className="text-right p-2">Brier</th>
                <th className="text-right p-2">Cal Err</th>
                <th className="text-right p-2">الحالة</th>
              </tr>
            </thead>
            <tbody>
              {(mlModels || []).map((m: any) => (
                <tr key={m.id} className="border-t border-bg-border">
                  <td className="p-2 font-medium">v{m.version}</td>
                  <td className="p-2 text-gray-400">{m.train_samples}</td>
                  <td className="p-2 text-gray-400">{m.test_samples}</td>
                  <td className="p-2 text-emerald-400">{m.precision?.toFixed(3) || '—'}</td>
                  <td className="p-2">{m.f1?.toFixed(3) || '—'}</td>
                  <td className="p-2 text-amber-400">{m.brier_score?.toFixed(4) || '—'}</td>
                  <td className="p-2">{m.calibration_error?.toFixed(4) || '—'}</td>
                  <td className="p-2">
                    <span className={`badge ${
                      m.status === 'adopted' ? 'badge-success' :
                      m.status === 'trained' ? 'badge-warning' : 'badge-neutral'
                    }`}>{m.status}</span>
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

function StatCard({ label, value, icon }: { label: string; value: any; icon: React.ReactNode }) {
  return (
    <div className="card flex items-center gap-3 py-3">
      <div className="w-10 h-10 rounded-lg bg-bg-soft flex items-center justify-center flex-shrink-0">
        {icon}
      </div>
      <div>
        <p className="text-xs text-gray-500">{label}</p>
        <p className="text-lg font-bold">{value}</p>
      </div>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: any }) {
  return (
    <div className="bg-bg-soft rounded-lg p-3">
      <p className="text-xs text-gray-500">{label}</p>
      <p className="font-bold text-lg">{value}</p>
    </div>
  )
}
