export type Case = {
  id: string;
  name: string;
  description: string;
  created_at: string;
  evidence_count: number;
};
export type Evidence = {
  id: string;
  name: string;
  size: number;
  kind: string;
  sector_size: number;
  imported_at: string;
  status: "queued" | "running" | "completed" | "failed";
  progress: number;
  stage: string;
  sha256: string | null;
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
export type Detail = Evidence & { partitions: Partition[] };
export type Artifact = {
  id: number;
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
