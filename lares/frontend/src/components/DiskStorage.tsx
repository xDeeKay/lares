import { getLatestDiskInfo } from '../api';
import { usePolling } from '../hooks/usePolling';
import type { DiskFreshness, DiskInfo } from '../types';
import { StatusMessage } from './StatusMessage';
import './DiskStorage.css';

const POLL_INTERVAL_MS = 30_000;

const FRESHNESS_LABEL: Record<DiskFreshness, string> = {
  fresh: 'Fresh',
  stale: 'Stale',
  missing: 'Missing',
};

function usageFillClass(usedPct: number): string {
  if (usedPct >= 90) return 'disk-bar__fill--danger';
  if (usedPct >= 75) return 'disk-bar__fill--warning';
  return 'disk-bar__fill--healthy';
}

function DiskCard({ disk }: { disk: DiskInfo }) {
  return (
    <div className={`disk-card disk-card--${disk.freshness}`}>
      <div className="disk-card__header">
        <span className="disk-card__mount">{disk.mount_point}</span>
        {disk.freshness !== 'fresh' && (
          <span className={`freshness-badge freshness-badge--${disk.freshness}`}>
            {FRESHNESS_LABEL[disk.freshness]}
          </span>
        )}
      </div>
      <div className="disk-bar">
        <div
          className={`disk-bar__fill ${usageFillClass(disk.used_pct)}`}
          style={{ width: `${Math.min(disk.used_pct, 100)}%` }}
        />
      </div>
      <div className="disk-card__detail">
        {disk.used_gb.toFixed(1)} / {disk.total_gb.toFixed(1)} GB used ({disk.used_pct.toFixed(1)}%)
      </div>
      <div className="disk-card__device">{disk.device}</div>
    </div>
  );
}

export function DiskStorage() {
  const { data, error, loading } = usePolling(getLatestDiskInfo, POLL_INTERVAL_MS);

  if (loading && !data) {
    return <StatusMessage>Loading drive info…</StatusMessage>;
  }

  if (error && !data) {
    return <StatusMessage tone="danger">Can't reach the Lares API. Is the backend running?</StatusMessage>;
  }

  if (data && data.length === 0) {
    return <StatusMessage>No drives reported yet. Is the disk collector running?</StatusMessage>;
  }

  if (!data) {
    return null;
  }

  return (
    <section className="card disk-storage">
      <h2>Storage</h2>
      <div className="disk-storage__grid">
        {data.map((disk) => (
          <DiskCard key={disk.mount_point} disk={disk} />
        ))}
      </div>
      {error ? (
        <StatusMessage tone="danger">Last update failed, showing most recent known data.</StatusMessage>
      ) : null}
    </section>
  );
}
