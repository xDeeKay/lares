import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';
import {
  changePassword as apiChangePassword,
  getAuthStatus,
  login as apiLogin,
  logout as apiLogout,
  setAuthToken,
  setupPassword as apiSetupPassword,
  setUnauthorizedHandler,
} from '../api';

const TOKEN_STORAGE_KEY = 'lares_auth_token';

type AuthStatusValue = 'loading' | 'setup_required' | 'unauthenticated' | 'authenticated';

interface AuthContextValue {
  status: AuthStatusValue;
  setup: (password: string) => Promise<void>;
  login: (password: string) => Promise<void>;
  logout: () => Promise<void>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function persistToken(token: string) {
  setAuthToken(token);
  localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

function clearToken() {
  setAuthToken(null);
  localStorage.removeItem(TOKEN_STORAGE_KEY);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatusValue>('loading');

  useEffect(() => {
    setUnauthorizedHandler(() => {
      clearToken();
      setStatus('unauthenticated');
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    const stored = localStorage.getItem(TOKEN_STORAGE_KEY);
    if (stored) {
      // Optimistic: no ping call to validate the stored token on boot. A
      // stale/expired token just means the dashboard shell renders briefly
      // before the first API call 401s and the onUnauthorized handler above
      // bounces back to login, which is covered either way.
      setAuthToken(stored);
      setStatus('authenticated');
      return;
    }
    getAuthStatus()
      .then((res) => setStatus(res.setup_required ? 'setup_required' : 'unauthenticated'))
      .catch(() => setStatus('unauthenticated'));
  }, []);

  const setup = useCallback(async (password: string) => {
    const res = await apiSetupPassword(password);
    persistToken(res.token);
    setStatus('authenticated');
  }, []);

  const login = useCallback(async (password: string) => {
    const res = await apiLogin(password);
    persistToken(res.token);
    setStatus('authenticated');
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiLogout();
    } finally {
      clearToken();
      setStatus('unauthenticated');
    }
  }, []);

  const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
    const res = await apiChangePassword(currentPassword, newPassword);
    // Must happen before anything else: any request already queued (e.g. a
    // poll) picks up the new token rather than racing a deferred update.
    persistToken(res.token);
  }, []);

  return (
    <AuthContext.Provider value={{ status, setup, login, logout, changePassword }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return ctx;
}
