// Mirrors backend/routers/*.py's Pydantic response models. Keep in sync by
// hand; there's no shared schema generation between the two yet.

export interface ThrottleState {
  raw: string | null;
  available: boolean;
  flags: Record<string, boolean>;
}

export interface SystemMetric {
  host: string;
  timestamp: string;
  cpu_pct: number | null;
  mem_used_mb: number | null;
  mem_total_mb: number | null;
  mem_used_pct: number | null;
  temp_c: number | null;
  load_1m: number | null;
  throttled: ThrottleState;
}

export type DiskFreshness = 'fresh' | 'stale' | 'missing';

export interface DiskInfo {
  device: string;
  mount_point: string;
  timestamp: string;
  total_gb: number;
  used_gb: number;
  free_gb: number;
  used_pct: number;
  freshness: DiskFreshness;
}

export interface DiskConfig {
  poll_interval_seconds: number;
  stale_threshold_seconds: number;
  missing_threshold_seconds: number;
}

export interface ContainerInfo {
  container_id: string;
  name: string;
  image: string;
  status: string;
  update_available: boolean;
  last_updated: string;
}

export type ContainerAction = 'stop' | 'restart';

export interface ContainerActionResult {
  container_id: string;
  action: ContainerAction;
  timestamp: string;
  success: boolean;
  status: string | null;
}

export interface ContainerLogs {
  container_id: string;
  lines: string[];
}

export interface AuthStatus {
  setup_required: boolean;
}

export interface AuthToken {
  token: string;
}

export type UptimeTargetType = 'http' | 'tcp' | 'ping';

export interface UptimeTarget {
  id: number;
  name: string;
  target_type: UptimeTargetType;
  address: string;
  enabled: boolean;
  created_at: string;
  check_interval_seconds: number | null;
  check_timeout_seconds: number | null;
}

export type UptimeState = 'up' | 'down' | 'pending' | 'stale' | 'unknown';

export interface UptimeStatus {
  target: UptimeTarget;
  state: UptimeState;
  last_checked: string | null;
  response_ms: number | null;
  sla_24h_pct: number | null;
  sla_7d_pct: number | null;
}

export interface UptimeCheck {
  timestamp: string;
  is_up: boolean;
  response_ms: number | null;
}

export interface UptimeIncident {
  started_at: string;
  ended_at: string | null;
  duration_seconds: number;
}

export type DeviceCategory = 'trusted' | 'iot' | 'guest' | 'unknown';
export type DeviceState = 'present' | 'absent';

export interface Device {
  mac_address: string;
  device_type: string;
  vendor: string | null;
  hostname: string | null;
  last_ip: string | null;
  last_rssi: number | null;
  category: DeviceCategory;
  nickname: string | null;
  first_seen: string;
  last_seen: string;
  state: DeviceState;
}

export interface DeviceSighting {
  timestamp: string;
  ip_address: string | null;
  rssi: number | null;
  is_present: boolean;
}

export interface LanScanSettings {
  cidr: string | null;
  effective_cidr: string | null;
  scan_interval_seconds: number;
  last_scan_at: string | null;
}

export interface BleScanSettings {
  flush_interval_seconds: number;
  last_flush_at: string | null;
}
