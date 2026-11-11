import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
  /** 出错时显示的标题，便于用户知道是哪一块坏了。 */
  label?: string
  /** 出错的回调，用于上报或状态收敛。 */
  onError?: (error: Error, info: ErrorInfo) => void
}

interface State {
  error: Error | null
}

/**
 * 渲染错误边界：任何子树抛异常时降级为一块可恢复的提示，而不是让整页白屏。
 *
 * React 的错误边界只能捕获渲染期异常，不能捕获事件处理器 / 异步里的异常，
 * 所以它只是最后一道网——真正的防御仍然写在数据入口（normalize / try-catch）。
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 保留到控制台，便于现场排查；不影响用户看到降级界面。
    console.error('[ErrorBoundary]', this.props.label || '', error, info?.componentStack)
    this.props.onError?.(error, info)
  }

  private reset = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div className="crash-panel" role="alert">
        <div className="crash-title">
          {this.props.label ? `${this.props.label}出现问题` : '这一块渲染出错了'}
        </div>
        <div className="crash-msg">{error.message || String(error)}</div>
        <div className="crash-actions">
          <button type="button" className="ghost-btn" onClick={this.reset}>
            重试
          </button>
          <button
            type="button"
            className="ghost-btn"
            onClick={() => { window.location.reload() }}
          >
            刷新页面
          </button>
        </div>
      </div>
    )
  }
}