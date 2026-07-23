import { useState, type FormEvent } from 'react';
import { ApiError } from '../api';
import { useAuth } from '../auth/AuthContext';
import { Modal } from './Modal';
import './ChangePasswordModal.css';

interface ChangePasswordModalProps {
  onClose: () => void;
}

export function ChangePasswordModal({ onClose }: ChangePasswordModalProps) {
  const { changePassword } = useAuth();
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmNewPassword, setConfirmNewPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    if (newPassword !== confirmNewPassword) {
      setError('New passwords do not match.');
      return;
    }

    setBusy(true);
    try {
      await changePassword(currentPassword, newPassword);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong, try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal onClose={onClose}>
      <h3>Change password</h3>
      <form className="change-password-modal__form" onSubmit={handleSubmit}>
        <label className="change-password-modal__label" htmlFor="current-password">
          Current password
        </label>
        <input
          id="current-password"
          type="password"
          className="change-password-modal__input"
          value={currentPassword}
          onChange={(e) => setCurrentPassword(e.target.value)}
          autoFocus
          required
          disabled={busy}
        />

        <label className="change-password-modal__label" htmlFor="new-password">
          New password
        </label>
        <input
          id="new-password"
          type="password"
          className="change-password-modal__input"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          required
          disabled={busy}
        />

        <label className="change-password-modal__label" htmlFor="confirm-new-password">
          Confirm new password
        </label>
        <input
          id="confirm-new-password"
          type="password"
          className="change-password-modal__input"
          value={confirmNewPassword}
          onChange={(e) => setConfirmNewPassword(e.target.value)}
          required
          disabled={busy}
        />

        {error ? <p className="change-password-modal__error">{error}</p> : null}

        <div className="change-password-modal__actions">
          <button type="button" className="btn btn--ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="btn btn--primary" disabled={busy}>
            {busy ? 'Working…' : 'Change password'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
