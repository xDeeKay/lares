import { useEffect, useState } from 'react';
import {
  ApiError,
  getDevices,
  getLanSettings,
  scanLanNow,
  updateDevice,
  updateLanSettings,
} from '../api';
import { usePolling } from '../hooks/usePolling';
import type { Device, DeviceCategory, DeviceState } from '../types';
import { StatusMessage } from './StatusMessage';
import './Devices.css';

const POLL_INTERVAL_MS = 15_000;
const CATEGORIES: DeviceCategory[] = ['trusted', 'iot', 'guest', 'unknown'];

function stateBadgeClass(state: DeviceState): string {
  return state === 'present' ? 'status-badge--healthy' : 'status-badge--muted';
}

function categorySelectClass(category: DeviceCategory): string {
  return `devices__category-select devices__category-select--${category}`;
}

function formatTimestamp(ts: string | null | undefined): string {
  if (!ts) return 'never';
  return new Date(ts).toLocaleString();
}

export function Devices() {
  const { data: devices, error, loading, refetch: refetchDevices } = usePolling(
    getDevices,
    POLL_INTERVAL_MS,
  );
  const { data: settings, refetch: refetchSettings } = usePolling(getLanSettings, POLL_INTERVAL_MS);

  const [cidrInput, setCidrInput] = useState('');
  const [intervalInput, setIntervalInput] = useState('');
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [scanBusy, setScanBusy] = useState(false);
  const [nicknameEdits, setNicknameEdits] = useState<Record<string, string>>({});

  // Only seed the inputs from the server once per settings change, not on
  // every poll, so typing isn't clobbered mid-edit by the next 15s refetch.
  useEffect(() => {
    if (settings) {
      setCidrInput(settings.cidr ?? '');
      setIntervalInput(String(settings.scan_interval_seconds));
    }
  }, [settings]);

  async function handleSaveSettings() {
    setSettingsBusy(true);
    setSettingsError(null);
    try {
      const trimmedCidr = cidrInput.trim();
      const parsedInterval = Number(intervalInput);
      await updateLanSettings({
        cidr: trimmedCidr === '' ? null : trimmedCidr,
        clear_cidr: trimmedCidr === '',
        scan_interval_seconds: Number.isFinite(parsedInterval) ? parsedInterval : undefined,
      });
      refetchSettings();
    } catch (err) {
      setSettingsError(err instanceof ApiError ? err.message : 'Failed to save scan settings.');
    } finally {
      setSettingsBusy(false);
    }
  }

  async function handleScanNow() {
    setScanBusy(true);
    try {
      await scanLanNow();
      refetchSettings();
      refetchDevices();
    } catch {
      // Same as Uptime's "check now": a failed nudge isn't worth its own
      // error banner, last_scan_at simply won't have moved.
    } finally {
      setScanBusy(false);
    }
  }

  async function handleCategoryChange(device: Device, category: DeviceCategory) {
    try {
      await updateDevice(device.mac_address, { category });
      refetchDevices();
    } catch {
      // Non-critical inline edit; the select reverts on the next poll if it failed.
    }
  }

  function handleNicknameInput(mac: string, value: string) {
    setNicknameEdits((prev) => ({ ...prev, [mac]: value }));
  }

  async function handleNicknameCommit(device: Device) {
    const value = nicknameEdits[device.mac_address];
    if (value === undefined || value === (device.nickname ?? '')) return;
    try {
      await updateDevice(device.mac_address, {
        nickname: value.trim() === '' ? undefined : value,
        clear_nickname: value.trim() === '',
      });
      refetchDevices();
    } catch {
      // Reverts visually on the next poll if this fails.
    }
  }

  if (loading && !devices) {
    return <StatusMessage>Loading devices…</StatusMessage>;
  }

  if (error && !devices) {
    return <StatusMessage tone="danger">Can't reach the Lares API. Is the backend running?</StatusMessage>;
  }

  return (
    <section className="card devices">
      <h2>LAN Devices</h2>

      <div className="devices__settings">
        <label>
          Scan range
          <input
            type="text"
            placeholder={settings?.effective_cidr ?? 'auto-detect'}
            value={cidrInput}
            onChange={(e) => setCidrInput(e.target.value)}
          />
        </label>
        <label>
          Interval (s)
          <input
            type="number"
            min={30}
            value={intervalInput}
            onChange={(e) => setIntervalInput(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="btn btn--ghost btn--small"
          onClick={handleSaveSettings}
          disabled={settingsBusy}
        >
          Save
        </button>
        <button
          type="button"
          className="btn btn--primary btn--small"
          onClick={handleScanNow}
          disabled={scanBusy}
        >
          {scanBusy ? 'Scanning…' : 'Scan now'}
        </button>
        <span className="devices__last-scan">Last scan: {formatTimestamp(settings?.last_scan_at)}</span>
      </div>
      {settingsError ? <StatusMessage tone="danger">{settingsError}</StatusMessage> : null}

      {devices && devices.length === 0 ? (
        <StatusMessage>No devices seen yet. They'll appear here after the next scan.</StatusMessage>
      ) : null}

      {devices && devices.length > 0 ? (
        <div className="devices__table-wrap">
          <table className="devices__table">
            <thead>
              <tr>
                <th>Device</th>
                <th>IP</th>
                <th>Vendor</th>
                <th>Category</th>
                <th>Status</th>
                <th>Last seen</th>
              </tr>
            </thead>
            <tbody>
              {devices.map((d) => (
                <tr key={d.mac_address}>
                  <td>
                    <input
                      type="text"
                      className="devices__nickname-input"
                      placeholder={d.hostname ?? d.mac_address}
                      value={nicknameEdits[d.mac_address] ?? d.nickname ?? ''}
                      onChange={(e) => handleNicknameInput(d.mac_address, e.target.value)}
                      onBlur={() => handleNicknameCommit(d)}
                    />
                    <div className="devices__mac">{d.mac_address}</div>
                  </td>
                  <td>{d.last_ip ?? '—'}</td>
                  <td>{d.vendor ?? '—'}</td>
                  <td>
                    <select
                      className={categorySelectClass(d.category)}
                      value={d.category}
                      onChange={(e) => handleCategoryChange(d, e.target.value as DeviceCategory)}
                    >
                      {CATEGORIES.map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <span className={`status-badge ${stateBadgeClass(d.state)}`}>{d.state}</span>
                  </td>
                  <td>{formatTimestamp(d.last_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {error ? (
        <StatusMessage tone="danger">Last update failed, showing most recent known data.</StatusMessage>
      ) : null}
    </section>
  );
}
