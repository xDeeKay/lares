import type { ReactNode } from 'react';
import './StatusMessage.css';

interface StatusMessageProps {
  tone?: 'muted' | 'danger';
  children: ReactNode;
}

export function StatusMessage({ tone = 'muted', children }: StatusMessageProps) {
  return <p className={`status-message status-message--${tone}`}>{children}</p>;
}
