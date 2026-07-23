import type {
  AuthStatus,
  AuthToken,
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

let authToken: string | null = null;

export function setAuthToken(token: string | null) {
  authToken = token;
}

let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(fn: (() => void) | null) {
  onUnauthorized = fn;
}

function authHeaders(): HeadersInit {
  return authToken ? { Authorization: `Bearer ${authToken}` } : {};
}

async function handleResponse<T>(res: Response, tokenAtRequest?: string | null): Promise<T> {
  // Only treat a 401 as "the current session died" if the token this
  // specific request was sent with is still the token in use. A stale
  // in-flight request (sent with a token that's since been legitimately
  // rotated, e.g. by change-password) would otherwise be misread as the
  // current session dying and log the user out of their own still-valid,
  // freshly-rotated session.
  if (res.status === 401 && tokenAtRequest && tokenAtRequest === authToken) {
    onUnauthorized?.();
  }
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
  if (res.status === 204) {
    return undefined as T;
  }
  return res.json() as Promise<T>;
}

function getJSON<T>(path: string): Promise<T> {
  const tokenAtRequest = authToken;
  return fetch(path, { headers: authHeaders() }).then((res) =>
    handleResponse<T>(res, tokenAtRequest),
  );
}

function postJSON<T>(path: string, body: unknown): Promise<T> {
  const tokenAtRequest = authToken;
  return fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
  }).then((res) => handleResponse<T>(res, tokenAtRequest));
}

export function getAuthStatus(): Promise<AuthStatus> {
  return getJSON<AuthStatus>('/api/auth/status');
}

export function setupPassword(password: string): Promise<AuthToken> {
  return postJSON<AuthToken>('/api/auth/setup', { password });
}

export function login(password: string): Promise<AuthToken> {
  return postJSON<AuthToken>('/api/auth/login', { password });
}

export function logout(): Promise<void> {
  return postJSON<void>('/api/auth/logout', {});
}

export function changePassword(currentPassword: string, newPassword: string): Promise<AuthToken> {
  return postJSON<AuthToken>('/api/auth/change-password', {
    current_password: currentPassword,
    new_password: newPassword,
  });
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
