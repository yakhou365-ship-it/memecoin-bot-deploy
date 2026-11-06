import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Wallet, Save, Play, Square, TrendingUp, AlertTriangle, DollarSign, RefreshCw } from 'lucide-react'
import { api, endpoints } from '../lib/api'

export default function BudgetPage() {
  const qc = useQueryClient()
  const [settings, setSettings] = useState<any>(null)

  const { data: budget, isLoading: budgetLoading, isError: budgetError, error: budgetErr } = useQuery({
    queryKey: ['budget-settings'],
    queryFn: () => api.get(endpoints.budgetSettings).then(r => r.data),
    refetchInterval: 5000,
    retry: false,  // V508: don't retry on 401 — show error immediately
  })

  const { data: dailyRecord } = useQuery({
    queryKey: ['budget-daily'],
    queryFn: () => api.get(endpoints.budgetDailyRecord).then(r => r.data),
    refetchInterval: 5000,
    retry: false,
  })

  // V508: fix query params — /api/budget/can-trade takes required_capital + risk_amount
  const { data: canTrade } = useQuery({
    queryKey: ['budget-can-trade'],
    queryFn: () => api.get(endpoints.budgetCanTrade, { params: { required_capital: 5, risk_amount: 1 } }).then(r => r.data),
    refetchInterval: 3000,
    retry: false,
  })

  // Live SOL price (for USD → SOL conversion)
  const { data: wallet } = useQuery({
    queryKey: ['budget-sol-price'],
    queryFn: () => api.get(endpoints.pumpfunWalletBalance).then(r => r.data),
    refetchInterval: 60000,
    retry: false,
  })
  const solPrice = Number(wallet?.sol_price_usd || 0) || 200

  // V508: use useEffect to initialize settings (avoids setState-during-render warning
  // and ensures settings update when budget data refreshes).
  useEffect(() => {
    if (budget && !settings) {
      setSettings({
        daily_trading_budget: budget.daily_trading_budget || 1000,
        max_capital_per_trade: budget.max_capital_per_trade || 100,
        max_daily_trades: budget.max_daily_trades || 20,
        max_simultaneous_positions: budget.max_simultaneous_positions || 5,
        max_daily_loss: budget.max_daily_loss || 50,
        max_daily_drawdown_pct: budget.max_daily_drawdown_pct || 5,
        daily_risk_exposure_limit: budget.daily_risk_exposure_limit || 200,
      })
    }
  }, [budget, settings])

  const saveMutation = useMutation({
    mutationFn: (data: any) => api.post(endpoints.budgetSettings, data).then(r => r.data),
    onSuccess: () => {
      toast.success('✅ تم حفظ إعدادات الميزانية')
      qc.invalidateQueries({ queryKey: ['budget-settings'] })
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'فشل حفظ الإعدادات'),
  })

  const stopMutation = useMutation({
    mutationFn: () => api.post(endpoints.budgetStop).then(r => r.data),
    onSuccess: () => {
      toast.success('⏸️ تم إيقاف التداول')
      qc.invalidateQueries({ queryKey: ['budget-can-trade'] })
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'فشل الإيقاف'),
  })

  const startMutation = useMutation({
    mutationFn: () => api.post(endpoints.budgetStart).then(r => r.data),
    onSuccess: () => {
      toast.success('▶️ تم استئناف التداول')
      qc.invalidateQueries({ queryKey: ['budget-can-trade'] })
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'فشل الاستئناف'),
  })

  const handleSave = () => {
    if (settings) saveMutation.mutate(settings)
  }

  const handleInputChange = (field: string, value: number) => {
    setSettings({ ...settings, [field]: value })
  }

  // V508: explicit error state — instead of infinite "جاري التحميل..." when API fails.
  // Show the actual error (401 = auth failure, 500 = server error) with a retry button.
  if (budgetError) {
    const status = (budgetErr as any)?.response?.status
    const detail = (budgetErr as any)?.response?.data?.detail || (budgetErr as any)?.message || 'خطأ غير معروف'
    return (
      <div className="p-6 space-y-4">
        <h1 className="text-2xl font-bold">إدارة الميزانية والمخاطر</h1>
        <div className="card border-red-500/40 bg-red-500/5 p-6">
          <div className="flex items-start gap-3">
            <AlertTriangle className="text-red-400 mt-0.5" size={20} />
            <div className="flex-1">
              <p className="font-medium text-red-300">
                {status === 401 ? '🔒 مفتاح API خاطئ أو غير مُدخل' : `⚠️ فشل تحميل البيانات (HTTP ${status})`}
              </p>
              <p className="text-xs text-gray-400 mt-1 font-mono break-all">{detail}</p>
              {status === 401 && (
                <p className="text-xs text-amber-300 mt-2">
                  افتح الرابط مع <span className="font-mono">#resetkey</span> في النهاية لإدخال المفتاح الصحيح من Railway Variables (API_AUTH_KEY).
                </p>
              )}
              <button
                onClick={() => qc.invalidateQueries({ queryKey: ['budget-settings'] })}
                className="btn-primary mt-3 px-3 py-1 text-sm"
              >
                <RefreshCw size={14} className="inline ml-1" />
                إعادة المحاولة
              </button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  if (budgetLoading || !settings) return <div className="p-6">جاري التحميل...</div>

  // V508: API returns {"allowed": true, "reason": ""} — NOT can_trade.
  // Old code checked canTrade?.can_trade which was always undefined.
  const isTrading = canTrade?.allowed ?? budget?.is_active ?? false

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">إدارة الميزانية والمخاطر</h1>
        <div className="flex items-center gap-2">
          {isTrading ? (
            <button onClick={() => stopMutation.mutate()} disabled={stopMutation.isPending} className="btn-danger">
              <Square size={16} className="inline ml-2" />
              إيقاف التداول
            </button>
          ) : (
            <button onClick={() => startMutation.mutate()} disabled={startMutation.isPending} className="btn-success">
              <Play size={16} className="inline ml-2" />
              استئناف التداول
            </button>
          )}
        </div>
      </div>

      {/* Trading Status */}
      <div className={`card p-4 ${isTrading ? 'border-emerald-500/30' : 'border-red-500/30'}`}>
        <div className="flex items-center gap-3">
          {isTrading ? (
            <span className="badge badge-success">🟢 التداول مفعّل</span>
          ) : (
            <span className="badge badge-danger">🔴 التداول متوقف</span>
          )}
          {canTrade?.reason && (
            <span className="text-sm text-amber-400 flex items-center gap-1">
              <AlertTriangle size={14} />
              {canTrade.reason}
            </span>
          )}
        </div>
      </div>

      {/* Daily Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard icon={<DollarSign size={20} />} label="ميزانية اليوم" value={`$${dailyRecord?.total_spent?.toFixed(2) || '0'} / $${budget?.daily_trading_budget || 1000}`} />
        <StatCard icon={<TrendingUp size={20} />} label="صفقات اليوم" value={`${dailyRecord?.trades_today || 0} / ${budget?.max_daily_trades || 20}`} />
        <StatCard icon={<AlertTriangle size={20} />} label="خسارة اليوم" value={`$${dailyRecord?.total_loss?.toFixed(2) || '0'} / $${budget?.max_daily_loss || 50}`} color="text-amber-400" />
        <StatCard icon={<Wallet size={20} />} label="صفقات مفتوحة" value={`${dailyRecord?.open_positions || 0} / ${budget?.max_simultaneous_positions || 5}`} />
      </div>

      {/* Settings Form */}
      <div className="card p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-medium">إعدادات الميزانية</h3>
          <button onClick={handleSave} disabled={saveMutation.isPending} className="btn-primary">
            <Save size={16} className="inline ml-2" />
            {saveMutation.isPending ? '⏳ حفظ...' : '💾 حفظ التغييرات'}
          </button>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <InputField
            label="الميزانية اليومية ($)"
            value={settings.daily_trading_budget}
            onChange={(v) => handleInputChange('daily_trading_budget', v)}
            step={10}
            min={10}
          />
          <InputField
            label="حد الصفقة الواحدة ($)"
            value={settings.max_capital_per_trade}
            onChange={(v) => handleInputChange('max_capital_per_trade', v)}
            step={5}
            min={1}
          />
          <InputField
            label="عدد الصفقات اليومي"
            value={settings.max_daily_trades}
            onChange={(v) => handleInputChange('max_daily_trades', v)}
            step={1}
            min={1}
          />
          <InputField
            label="الصفقات المفتوحة (حد أقصى)"
            value={settings.max_simultaneous_positions}
            onChange={(v) => handleInputChange('max_simultaneous_positions', v)}
            step={1}
            min={1}
          />
          <InputField
            label="الخسارة اليومية القصوى ($)"
            value={settings.max_daily_loss}
            onChange={(v) => handleInputChange('max_daily_loss', v)}
            step={5}
            min={1}
          />
          <InputField
            label="الانخفاض الأقصى (%)"
            value={settings.max_daily_drawdown_pct}
            onChange={(v) => handleInputChange('max_daily_drawdown_pct', v)}
            step={0.5}
            min={1}
          />
          <InputField
            label="حد المخاطرة اليومية ($)"
            value={settings.daily_risk_exposure_limit}
            onChange={(v) => handleInputChange('daily_risk_exposure_limit', v)}
            step={10}
            min={10}
          />
        </div>
      </div>

      {/* Risk Summary */}
      <div className="card p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">📊 ملخص المخاطر</h3>
        <div className="grid grid-cols-2 gap-4 text-sm">
          <Row label="نسبة المخاطرة لكل صفقة" value={`${((settings.max_capital_per_trade / settings.daily_trading_budget) * 100).toFixed(1)}%`} />
          <Row label="نسبة الخسارة القصوى" value={`${((settings.max_daily_loss / settings.daily_trading_budget) * 100).toFixed(1)}%`} />
          <Row label="الحد الأقصى لكل صفقة (SOL)" value={`${(settings.max_capital_per_trade / solPrice).toFixed(4)} SOL`} />
          <Row label="سعر SOL الحالي" value={`$${solPrice.toFixed(2)}`} />
          <Row label="الإجمالي المتاح للتداول" value={`$${settings.daily_trading_budget}`} />
        </div>
      </div>
    </div>
  )
}

function StatCard({ icon, label, value, color }: { icon: React.ReactNode; label: string; value: string; color?: string }) {
  return (
    <div className="card p-4">
      <div className="flex items-center gap-2 mb-2">
        <div className="w-8 h-8 rounded-lg bg-bg-soft flex items-center justify-center text-accent-blue">
          {icon}
        </div>
        <p className="text-xs text-gray-500">{label}</p>
      </div>
      <p className={`font-bold ${color || ''}`}>{value}</p>
    </div>
  )
}

function InputField({ label, value, onChange, step, min }: { label: string; value: number; onChange: (v: number) => void; step: number; min: number }) {
  return (
    <div>
      <label className="text-sm text-gray-400 mb-1 block">{label}</label>
      <input
        type="number"
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value) || min)}
        step={step}
        min={min}
        className="w-full bg-bg-soft border border-bg-border rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:border-accent-blue"
      />
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between items-center p-2 bg-bg-soft rounded-lg">
      <span className="text-gray-400 text-xs">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  )
}
