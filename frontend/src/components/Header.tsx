import { useState } from 'react';
import { useAuth } from '../auth/AuthContext';
import { ChangePasswordModal } from './ChangePasswordModal';
import './Header.css';

export function Header() {
  const { logout } = useAuth();
  const [showChangePassword, setShowChangePassword] = useState(false);

  return (
    <header className="app-header">
      <div className="app-header__row">
        <div>
          <h1 className="app-header__wordmark">Lares</h1>
          <p className="app-header__tagline">Home lab, watched over</p>
        </div>
        <div className="app-header__actions">
          <button
            type="button"
            className="btn btn--ghost btn--small"
            onClick={() => setShowChangePassword(true)}
          >
            Change password
          </button>
          <button type="button" className="btn btn--ghost btn--small" onClick={() => logout()}>
            Log out
          </button>
        </div>
      </div>
      {showChangePassword ? (
        <ChangePasswordModal onClose={() => setShowChangePassword(false)} />
      ) : null}
    </header>
  );
}
