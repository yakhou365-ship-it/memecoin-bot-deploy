import { Routes, Route, NavLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, endpoints } from './lib/api'
import {
  LayoutDashboard, Coins, TrendingUp, Brain, BarChart3, ShieldAlert,
  Bot, CreditCard, Wallet, FlaskConical,
} from 'lucide-react'
import HomePage from './pages/HomePage'
import TokensPage from './pages/TokensPage'
import TradesPage from './pages/TradesPage'
import LearningPage from './pages/LearningPage'
import AnalyticsPage from './pages/AnalyticsPage'
import AgentsPage from './pages/AgentsPage'
import LiveTradingPage from './pages/LiveTradingPage'
import BudgetPage from './pages/BudgetPage'
import ResearchPage from './pages/ResearchPage'

// V31.2: Removed ChatPage and AlertsPage per user request
const navItems = [
  { to: '/', label: 'الرئيسية', icon: LayoutDashboard, end: true },
  { to: '/tokens', label: 'العملات', icon: Coins },
  { to: '/trades', label: 'الصفقات', icon: TrendingUp },
  { to: '/agents', label: 'وكلاء AI', icon: Bot },
  { to: '/research', label: 'الأبحاث', icon: FlaskConical },
  { to: '/analytics', label: 'التحليلات', icon: BarChart3 },
  { to: '/budget', label: 'الميزانية', icon: Wallet },
  { to: '/live-trading', label: 'تداول حقيقي', icon: CreditCard },
]

function NavItem({ to, label, icon: Icon, end }: { to: string; label: string; icon: any; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        `flex items-center gap-3 px-4 py-3 rounded-lg transition-colors ${
          isActive
            ? 'bg-accent-blue/20 text-accent-blue font-medium'
            : 'text-gray-400 hover:text-gray-200 hover:bg-bg-soft'
        }`
      }
    >
      <Icon size={18} />
      <span>{label}</span>
    </NavLink>
  )
}

export default function App() {
  const { data: sys } = useQuery({
    queryKey: ['app-system-status'],
    queryFn: () => api.get(endpoints.systemStatus).then(r => r.data),
    refetchInterval: 1_500,
  })
  const isLive = sys?.is_live_trading ?? false
  const agentsEnabled = sys?.ai_agents_enabled || {}
  const activeAgents = Object.values(agentsEnabled).filter(Boolean).length || 5

  return (
    <div className="min-h-screen flex bg-bg">
      {/* Sidebar */}
      <aside className="w-60 bg-bg-card border-l border-bg-border p-4 flex flex-col gap-2 overflow-y-auto scrollbar">
        <div className="flex items-center gap-2 mb-6 px-2">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent-purple to-accent-blue flex items-center justify-center">
            <ShieldAlert size={22} className="text-white" />
          </div>
          <div>
            <h1 className="font-bold text-sm">Memecoin AI</h1>
            <p className="text-xs text-gray-500">v0.3.0</p>
          </div>
        </div>
        <nav className="flex-1 flex flex-col gap-1">
          {navItems.map((item) => (
            <NavItem key={item.to} {...item} />
          ))}
        </nav>
        <div className="text-xs text-gray-500 px-2 pt-4 border-t border-bg-border">
          <p className="text-red-400 mt-1">● LIVE Mode</p>
          <p className="text-xs text-gray-600 mt-1">{activeAgents} AI Agents</p>
          <p className="text-xs text-gray-600">{sys?.pumpfun_configured ? 'PumpPortal Ready' : 'PumpPortal Not Set'}</p>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto scrollbar">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/tokens" element={<TokensPage />} />
          <Route path="/tokens/:id" element={<TokensPage />} />
          <Route path="/trades" element={<TradesPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/research" element={<ResearchPage />} />
          <Route path="/learning" element={<LearningPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/budget" element={<BudgetPage />} />
          <Route path="/live-trading" element={<LiveTradingPage />} />
          {/* V31.2: Removed /chat and /alerts routes */}
          <Route path="/chat" element={<HomePage />} />
          <Route path="/alerts" element={<HomePage />} />
        </Routes>
      </main>
    </div>
  )
}
