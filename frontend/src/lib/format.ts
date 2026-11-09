// Utility formatting functions

export const fmtUsd = (n: number | null | undefined, decimals?: number): string => {
  if (n == null || isNaN(n)) return '—'
  const abs = Math.abs(n)
  const digits =
    decimals != null
      ? decimals
      : abs >= 1
        ? 2
        : abs >= 0.01
          ? 4
          : abs > 0
            ? 6
            : 2
  return n.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export const fmtPct = (n: number | null | undefined, decimals = 2): string => {
  if (n == null || isNaN(n)) return '—'
  return `${n >= 0 ? '+' : ''}${n.toFixed(decimals)}%`
}

export const fmtNum = (n: number | null | undefined, decimals = 4): string => {
  if (n == null || isNaN(n)) return '—'
  return n.toFixed(decimals)
}

export const fmtPrice = (n: number | null | undefined): string => {
  if (n == null || isNaN(n)) return '—'
  if (n >= 1) return n.toFixed(4)
  if (n >= 0.01) return n.toFixed(6)
  if (n >= 0.0001) return n.toFixed(8)
  return n.toExponential(4)
}

export const fmtCompact = (n: number | null | undefined): string => {
  if (n == null || isNaN(n)) return '—'
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`
  return `$${n.toFixed(2)}`
}

export const fmtTime = (iso: string | null | undefined): string => {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('en-GB', {
      day: '2-digit', month: '2-digit', year: '2-digit',
      hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return '—'
  }
}

export const scoreColor = (score: number | null | undefined): string => {
  if (score == null) return 'text-gray-400'
  if (score >= 70) return 'text-emerald-400'
  if (score >= 50) return 'text-amber-400'
  if (score >= 30) return 'text-orange-400'
  return 'text-red-400'
}

export const riskColor = (risk: number | null | undefined): string => {
  if (risk == null) return 'text-gray-400'
  if (risk < 30) return 'text-emerald-400'
  if (risk < 50) return 'text-amber-400'
  if (risk < 70) return 'text-orange-400'
  return 'text-red-400'
}

export const scoreBarColor = (score: number | null | undefined): string => {
  if (score == null) return 'bg-gray-500'
  if (score >= 70) return 'bg-emerald-500'
  if (score >= 50) return 'bg-amber-500'
  if (score >= 30) return 'bg-orange-500'
  return 'bg-red-500'
}
