import {
  CategoryScale,
  Chart as ChartJS,
  LinearScale,
  LineElement,
  PointElement,
  Tooltip,
  type ChartOptions,
} from 'chart.js';
import { useEffect, useState } from 'react';
import { Line } from 'react-chartjs-2';
import { ApiError, getUptimeTargetHistory, getUptimeTargetIncidents } from '../api';
import type { UptimeCheck, UptimeIncident, UptimeTarget } from '../types';
import { Modal } from './Modal';
import { StatusMessage } from './StatusMessage';
import './UptimeTargetDetailModal.css';

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip);

const HISTORY_HOURS = 24;
const INCIDENTS_HOURS = 24 * 7;

interface UptimeTargetDetailModalProps {
  target: UptimeTarget;
  onClose: () => void;
}

function formatTimeLabel(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatFullTimestamp(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  return remMinutes > 0 ? `${hours}h ${remMinutes}m` : `${hours}h`;
}

export function UptimeTargetDetailModal({ target, onClose }: UptimeTargetDetailModalProps) {
  const [history, setHistory] = useState<UptimeCheck[] | null>(null);
  const [incidents, setIncidents] = useState<UptimeIncident[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([
      getUptimeTargetHistory(target.id, HISTORY_HOURS),
      getUptimeTargetIncidents(target.id, INCIDENTS_HOURS),
    ])
      .then(([h, i]) => {
        if (cancelled) return;
        setHistory(h);
        setIncidents(i);
      })
      .catch((err) => {
        if (!cancelled) setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [target.id]);

  const chartData = history
    ? {
        labels: history.map((c) => formatTimeLabel(c.timestamp)),
        datasets: [
          {
            label: 'Response time (ms)',
            data: history.map((c) => c.response_ms),
            spanGaps: true,
            borderColor: '#4f7a63',
            backgroundColor: '#4f7a63',
            pointBackgroundColor: history.map((c) => (c.is_up ? '#4f7a63' : '#b3654f')),
            pointRadius: history.length > 200 ? 0 : 2,
            tension: 0.2,
          },
        ],
      }
    : null;

  const chartOptions: ChartOptions<'line'> = {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    scales: {
      x: { ticks: { maxTicksLimit: 8, color: '#8fa396' }, grid: { color: '#2c332c' } },
      y: { ticks: { color: '#8fa396' }, grid: { color: '#2c332c' }, title: { display: true, text: 'ms' } },
    },
    plugins: {
      tooltip: {
        callbacks: {
          label: (ctx) => (ctx.parsed.y == null ? 'down' : `${ctx.parsed.y} ms`),
        },
      },
    },
  };

  return (
    <Modal onClose={onClose}>
      <div className="uptime-detail__header">
        <h3>{target.name}</h3>
        <span className="uptime-detail__subtitle">last {HISTORY_HOURS}h</span>
      </div>

      {loading && !history ? <StatusMessage>Loading history…</StatusMessage> : null}
      {error && !history ? (
        <StatusMessage tone="danger">
          {error instanceof ApiError ? error.message : "Couldn't load history."}
        </StatusMessage>
      ) : null}

      {chartData ? (
        history && history.length > 0 ? (
          <div className="uptime-detail__chart">
            <Line data={chartData} options={chartOptions} />
          </div>
        ) : (
          <StatusMessage>No checks recorded yet in this window.</StatusMessage>
        )
      ) : null}

      <h4 className="uptime-detail__incidents-title">Recent incidents (last {INCIDENTS_HOURS / 24}d)</h4>
      {incidents && incidents.length === 0 ? (
        <StatusMessage>No downtime recorded in this window.</StatusMessage>
      ) : null}
      {incidents && incidents.length > 0 ? (
        <ul className="uptime-detail__incidents">
          {incidents.map((inc) => (
            <li key={inc.started_at} className="uptime-detail__incident">
              <span className="uptime-detail__incident-time">{formatFullTimestamp(inc.started_at)}</span>
              <span className="uptime-detail__incident-duration">
                {inc.ended_at ? formatDuration(inc.duration_seconds) : 'ongoing'}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="uptime-detail__actions">
        <button type="button" className="btn btn--ghost" onClick={onClose}>
          Close
        </button>
      </div>
    </Modal>
  );
}
