import { useState, type FormEvent } from 'react';
import { ApiError, createUptimeTarget, updateUptimeTarget } from '../api';
import type { UptimeTarget, UptimeTargetType } from '../types';
import { Modal } from './Modal';
import './UptimeTargetModal.css';

interface UptimeTargetModalProps {
  target: UptimeTarget | null;
  onClose: () => void;
  onSaved: () => void;
}

const ADDRESS_PLACEHOLDER: Record<UptimeTargetType, string> = {
  http: 'https://example.com/health',
  tcp: '192.168.1.10:22',
  ping: '192.168.1.10',
};

function validateAddress(type: UptimeTargetType, address: string): string | null {
  const trimmed = address.trim();
  if (!trimmed) return 'Address must not be empty.';

  if (type === 'http') {
    try {
      const parsed = new URL(trimmed);
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
        return 'http targets need a URL starting with http:// or https://.';
      }
    } catch {
      return 'http targets need a full URL starting with http:// or https://.';
    }
  } else if (type === 'tcp') {
    if (trimmed.includes('://')) {
      return 'tcp targets must be "host:port", not a URL (no http:// prefix).';
    }
    const idx = trimmed.lastIndexOf(':');
    const port = idx >= 0 ? Number(trimmed.slice(idx + 1)) : NaN;
    if (idx <= 0 || !Number.isInteger(port) || port < 1 || port > 65535) {
      return 'tcp targets must be "host:port", e.g. 192.168.1.10:22.';
    }
  } else if (type === 'ping') {
    if (trimmed.includes('://')) {
      return 'ping targets must be a bare hostname or IP, not a URL.';
    }
    if (trimmed.startsWith('-')) {
      return 'ping targets must not start with "-".';
    }
  }
  return null;
}

function parseOptionalSeconds(value: string): number | null | undefined {
  if (!value.trim()) return null; // blank = clear the override, use the global default
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1) return undefined; // undefined signals "invalid"
  return n;
}

export function UptimeTargetModal({ target, onClose, onSaved }: UptimeTargetModalProps) {
  const isEdit = target !== null;
  const [name, setName] = useState(target?.name ?? '');
  const [targetType, setTargetType] = useState<UptimeTargetType>(target?.target_type ?? 'http');
  const [address, setAddress] = useState(target?.address ?? '');
  const [enabled, setEnabled] = useState(target?.enabled ?? true);
  const [intervalInput, setIntervalInput] = useState(
    target?.check_interval_seconds != null ? String(target.check_interval_seconds) : '',
  );
  const [timeoutInput, setTimeoutInput] = useState(
    target?.check_timeout_seconds != null ? String(target.check_timeout_seconds) : '',
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    const trimmedName = name.trim();
    if (!trimmedName) {
      setError('Name must not be empty.');
      return;
    }
    const addressError = validateAddress(targetType, address);
    if (addressError) {
      setError(addressError);
      return;
    }

    const checkInterval = parseOptionalSeconds(intervalInput);
    if (checkInterval === undefined) {
      setError('Check interval must be a whole number of seconds (or blank for the default).');
      return;
    }
    const checkTimeout = parseOptionalSeconds(timeoutInput);
    if (checkTimeout === undefined) {
      setError('Check timeout must be a whole number of seconds (or blank for the default).');
      return;
    }

    setBusy(true);
    try {
      if (isEdit) {
        await updateUptimeTarget(target.id, {
          name: trimmedName,
          target_type: targetType,
          address: address.trim(),
          enabled,
          check_interval_seconds: checkInterval ?? undefined,
          check_timeout_seconds: checkTimeout ?? undefined,
          clear_check_interval: checkInterval === null,
          clear_check_timeout: checkTimeout === null,
        });
      } else {
        await createUptimeTarget({
          name: trimmedName,
          target_type: targetType,
          address: address.trim(),
          enabled,
          check_interval_seconds: checkInterval,
          check_timeout_seconds: checkTimeout,
        });
      }
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong, try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal onClose={onClose}>
      <h3>{isEdit ? 'Edit target' : 'Add target'}</h3>
      <form className="uptime-target-modal__form" onSubmit={handleSubmit}>
        <label className="uptime-target-modal__label" htmlFor="uptime-target-name">
          Name
        </label>
        <input
          id="uptime-target-name"
          type="text"
          className="uptime-target-modal__input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          autoFocus
          required
          disabled={busy}
        />

        <label className="uptime-target-modal__label" htmlFor="uptime-target-type">
          Type
        </label>
        <select
          id="uptime-target-type"
          className="uptime-target-modal__input"
          value={targetType}
          onChange={(e) => setTargetType(e.target.value as UptimeTargetType)}
          disabled={busy}
        >
          <option value="http">HTTP</option>
          <option value="tcp">TCP</option>
          <option value="ping">Ping</option>
        </select>

        <label className="uptime-target-modal__label" htmlFor="uptime-target-address">
          Address
        </label>
        <input
          id="uptime-target-address"
          type="text"
          className="uptime-target-modal__input"
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          placeholder={ADDRESS_PLACEHOLDER[targetType]}
          required
          disabled={busy}
        />

        <div className="uptime-target-modal__row">
          <div className="uptime-target-modal__col">
            <label className="uptime-target-modal__label" htmlFor="uptime-target-interval">
              Check interval (s)
            </label>
            <input
              id="uptime-target-interval"
              type="text"
              inputMode="numeric"
              className="uptime-target-modal__input"
              value={intervalInput}
              onChange={(e) => setIntervalInput(e.target.value)}
              placeholder="Default (30s)"
              disabled={busy}
            />
          </div>
          <div className="uptime-target-modal__col">
            <label className="uptime-target-modal__label" htmlFor="uptime-target-timeout">
              Check timeout (s)
            </label>
            <input
              id="uptime-target-timeout"
              type="text"
              inputMode="numeric"
              className="uptime-target-modal__input"
              value={timeoutInput}
              onChange={(e) => setTimeoutInput(e.target.value)}
              placeholder="Default (5s)"
              disabled={busy}
            />
          </div>
        </div>

        <label className="uptime-target-modal__checkbox-label">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            disabled={busy}
          />
          Enabled
        </label>

        {error ? <p className="uptime-target-modal__error">{error}</p> : null}

        <div className="uptime-target-modal__actions">
          <button type="button" className="btn btn--ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="btn btn--primary" disabled={busy}>
            {busy ? 'Working…' : isEdit ? 'Save' : 'Add target'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
