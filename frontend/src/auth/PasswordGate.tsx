import { useState, type FormEvent } from 'react';
import { ApiError } from '../api';
import { useAuth } from './AuthContext';
import './PasswordGate.css';

interface PasswordGateProps {
  mode: 'setup' | 'login';
}

export function PasswordGate({ mode }: PasswordGateProps) {
  const { setup, login } = useAuth();
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    if (mode === 'setup' && password !== confirmPassword) {
      setError('Passwords do not match.');
      return;
    }

    setBusy(true);
    try {
      if (mode === 'setup') {
        await setup(password);
      } else {
        await login(password);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong, try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="password-gate">
      <form className="password-gate__card card" onSubmit={handleSubmit}>
        <h1 className="password-gate__wordmark">Lares</h1>
        <p className="password-gate__subtitle">
          {mode === 'setup' ? 'Set a password to get started.' : 'Enter your password to continue.'}
        </p>

        <label className="password-gate__label" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          type="password"
          className="password-gate__input"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoFocus
          required
          disabled={busy}
        />

        {mode === 'setup' ? (
          <>
            <label className="password-gate__label" htmlFor="confirm-password">
              Confirm password
            </label>
            <input
              id="confirm-password"
              type="password"
              className="password-gate__input"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              disabled={busy}
            />
          </>
        ) : null}

        {error ? <p className="password-gate__error">{error}</p> : null}

        <button type="submit" className="btn btn--primary password-gate__submit" disabled={busy}>
          {busy ? 'Working…' : mode === 'setup' ? 'Set password' : 'Log in'}
        </button>
      </form>
    </div>
  );
}
