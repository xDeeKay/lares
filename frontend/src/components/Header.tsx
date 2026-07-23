import { useAuth } from '../auth/AuthContext';
import './Header.css';

export function Header() {
  const { logout } = useAuth();

  return (
    <header className="app-header">
      <div className="app-header__row">
        <div>
          <h1 className="app-header__wordmark">Lares</h1>
          <p className="app-header__tagline">Home lab, watched over</p>
        </div>
        <button type="button" className="btn btn--ghost btn--small" onClick={() => logout()}>
          Log out
        </button>
      </div>
    </header>
  );
}
