export type Coverage = {
  schema_version: number;
  run_id: string;
  status: "complete" | "partial" | "failed" | "unknown";
  started_at: string | null;
  finished_at: string | null;
  scope: string;
  tool_version?: string;
  limits: Record<string, number>;
  steps: {
    id: string;
    label: string;
    parser: string;
    status:
      "complete" | "partial" | "failed" | "skipped" | "unsupported" | "unknown";
    reason: string;
    detail: string;
    processed: number;
    total: number | null;
    unit: string;
    partition_offset?: number;
    sector_size?: number;
    records_returned?: number;
    malformed_records?: number;
  }[];
};
export type Case = {
  id: string;
  name: string;
  description: string;
  created_at: string;
  evidence_count: number;
};
export type JobStatus = "queued" | "running" | "completed" | "failed";
export type RunStatus = JobStatus | "interrupted" | "unknown";
export type AnalysisRun = {
  id: string;
  evidence_id: string;
  sequence: number;
  status: RunStatus;
  started_at: string | null;
  finished_at: string | null;
  settings: Record<string, string | number>;
  error: string | null;
  legacy: boolean;
  artifact_count: number;
  coverage_status: Coverage["status"];
  parser_version: string | null;
  tool_version: string | null;
};
export type RunSnapshot = Omit<
  AnalysisRun,
  "artifact_count" | "coverage_status" | "parser_version" | "tool_version"
> & {
  metadata: Record<string, unknown>;
  coverage: Coverage | null;
  warnings: string[];
};
export type Evidence = {
  id: string;
  name: string;
  size: number;
  kind: string;
  sector_size: number;
  imported_at: string;
  status: RunStatus;
  current_job_status: JobStatus;
  run_id: string | null;
  active_run_id: string | null;
  run_count: number;
  progress: number;
  stage: string;
  sha256: string | null;
  coverage: Coverage | null;
  warnings: string[];
  error: string | null;
  artifact_count: number;
  metadata: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
};
export type Partition = {
  id: number;
  slot: string;
  start_sector: number;
  length_sectors: number;
  sector_size: number;
  description: string;
};
export type Detail = Evidence & {
  case_id: string;
  partitions: Partition[];
  run: RunSnapshot | null;
};
export type Artifact = {
  id: number;
  run_id: string;
  path: string;
  kind: string;
  size: number;
  deleted: boolean;
  partition_offset: number | null;
  metadata_address: string | null;
  details: Record<string, unknown>;
};
export type Audit = {
  id: number;
  action: string;
  detail: string;
  at: string;
  evidence_id: string | null;
};
export type TimelineEvent = {
  at: string;
  timestamp_kind: string;
  timestamp_label: string;
  origin: "filesystem" | "import";
  source_id: string;
  source_name: string;
  artifact_id: number | null;
  artifact_path: string;
  artifact_kind: string;
  deleted: boolean;
};
export type Timeline = {
  case: Case;
  total: number;
  events: TimelineEvent[];
};
export type Health = {
  workers: number;
  coordinator_running: boolean;
  sleuthkit_available: boolean;
  max_upload_bytes: number;
};

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { "X-CDS-Request": "local-ui", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      typeof body?.detail === "string"
        ? body.detail
        : `Request failed (${response.status})`,
    );
  }
  return response.json();
}

export function bytes(size: number): string {
  if (size < 1024) return `${size} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = size / 1024,
    index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index++;
  }
  return `${value.toFixed(1)} ${units[index]}`;
}
export function date(value: string) {
  return new Date(value).toLocaleString();
}
