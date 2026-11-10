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

  // A real <form> gives Enter-to-submit and password-manager autofill.
  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    submit()
  }

  return (
    <div className={'auth-page' + (mode === 'register' ? ' register' : '')}>
      <div className="auth-glow" aria-hidden="true" />
      <div className="auth-card">
        <div className="auth-brand">
          <img className="auth-mark" src="/logo.png" alt="" width={60} height={60} />
          <h1>Hlaudz</h1>
          <p>{mode === 'login' ? '登录你的智能 Agent 工作台' : '创建一个账号，开始你自己的 Agent 工作台'}</p>
        </div>

        <div className="auth-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'login'}
            className={mode === 'login' ? 'active' : ''}
            onClick={() => { setMode('login'); setError(null) }}
          >
            登录
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'register'}
            className={mode === 'register' ? 'active' : ''}
            onClick={() => { setMode('register'); setError(null) }}
          >
            注册
          </button>
        </div>

        <form className="auth-form" onSubmit={onSubmit}>
          {mode === 'register' && (
            <>
              <label className="auth-field">
                <span>邮箱</span>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  autoComplete="email"
                />
              </label>
              <div className="auth-row">
                <label className="auth-field">
                  <span>昵称</span>
                  <input value={name} onChange={(e) => setName(e.target.value)} placeholder="可选" />
                </label>
                <label className="auth-field">
                  <span>用户名</span>
                  <input
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder="默认取邮箱前缀"
                    autoComplete="username"
                  />
                </label>
              </div>
            </>
          )}

          {mode === 'login' && (
            <label className="auth-field">
              <span>邮箱或用户名</span>
              <input
                value={identifier}
                onChange={(e) => setIdentifier(e.target.value)}
                placeholder="you@example.com 或 用户名"
                autoComplete="username"
              />
            </label>
          )}

          <label className="auth-field">
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
            {busy ? '请稍候…' : mode === 'login' ? '登录' : '创建账号'}
          </button>
        </form>

        <p className="auth-foot">
          {mode === 'login'
            ? '继续即表示你同意本实例的使用约定。'
            : '账号只建在你自己部署的这个实例里，数据不会外发。'}
        </p>
      </div>
    </div>
  )
}
