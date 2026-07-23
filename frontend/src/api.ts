import type {
  ContainerAction,
  ContainerActionResult,
  ContainerInfo,
  ContainerLogs,
  DiskConfig,
  DiskInfo,
  SystemMetric,
} from './types';

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail: string | undefined;
    try {
      const body = await res.json();
      detail = typeof body?.detail === 'string' ? body.detail : undefined;
    } catch {
      // response wasn't JSON, fall back to statusText below
    }
    throw new ApiError(res.status, detail ?? res.statusText);
  }
  return res.json() as Promise<T>;
}

function getJSON<T>(path: string): Promise<T> {
  return fetch(path).then((res) => handleResponse<T>(res));
}

function postJSON<T>(path: string, body: unknown): Promise<T> {
  return fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((res) => handleResponse<T>(res));
}

export function getLatestSystemMetric(): Promise<SystemMetric> {
  return getJSON<SystemMetric>('/api/system/metrics/latest');
}

export function getLatestDiskInfo(): Promise<DiskInfo[]> {
  return getJSON<DiskInfo[]>('/api/disk/latest');
}

export function getDiskConfig(): Promise<DiskConfig> {
  return getJSON<DiskConfig>('/api/config/disk');
}

export function getContainers(): Promise<ContainerInfo[]> {
  return getJSON<ContainerInfo[]>('/api/containers');
}

export function getContainerLogs(containerId: string, tail = 200): Promise<ContainerLogs> {
  return getJSON<ContainerLogs>(
    `/api/containers/${encodeURIComponent(containerId)}/logs?tail=${tail}`,
  );
}

export function postContainerAction(
  containerId: string,
  action: ContainerAction,
): Promise<ContainerActionResult> {
  return postJSON<ContainerActionResult>(
    `/api/containers/${encodeURIComponent(containerId)}/${action}`,
    { confirm: true },
  );
}
