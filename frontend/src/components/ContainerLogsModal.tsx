import { useEffect, useState } from 'react';
import { ApiError, getContainerLogs } from '../api';
import { Modal } from './Modal';
import { StatusMessage } from './StatusMessage';
import './ContainerLogsModal.css';

const TAIL_LINES = 200;

interface ContainerLogsModalProps {
  containerId: string;
  containerName: string;
  onClose: () => void;
}

export function ContainerLogsModal({ containerId, containerName, onClose }: ContainerLogsModalProps) {
  const [lines, setLines] = useState<string[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const result = await getContainerLogs(containerId, TAIL_LINES);
      setLines(result.lines);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, [containerId]);

  return (
    <Modal onClose={onClose}>
      <div className="logs-modal__header">
        <h3>
          {containerName} <span className="logs-modal__subtitle">last {TAIL_LINES} lines</span>
        </h3>
        <button type="button" className="btn btn--ghost btn--small" onClick={load} disabled={loading}>
          Refresh
        </button>
      </div>

      {loading && !lines ? <StatusMessage>Loading logs…</StatusMessage> : null}
      {error && !lines ? (
        <StatusMessage tone="danger">
          {error instanceof ApiError ? error.message : "Couldn't load logs."}
        </StatusMessage>
      ) : null}
      {lines ? (
        <pre className="logs-modal__body">{lines.length > 0 ? lines.join('\n') : '(no log output)'}</pre>
      ) : null}

      <div className="logs-modal__footer">
        <button type="button" className="btn btn--ghost" onClick={onClose}>
          Close
        </button>
      </div>
    </Modal>
  );
}
