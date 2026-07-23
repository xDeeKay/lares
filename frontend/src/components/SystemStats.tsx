import { ApiError, getLatestSystemMetric } from '../api';
import { usePolling } from '../hooks/usePolling';
import type { ThrottleState } from '../types';
import { StatusMessage } from './StatusMessage';
import './SystemStats.css';

const POLL_INTERVAL_MS = 10_000;

const ACTIVE_FLAG_LABELS: Record<string, string> = {
  under_voltage: 'Under-voltage detected',
  arm_freq_capped: 'ARM frequency capped',
  currently_throttled: 'Currently throttled',
  soft_temp_limit: 'Soft temperature limit active',
};

const HISTORICAL_FLAG_LABELS: Record<string, string> = {
  under_voltage_occurred: 'Under-voltage has occurred',
  arm_freq_capped_occurred: 'Frequency capping has occurred',
  throttled_occurred: 'Throttling has occurred',
  soft_temp_limit_occurred: 'Soft temperature limit has occurred',
};

function formatNumber(value: number | null, unit: string, decimals = 1): string {
  return value == null ? '—' : `${value.toFixed(decimals)}${unit}`;
}

function ThrottleIndicator({ throttled }: { throttled: ThrottleState }) {
  if (!throttled.available) {
    return (
      <div className="throttle-indicator throttle-indicator--muted">
        Throttle state unavailable (not running on a Pi, or vcgencmd failed)
      </div>
    );
  }

  const activeIssues = Object.entries(ACTIVE_FLAG_LABELS).filter(([key]) => throttled.flags[key]);
  const historicalIssues = Object.entries(HISTORICAL_FLAG_LABELS).filter(
    ([key]) => throttled.flags[key],
  );

  if (activeIssues.length > 0) {
    return (
      <div className="throttle-indicator throttle-indicator--danger">
        <strong>Throttling active:</strong>{' '}
        {activeIssues.map(([, label]) => label).join(', ')}
      </div>
    );
  }

  if (historicalIssues.length > 0) {
    return (
      <div className="throttle-indicator throttle-indicator--warning">
        No active throttling, but occurred since boot: {historicalIssues.map(([, label]) => label).join(', ')}
      </div>
    );
  }

  return <div className="throttle-indicator throttle-indicator--healthy">No throttling detected</div>;
}

export function SystemStats() {
  const { data, error, loading } = usePolling(getLatestSystemMetric, POLL_INTERVAL_MS);

  if (loading && !data) {
    return <StatusMessage>Loading system stats…</StatusMessage>;
  }

  if (error instanceof ApiError && error.status === 404 && !data) {
    return <StatusMessage>No system metrics collected yet. Is the collector running?</StatusMessage>;
  }

  if (error && !data) {
    return <StatusMessage tone="danger">Can't reach the Lares API. Is the backend running?</StatusMessage>;
  }

  if (!data) {
    return null;
  }

  return (
    <section className="card system-stats">
      <h2>System</h2>
      <div className="system-stats__grid">
        <div className="stat-tile">
          <span className="stat-tile__label">CPU</span>
          <span className="stat-tile__value">{formatNumber(data.cpu_pct, '%')}</span>
        </div>
        <div className="stat-tile">
          <span className="stat-tile__label">Memory</span>
          <span className="stat-tile__value">{formatNumber(data.mem_used_pct, '%')}</span>
          <span className="stat-tile__detail">
            {data.mem_used_mb != null && data.mem_total_mb != null
              ? `${(data.mem_used_mb / 1024).toFixed(1)} / ${(data.mem_total_mb / 1024).toFixed(1)} GB`
              : '—'}
          </span>
        </div>
        <div className="stat-tile">
          <span className="stat-tile__label">Temp</span>
          <span className="stat-tile__value">{formatNumber(data.temp_c, '°C')}</span>
        </div>
        <div className="stat-tile">
          <span className="stat-tile__label">Load (1m)</span>
          <span className="stat-tile__value">{formatNumber(data.load_1m, '', 2)}</span>
        </div>
      </div>
      <ThrottleIndicator throttled={data.throttled} />
      {error ? (
        <StatusMessage tone="danger">Last update failed, showing most recent known data.</StatusMessage>
      ) : null}
    </section>
  );
}
