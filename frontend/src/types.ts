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
