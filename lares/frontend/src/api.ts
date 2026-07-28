import type {
  AuthStatus,
  AuthToken,
  BleScanSettings,
  ContainerAction,
  ContainerActionResult,
  ContainerInfo,
  ContainerLogs,
  Device,
  DeviceCategory,
  DeviceSighting,
  DiskConfig,
  DiskInfo,
  LanScanSettings,
  SystemMetric,
  UptimeCheck,
  UptimeIncident,
  UptimeStatus,
  UptimeTarget,
  UptimeTargetType,
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

function patchJSON<T>(path: string, body: unknown): Promise<T> {
  const tokenAtRequest = authToken;
  return fetch(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
  }).then((res) => handleResponse<T>(res, tokenAtRequest));
}

function deleteRequest<T>(path: string): Promise<T> {
  const tokenAtRequest = authToken;
  return fetch(path, { method: 'DELETE', headers: authHeaders() }).then((res) =>
    handleResponse<T>(res, tokenAtRequest),
  );
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

export function getUptimeStatus(): Promise<UptimeStatus[]> {
  return getJSON<UptimeStatus[]>('/api/uptime/status');
}

export function getUptimeTargets(): Promise<UptimeTarget[]> {
  return getJSON<UptimeTarget[]>('/api/uptime/targets');
}

export interface UptimeTargetInput {
  name: string;
  target_type: UptimeTargetType;
  address: string;
  enabled?: boolean;
  check_interval_seconds?: number | null;
  check_timeout_seconds?: number | null;
}

export interface UptimeTargetPatch extends Partial<UptimeTargetInput> {
  clear_check_interval?: boolean;
  clear_check_timeout?: boolean;
}

export function createUptimeTarget(target: UptimeTargetInput): Promise<UptimeTarget> {
  return postJSON<UptimeTarget>('/api/uptime/targets', target);
}

export function updateUptimeTarget(id: number, patch: UptimeTargetPatch): Promise<UptimeTarget> {
  return patchJSON<UptimeTarget>(`/api/uptime/targets/${id}`, patch);
}

export function deleteUptimeTarget(id: number): Promise<void> {
  return deleteRequest<void>(`/api/uptime/targets/${id}`);
}

export function checkUptimeTargetNow(id: number): Promise<UptimeStatus> {
  return postJSON<UptimeStatus>(`/api/uptime/targets/${id}/check`, {});
}

export function getUptimeTargetHistory(id: number, hours = 24): Promise<UptimeCheck[]> {
  return getJSON<UptimeCheck[]>(`/api/uptime/targets/${id}/history?hours=${hours}`);
}

export function getUptimeTargetIncidents(id: number, hours = 24 * 7): Promise<UptimeIncident[]> {
  return getJSON<UptimeIncident[]>(`/api/uptime/targets/${id}/incidents?hours=${hours}`);
}

export function getDevices(): Promise<Device[]> {
  return getJSON<Device[]>('/api/lan/devices');
}

export interface DeviceUpdateInput {
  category?: DeviceCategory;
  nickname?: string;
  clear_nickname?: boolean;
}

export function updateDevice(macAddress: string, patch: DeviceUpdateInput): Promise<Device> {
  return patchJSON<Device>(`/api/lan/devices/${encodeURIComponent(macAddress)}`, patch);
}

export function getDeviceSightings(macAddress: string, hours = 24): Promise<DeviceSighting[]> {
  return getJSON<DeviceSighting[]>(
    `/api/lan/devices/${encodeURIComponent(macAddress)}/sightings?hours=${hours}`,
  );
}

export function getLanSettings(): Promise<LanScanSettings> {
  return getJSON<LanScanSettings>('/api/lan/settings');
}

export interface LanScanSettingsInput {
  cidr?: string | null;
  scan_interval_seconds?: number;
  clear_cidr?: boolean;
}

export function updateLanSettings(patch: LanScanSettingsInput): Promise<LanScanSettings> {
  return patchJSON<LanScanSettings>('/api/lan/settings', patch);
}

export function scanLanNow(): Promise<LanScanSettings> {
  return postJSON<LanScanSettings>('/api/lan/scan-now', {});
}

export function getBleDevices(): Promise<Device[]> {
  return getJSON<Device[]>('/api/ble/devices');
}

export function updateBleDevice(macAddress: string, patch: DeviceUpdateInput): Promise<Device> {
  return patchJSON<Device>(`/api/ble/devices/${encodeURIComponent(macAddress)}`, patch);
}

export function getBleDeviceSightings(macAddress: string, hours = 24): Promise<DeviceSighting[]> {
  return getJSON<DeviceSighting[]>(
    `/api/ble/devices/${encodeURIComponent(macAddress)}/sightings?hours=${hours}`,
  );
}

export function getBleSettings(): Promise<BleScanSettings> {
  return getJSON<BleScanSettings>('/api/ble/settings');
}

export interface BleScanSettingsInput {
  flush_interval_seconds?: number;
}

export function updateBleSettings(patch: BleScanSettingsInput): Promise<BleScanSettings> {
  return patchJSON<BleScanSettings>('/api/ble/settings', patch);
}

export function flushBleNow(): Promise<BleScanSettings> {
  return postJSON<BleScanSettings>('/api/ble/flush-now', {});
}
