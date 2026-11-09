import { useState, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Send, Trash2, Bot, User, Loader2 } from 'lucide-react'
import { api, endpoints } from '../lib/api'

interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  timestamp: string
  context_used?: any
}

export default function ChatPage() {
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [isThinking, setIsThinking] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  const qc = useQueryClient()

  // Load history on mount
  const { data: history } = useQuery({
    queryKey: ['chat-history'],
    queryFn: () => api.get(endpoints.chatHistory).then(r => r.data.history as ChatMsg[]),
  })

  useEffect(() => {
    if (history) setMessages(history)
  }, [history])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, isThinking])

  const sendMutation = useMutation({
    mutationFn: (msg: string) => api.post(endpoints.chatSend, { message: msg }).then(r => r.data),
    onMutate: () => setIsThinking(true),
    onSuccess: (data) => {
      // Refresh history from server
      qc.invalidateQueries({ queryKey: ['chat-history'] })
      setIsThinking(false)
    },
    onError: () => {
      toast.error('فشل الإرسال')
      setIsThinking(false)
    },
  })

  const clearMutation = useMutation({
    mutationFn: () => api.post(endpoints.chatClear),
    onSuccess: () => {
      setMessages([])
      qc.invalidateQueries({ queryKey: ['chat-history'] })
      toast.success('تم مسح المحادثة')
    },
  })

  const handleSend = async () => {
    const msg = input.trim()
    if (!msg || isThinking) return
    // Optimistic UI
    setMessages(prev => [...prev, { role: 'user', content: msg, timestamp: new Date().toISOString() }])
    setInput('')
    await sendMutation.mutateAsync(msg)
  }

  const suggestions = [
    'ما هو رصيد حسابي الافتراضي؟',
    'ما هي أفضل 5 عملات مكتشفة؟',
    'ما هي آخر الأخطاء التي تعلّمت منها؟',
    'كم عدد الصفقات المفتوحة والمغلقة؟',
    'ما هي حالة إدارة المخاطر؟',
  ]

  return (
    <div className="p-6 h-screen flex flex-col">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Bot size={24} className="text-accent-purple" />
            محادثة مع الذكاء الاصطناعي
          </h1>
          <p className="text-sm text-gray-500">اسأل أي شيء عن النظام، العملات، الصفقات، أو التحليلات</p>
        </div>
        {messages.length > 0 && (
          <button
            onClick={() => clearMutation.mutate()}
            className="btn bg-bg-soft text-gray-400 hover:text-red-400"
          >
            <Trash2 size={16} className="inline ml-2" />
            مسح المحادثة
          </button>
        )}
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 card p-4 overflow-y-auto scrollbar space-y-4">
        {messages.length === 0 && !isThinking && (
          <div className="h-full flex flex-col items-center justify-center text-center">
            <Bot size={48} className="text-accent-purple mb-3 opacity-50" />
            <p className="text-gray-400 mb-1">ابدأ محادثة مع المساعد الذكي</p>
            <p className="text-xs text-gray-600 mb-4">يمكنك السؤال عن أي شيء يتعلق بالنظام</p>
            <div className="flex flex-wrap gap-2 justify-center max-w-md">
              {suggestions.map((s) => (
                <button
                  key={s}
                  onClick={() => { setInput(s); }}
                  className="px-3 py-1.5 text-xs bg-bg-soft hover:bg-bg-border rounded-full text-gray-400"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`flex gap-3 ${m.role === 'user' ? 'flex-row-reverse' : ''}`}>
            <div className={`w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 ${
              m.role === 'user' ? 'bg-accent-blue' : 'bg-accent-purple'
            }`}>
              {m.role === 'user' ? <User size={16} className="text-white" /> : <Bot size={16} className="text-white" />}
            </div>
            <div className={`flex-1 ${m.role === 'user' ? 'text-left' : ''}`}>
              <div className={`inline-block px-4 py-2 rounded-lg max-w-[80%] ${
                m.role === 'user'
                  ? 'bg-accent-blue text-white'
                  : 'bg-bg-soft text-gray-200'
              }`}>
                <p className="whitespace-pre-wrap text-sm">{m.content}</p>
              </div>
              {m.context_used && Object.keys(m.context_used).length > 0 && (
                <details className="mt-1 text-xs text-gray-600">
                  <summary className="cursor-pointer hover:text-gray-400">البيانات المستخدمة للإجابة</summary>
                  <pre className="mt-1 p-2 bg-bg-soft rounded text-xs overflow-x-auto">
                    {JSON.stringify(m.context_used, null, 2).slice(0, 500)}
                  </pre>
                </details>
              )}
              <p className="text-xs text-gray-600 mt-1">
                {new Date(m.timestamp).toLocaleTimeString('en-GB')}
              </p>
            </div>
          </div>
        ))}

        {isThinking && (
          <div className="flex gap-3">
            <div className="w-8 h-8 rounded-full bg-accent-purple flex items-center justify-center flex-shrink-0">
              <Bot size={16} className="text-white" />
            </div>
            <div className="bg-bg-soft rounded-lg px-4 py-3 flex items-center gap-2">
              <Loader2 size={14} className="animate-spin text-accent-purple" />
              <span className="text-sm text-gray-400">يفكّر...</span>
            </div>
          </div>
        )}
      </div>

      {/* Input */}
      <div className="mt-4 flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSend()}
          placeholder="اكتب سؤالك..."
          disabled={isThinking}
          className="flex-1 bg-bg-soft border border-bg-border rounded-lg px-4 py-3 text-sm focus:outline-none focus:border-accent-blue"
        />
        <button
          onClick={handleSend}
          disabled={isThinking || !input.trim()}
          className="btn-primary"
        >
          <Send size={16} className="inline ml-2" />
          إرسال
        </button>
      </div>
    </div>
  )
}
