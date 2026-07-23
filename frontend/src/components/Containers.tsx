import { useState } from 'react';
import { ApiError, getContainers, postContainerAction } from '../api';
import { usePolling } from '../hooks/usePolling';
import type { ContainerAction, ContainerInfo } from '../types';
import { ConfirmDialog } from './ConfirmDialog';
import { ContainerLogsModal } from './ContainerLogsModal';
import { StatusMessage } from './StatusMessage';
import './Containers.css';

const POLL_INTERVAL_MS = 15_000;

function statusBadgeClass(status: string): string {
  if (status === 'running') return 'status-badge--healthy';
  if (status === 'exited' || status === 'dead') return 'status-badge--danger';
  return 'status-badge--muted';
}

interface PendingAction {
  container: ContainerInfo;
  action: ContainerAction;
}

export function Containers() {
  const { data, error, loading, refetch } = usePolling(getContainers, POLL_INTERVAL_MS);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [logsFor, setLogsFor] = useState<ContainerInfo | null>(null);

  function closeConfirm() {
    setPendingAction(null);
    setActionError(null);
  }

  async function handleConfirm() {
    if (!pendingAction) return;
    setActionBusy(true);
    setActionError(null);
    try {
      await postContainerAction(pendingAction.container.container_id, pendingAction.action);
      setPendingAction(null);
      refetch();
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : `Failed to ${pendingAction.action} container.`,
      );
    } finally {
      setActionBusy(false);
    }
  }

  if (loading && !data) {
    return <StatusMessage>Loading containers…</StatusMessage>;
  }

  if (error && !data) {
    return <StatusMessage tone="danger">Can't reach the Lares API. Is the backend running?</StatusMessage>;
  }

  if (data && data.length === 0) {
    return (
      <StatusMessage>
        No containers found. Is Docker reachable, and the container collector running?
      </StatusMessage>
    );
  }

  if (!data) {
    return null;
  }

  return (
    <section className="card containers">
      <h2>Containers</h2>
      <div className="containers__table-wrap">
        <table className="containers__table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Image</th>
              <th>Status</th>
              <th aria-label="Actions"></th>
            </tr>
          </thead>
          <tbody>
            {data.map((c) => (
              <tr key={c.container_id}>
                <td>{c.name}</td>
                <td className="containers__image">
                  <span className="containers__image-ref">{c.image}</span>
                  {c.update_available ? <span className="update-badge">Update available</span> : null}
                </td>
                <td>
                  <span className={`status-badge ${statusBadgeClass(c.status)}`}>{c.status}</span>
                </td>
                <td className="containers__actions">
                  <button type="button" className="btn btn--ghost btn--small" onClick={() => setLogsFor(c)}>
                    Logs
                  </button>
                  <button
                    type="button"
                    className="btn btn--bronze btn--small"
                    onClick={() => setPendingAction({ container: c, action: 'restart' })}
                  >
                    Restart
                  </button>
                  <button
                    type="button"
                    className="btn btn--danger btn--small"
                    disabled={c.status !== 'running'}
                    onClick={() => setPendingAction({ container: c, action: 'stop' })}
                  >
                    Stop
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {error ? (
        <StatusMessage tone="danger">Last update failed, showing most recent known data.</StatusMessage>
      ) : null}

      {pendingAction ? (
        <ConfirmDialog
          title={`${pendingAction.action === 'stop' ? 'Stop' : 'Restart'} ${pendingAction.container.name}?`}
          message={
            pendingAction.action === 'stop'
              ? `This will stop ${pendingAction.container.name}. Anything depending on it will go down too.`
              : `This will restart ${pendingAction.container.name}, briefly interrupting it.`
          }
          confirmLabel={pendingAction.action === 'stop' ? 'Stop' : 'Restart'}
          tone={pendingAction.action === 'stop' ? 'danger' : 'default'}
          busy={actionBusy}
          error={actionError}
          onConfirm={handleConfirm}
          onCancel={closeConfirm}
        />
      ) : null}

      {logsFor ? (
        <ContainerLogsModal
          containerId={logsFor.container_id}
          containerName={logsFor.name}
          onClose={() => setLogsFor(null)}
        />
      ) : null}
    </section>
  );
}
