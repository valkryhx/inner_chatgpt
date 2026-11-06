import { useState, type FormEvent } from 'react'
import { api, setToken } from '../api'
import type { User } from '../types'

interface Props {
  onAuthed: (user: User) => void
}

export default function LoginPage({ onAuthed }: Props) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  // 登录：邮箱或用户名都可以；注册：邮箱 + 用户名（可选，缺省用邮箱前缀）
  const [identifier, setIdentifier] = useState('')
  const [email, setEmail] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const canSubmit = mode === 'login' ? !!identifier.trim() && !!password : !!email.trim() && !!password

  const submit = async () => {
    if (!canSubmit || busy) return
    setBusy(true)
    setError(null)
    try {
      const r =
        mode === 'login'
          ? await api.login(identifier.trim(), password)
          : await api.register(
              email.trim(),
              password,
              name.trim() || undefined,
              username.trim() || undefined,
            )
      setToken(r.access_token)
      onAuthed(r.user)
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  // A real <form> gives Enter-to-submit and password-manager autofill; the
  // hand-rolled keydown handler only worked when focus was in the password box.
  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    submit()
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-logo">H</div>
          <h1>Hlaudz</h1>
          <p>HX 的智能 Agent 工作台</p>
        </div>
        <div className="auth-tabs">
          <button type="button" className={mode === 'login' ? 'active' : ''} onClick={() => setMode('login')}>
            登录
          </button>
          <button type="button" className={mode === 'register' ? 'active' : ''} onClick={() => setMode('register')}>
            注册
          </button>
        </div>
        <form className="auth-form" onSubmit={onSubmit}>
          {mode === 'register' && (
            <label>
              <span>昵称</span>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="可选" />
            </label>
          )}
          {mode === 'register' && (
            <label>
              <span>用户名</span>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="登录用，可选；默认取邮箱前缀"
                autoComplete="username"
              />
            </label>
          )}
          {mode === 'login' ? (
            <label>
              <span>邮箱或用户名</span>
              <input
                value={identifier}
                onChange={(e) => setIdentifier(e.target.value)}
                placeholder="you@example.com 或 用户名"
                autoComplete="username"
              />
            </label>
          ) : (
            <label>
              <span>邮箱</span>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                autoComplete="email"
              />
            </label>
          )}
          <label>
            <span>密码</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少 4 位"
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            />
          </label>
          {error && <div className="auth-error">{error}</div>}
          <button type="submit" className="auth-submit" disabled={busy || !canSubmit}>
            {busy ? '请稍候…' : mode === 'login' ? '登录' : '注册并登录'}
          </button>
        </form>
      </div>
    </div>
  )
}
