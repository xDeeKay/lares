import { useState } from 'react';
import { useAuth } from '../auth/AuthContext';
import { ChangePasswordModal } from './ChangePasswordModal';
import cornucopia from '../assets/cornucopia.svg';
import './Header.css';

export function Header() {
  const { logout } = useAuth();
  const [showChangePassword, setShowChangePassword] = useState(false);

  return (
    <header className="app-header">
      <div className="app-header__row">
        <div className="app-header__brand">
          <img className="app-header__logo" src={cornucopia} alt="" />
          <h1 className="app-header__wordmark">
            Lares <span className="app-header__tagline">Your home network, watched over.</span>
          </h1>
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
