import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import ErrorBoundary from './components/ErrorBoundary'
import './styles/index.css'

// 最外层兜底：任何未预料的渲染异常都降级成可恢复提示，绝不整页空白。
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary label="界面">
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)
