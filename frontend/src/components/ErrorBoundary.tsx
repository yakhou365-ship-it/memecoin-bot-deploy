import { Component } from 'react'

interface Props {
  children: React.ReactNode
}
interface State {
  hasError: boolean
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  public state: State = { hasError: false, error: null }

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  public componentDidCatch(error: Error, errorInfo: React.ErrorInfo): void {
    console.error('React ErrorBoundary caught:', error, errorInfo)
  }

  public render(): React.ReactNode {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen flex bg-gray-900 p-4">
          <div className="bg-red-900/20 border border-red-500/50 rounded-lg p-6 w-full">
            <h2 className="text-red-400 text-lg font-bold mb-2">خطأ في العرض</h2>
            <p className="text-red-200 text-sm mb-2">{this.state.error?.message || 'خطأ غير معروف'}</p>
            <button
              onClick={() => {
                this.setState({ hasError: false, error: null })
              }}
              className="bg-red-500 hover:bg-red-600 text-white px-4 py-2 rounded text-sm"
            >
              إعادة المحاولة
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
