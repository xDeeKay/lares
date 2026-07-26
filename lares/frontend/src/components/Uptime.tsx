import { useState } from 'react';
import { ApiError, checkUptimeTargetNow, deleteUptimeTarget, getUptimeStatus } from '../api';
import { usePolling } from '../hooks/usePolling';
import type { UptimeState, UptimeStatus, UptimeTarget } from '../types';
import { ConfirmDialog } from './ConfirmDialog';
import { StatusMessage } from './StatusMessage';
import { UptimeTargetDetailModal } from './UptimeTargetDetailModal';
import { UptimeTargetModal } from './UptimeTargetModal';
import './Uptime.css';

const POLL_INTERVAL_MS = 15_000;

function statusBadgeClass(state: UptimeState): string {
  if (state === 'up') return 'status-badge--healthy';
  if (state === 'down') return 'status-badge--danger';
  if (state === 'pending') return 'status-badge--warning';
  return 'status-badge--muted';
}

function formatSla(pct: number | null): string {
  return pct == null ? '—' : `${pct.toFixed(1)}%`;
}

function formatResponseMs(ms: number | null): string {
  return ms == null ? '—' : `${ms} ms`;
}

export function Uptime() {
  const { data, error, loading, refetch } = usePolling(getUptimeStatus, POLL_INTERVAL_MS);
  const [modalTarget, setModalTarget] = useState<UptimeTarget | null | 'new'>(null);
  const [detailTarget, setDetailTarget] = useState<UptimeTarget | null>(null);
  const [pendingDelete, setPendingDelete] = useState<UptimeTarget | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [checkingIds, setCheckingIds] = useState<Set<number>>(new Set());

  function closeModal() {
    setModalTarget(null);
  }

  function handleSaved() {
    closeModal();
    refetch();
  }

  function closeDeleteConfirm() {
    setPendingDelete(null);
    setDeleteError(null);
  }

  async function handleConfirmDelete() {
    if (!pendingDelete) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteUptimeTarget(pendingDelete.id);
      setPendingDelete(null);
      refetch();
    } catch (err) {
      setDeleteError(err instanceof ApiError ? err.message : 'Failed to delete target.');
    } finally {
      setDeleteBusy(false);
    }
  }

  async function handleCheckNow(target: UptimeTarget) {
    setCheckingIds((prev) => new Set(prev).add(target.id));
    try {
      await checkUptimeTargetNow(target.id);
      refetch();
    } catch {
      // A failed manual check isn't worth its own error banner; the row's
      // status simply won't have updated, which is visible enough.
    } finally {
      setCheckingIds((prev) => {
        const next = new Set(prev);
        next.delete(target.id);
        return next;
      });
    }
  }

  if (loading && !data) {
    return <StatusMessage>Loading uptime status…</StatusMessage>;
  }

  if (error && !data) {
    return <StatusMessage tone="danger">Can't reach the Lares API. Is the backend running?</StatusMessage>;
  }

  return (
    <section className="card uptime">
      <div className="uptime__header">
        <h2>Uptime</h2>
        <button type="button" className="btn btn--primary btn--small" onClick={() => setModalTarget('new')}>
          + Add target
        </button>
      </div>

      {data && data.length === 0 ? (
        <StatusMessage>No services being monitored yet. Add one to get started.</StatusMessage>
      ) : null}

      {data && data.length > 0 ? (
        <div className="uptime__table-wrap">
          <table className="uptime__table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Status</th>
                <th>Response</th>
                <th>24h</th>
                <th>7d</th>
                <th aria-label="Actions"></th>
              </tr>
            </thead>
            <tbody>
              {data.map((s: UptimeStatus) => (
                <tr key={s.target.id}>
                  <td>
                    <button
                      type="button"
                      className="uptime__name-link"
                      onClick={() => setDetailTarget(s.target)}
                    >
                      {s.target.name}
                    </button>
                  </td>
                  <td className="uptime__type">{s.target.target_type}</td>
                  <td>
                    <span className={`status-badge ${statusBadgeClass(s.state)}`}>{s.state}</span>
                  </td>
                  <td>{formatResponseMs(s.response_ms)}</td>
                  <td>{formatSla(s.sla_24h_pct)}</td>
                  <td>{formatSla(s.sla_7d_pct)}</td>
                  <td className="uptime__actions">
                    <button
                      type="button"
                      className="btn btn--ghost btn--small"
                      onClick={() => handleCheckNow(s.target)}
                      disabled={checkingIds.has(s.target.id)}
                    >
                      {checkingIds.has(s.target.id) ? 'Checking…' : 'Check now'}
                    </button>
                    <button
                      type="button"
                      className="btn btn--ghost btn--small"
                      onClick={() => setModalTarget(s.target)}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn btn--danger btn--small"
                      onClick={() => setPendingDelete(s.target)}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {error ? (
        <StatusMessage tone="danger">Last update failed, showing most recent known data.</StatusMessage>
      ) : null}

      {modalTarget !== null ? (
        <UptimeTargetModal
          target={modalTarget === 'new' ? null : modalTarget}
          onClose={closeModal}
          onSaved={handleSaved}
        />
      ) : null}

      {detailTarget ? (
        <UptimeTargetDetailModal target={detailTarget} onClose={() => setDetailTarget(null)} />
      ) : null}

      {pendingDelete ? (
        <ConfirmDialog
          title={`Delete ${pendingDelete.name}?`}
          message={`This will stop monitoring ${pendingDelete.name} and permanently delete its check history.`}
          confirmLabel="Delete"
          tone="danger"
          busy={deleteBusy}
          error={deleteError}
          onConfirm={handleConfirmDelete}
          onCancel={closeDeleteConfirm}
        />
      ) : null}
    </section>
  );
}
