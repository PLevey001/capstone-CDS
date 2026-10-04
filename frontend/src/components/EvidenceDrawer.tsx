import { useEffect, useRef, useState } from "react";
import {
  ArrowDownToLine,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  File,
  Fingerprint,
  Folder,
  HardDrive,
  LoaderCircle,
  Search,
  X,
} from "lucide-react";
import {
  api,
  bytes,
  date,
  type Artifact,
  type AnalysisRun,
  type Detail,
} from "../api";
import AnalysisHistory from "./AnalysisHistory";
import { CoverageDetails } from "./Coverage";
import Status from "./Status";
import RecordsPanel from "./RecordsPanel";

export default function EvidenceDrawer({
  id,
  revision,
  runId,
  artifactId,
  recordId,
  onClose,
}: {
  id: string;
  revision: string;
  runId?: string;
  artifactId?: number;
  recordId?: number;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<Detail | null>(null),
    [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [total, setTotal] = useState(0),
    [offset, setOffset] = useState(0),
    [query, setQuery] = useState("");
  const [error, setError] = useState(""),
    [expanded, setExpanded] = useState<number | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [selectedRun, setSelectedRun] = useState(runId || "");
  const [focusedId, setFocusedId] = useState<number | null>(
    recordId ? null : (artifactId ?? null),
  );
  const [selectedRecord, setSelectedRecord] = useState<number | null>(
    recordId ?? null,
  );
  const [view, setView] = useState<"files" | "records">(
    recordId ? "records" : "files",
  );
  const [focusedArtifact, setFocusedArtifact] = useState<Artifact | null>(null);
  const [runs, setRuns] = useState<AnalysisRun[]>([]);
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  useEffect(() => {
    let stopped = false;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const runQuery = selectedRun
          ? `?${new URLSearchParams({ run_id: selectedRun })}`
          : "";
        const [item, history] = await Promise.all([
          api<Detail>(`/evidence/${id}${runQuery}`, {
            signal: controller.signal,
          }),
          api<AnalysisRun[]>(`/evidence/${id}/runs`, {
            signal: controller.signal,
          }),
        ]);
        if (stopped) return;
        // Bind artifact retrieval to the snapshot we just loaded. A new run
        // finishing between requests must not mix old coverage with new rows.
        const rows = item.run_id
          ? await api<{ total: number; items: Artifact[] }>(
              `/evidence/${id}/artifacts?${new URLSearchParams({ q: query, offset: String(offset), run_id: item.run_id })}`,
              { signal: controller.signal },
            )
          : { total: 0, items: [] };
        const focused =
          focusedId !== null
            ? await api<Artifact>(`/evidence/${id}/artifacts/${focusedId}`, {
                signal: controller.signal,
              })
            : null;
        if (focused && focused.run_id !== item.run_id) {
          throw new Error(
            "The selected artifact does not belong to this saved run.",
          );
        }
        if (!stopped) {
          setDetail(item);
          setRuns(history);
          setArtifacts(rows.items);
          setTotal(rows.total);
          setFocusedArtifact(focused);
          setError("");
        }
      } catch (e) {
        if (!stopped) setError((e as Error).message);
      }
      if (!stopped) timer = setTimeout(refresh, 1500);
    };
    void refresh();
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [id, revision, query, offset, selectedRun, focusedId]);
  return (
    <dialog
      ref={ref}
      className="drawer"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="drawer-heading">
        <span className="eyebrow">EVIDENCE DETAILS</span>
        <button
          className="icon-button"
          aria-label="Close evidence details"
          onClick={onClose}
        >
          <X size={21} />
        </button>
      </div>
      {error && (
        <p role="alert" className="form-error">
          {error}
        </p>
      )}
      {detail ? (
        <>
          <div className="drawer-title">
            <span className="file-icon image-icon">
              {detail.kind === "raw_image" ? (
                <HardDrive size={25} />
              ) : (
                <File size={25} />
              )}
            </span>
            <div>
              <h2>{detail.name}</h2>
              <p>
                {bytes(detail.size)} ·{" "}
                {detail.kind === "raw_image"
                  ? "Raw disk image"
                  : "Logical file"}
              </p>
            </div>
          </div>
          <AnalysisHistory
            detail={detail}
            runs={runs}
            selected={selectedRun}
            onSelect={(runId) => {
              setSelectedRun(runId);
              setOffset(0);
              setQuery("");
              setExpanded(null);
              setDetail(null);
              setArtifacts([]);
              setFocusedId(null);
              setFocusedArtifact(null);
              setSelectedRecord(null);
            }}
          />
          <Status value={detail.status} />
          <p className="stage-label">
            {detail.status === "completed"
              ? "Processing finished"
              : detail.stage}
            {detail.status === "running" ? ` · ${detail.progress}%` : ""}
          </p>
          {detail.error && (
            <div className="error-detail">
              <p>{detail.error}</p>
            </div>
          )}
          <div className="evidence-views" aria-label="Evidence views">
            <button
              className="secondary-button"
              aria-pressed={view === "files"}
              onClick={() => setView("files")}
            >
              Files {detail.artifact_count}
            </button>
            <button
              className="secondary-button"
              aria-pressed={view === "records"}
              onClick={() => setView("records")}
            >
              Records {detail.record_count}
            </button>
          </div>
          {view === "records" && (
            <RecordsPanel
              key={detail.run_id || "pending"}
              evidence={detail}
              selectedId={selectedRecord}
              onSelect={(recordId) => {
                setSelectedRecord(recordId);
                if (recordId !== null) setSelectedRun(detail.run_id || "");
              }}
              onSource={(artifactId) => {
                setFocusedId(artifactId);
                setFocusedArtifact(null);
                setView("files");
              }}
            />
          )}
          {view === "files" && focusedArtifact && (
            <section
              className="timeline-artifact"
              aria-label="Selected timeline artifact"
            >
              <div className="artifact-heading">
                <h3>
                  {recordId || selectedRecord
                    ? "Source artifact"
                    : "Selected from timeline"}
                </h3>
                <button
                  className="icon-button"
                  aria-label="Clear selected artifact"
                  onClick={() => {
                    setFocusedId(null);
                    setFocusedArtifact(null);
                  }}
                >
                  <X size={16} />
                </button>
              </div>
              <strong>{focusedArtifact.path}</strong>
              <ArtifactDetails artifact={focusedArtifact} evidenceId={id} />
            </section>
          )}
          <CoverageDetails evidence={detail} />
          {(detail.current_job_status === "failed" ||
            detail.current_job_status === "completed") && (
            <div className="reanalyze-control">
              <button
                className="secondary-button"
                disabled={retrying}
                onClick={async () => {
                  setRetrying(true);
                  try {
                    await api(`/evidence/${id}/retry`, { method: "POST" });
                    setDetail({
                      ...detail,
                      status: selectedRun ? detail.status : "queued",
                      current_job_status: "queued",
                      stage: selectedRun ? detail.stage : "Queued",
                      progress: selectedRun ? detail.progress : 0,
                      error: selectedRun ? detail.error : null,
                    });
                    setOffset(0);
                    setExpanded(null);
                  } catch (e) {
                    setError((e as Error).message);
                  } finally {
                    setRetrying(false);
                  }
                }}
              >
                {retrying
                  ? "Queuing…"
                  : detail.current_job_status === "failed"
                    ? "Retry analysis"
                    : "Analyze again"}
              </button>
              <small>
                Starts a new run with current settings. All previous results are
                kept.
              </small>
            </div>
          )}
          <div className="hash-block">
            <label>
              <Fingerprint size={14} />
              SHA-256 · recorded for this result
            </label>
            <code>
              {detail.sha256 ||
                (detail.status === "queued" || detail.status === "running"
                  ? "Available after hashing completes"
                  : "Not recorded for this run")}
            </code>
          </div>
          <div className="detail-meta">
            <span>Imported</span>
            <strong>{date(detail.imported_at)}</strong>
            <span>Evidence ID</span>
            <code>{detail.id}</code>
          </div>
          {detail.warnings.map((warning, i) => (
            <div className="notice" key={i}>
              <CircleAlert size={16} />
              <span>{warning}</span>
            </div>
          ))}
          <details className="metadata-box">
            <summary>Source metadata</summary>
            <pre>{JSON.stringify(detail.metadata, null, 2)}</pre>
          </details>
          {detail.partitions.length > 0 && (
            <section className="partition-section">
              <h3>Observed partitions</h3>
              <p className="muted">
                Layout present in this image; not a history of changes.
              </p>
              {detail.partitions.map((p) => (
                <div className="partition-row" key={p.id}>
                  <HardDrive size={18} />
                  <div>
                    <strong>{p.description}</strong>
                    <small>
                      Sector {p.start_sector.toLocaleString()} ·{" "}
                      {bytes(p.length_sectors * p.sector_size)} ·{" "}
                      {p.sector_size}-byte sectors
                    </small>
                  </div>
                </div>
              ))}
            </section>
          )}
          {view === "files" && (
            <>
              <div className="artifact-heading">
                <h3>
                  Indexed artifacts <span>{total}</span>
                </h3>
                <span>
                  {detail.status === "queued" || detail.status === "running"
                    ? "Last saved results"
                    : "Files and directory entries"}
                </span>
              </div>
              <div className="search-input artifact-search">
                <Search size={16} />
                <input
                  aria-label="Search artifacts"
                  placeholder="Search paths…"
                  value={query}
                  onChange={(e) => {
                    setQuery(e.target.value);
                    setOffset(0);
                  }}
                />
              </div>
              <div className="artifact-list">
                {artifacts.map((a) => (
                  <div className="artifact-item" key={a.id}>
                    <button
                      onClick={() =>
                        setExpanded(expanded === a.id ? null : a.id)
                      }
                    >
                      {a.kind === "directory" ? (
                        <Folder size={16} />
                      ) : (
                        <File size={16} />
                      )}
                      <span>
                        <strong>{a.path}</strong>
                        <small>
                          {a.kind} · {bytes(a.size || 0)}
                          {a.deleted ? " · deleted entry" : ""}
                        </small>
                      </span>
                      <ChevronRight size={14} />
                    </button>
                    {expanded === a.id && (
                      <div className="artifact-detail">
                        <ArtifactDetails artifact={a} evidenceId={id} />
                      </div>
                    )}
                  </div>
                ))}
                {!artifacts.length && (
                  <p className="muted">
                    {detail.status === "running" || detail.status === "queued"
                      ? "Findings will appear when this source finishes."
                      : "No matching indexed artifacts. Check the coverage above for unexamined scope."}
                  </p>
                )}
              </div>
              <div className="pagination">
                <span>
                  {total ? offset + 1 : 0}–{Math.min(offset + 50, total)} of{" "}
                  {total}
                </span>
                <button
                  className="icon-button"
                  aria-label="Previous artifact page"
                  disabled={offset === 0}
                  onClick={() => setOffset(offset - 50)}
                >
                  <ChevronLeft size={17} />
                </button>
                <button
                  className="icon-button"
                  aria-label="Next artifact page"
                  disabled={offset + 50 >= total}
                  onClick={() => setOffset(offset + 50)}
                >
                  <ChevronRight size={17} />
                </button>
              </div>
            </>
          )}
        </>
      ) : (
        <div className="empty-state">
          <LoaderCircle className="spin" />
          Loading evidence…
        </div>
      )}
    </dialog>
  );
}

function ArtifactDetails({
  artifact,
  evidenceId,
}: {
  artifact: Artifact;
  evidenceId: string;
}) {
  return (
    <>
      <p>
        Run: <code>{artifact.run_id}</code> · Artifact ID: {artifact.id}
      </p>
      <p>
        Partition sector: {artifact.partition_offset ?? "N/A"} · Metadata
        address: {artifact.metadata_address ?? "N/A"}
      </p>
      {artifact.download.available ? (
        <p>
          <a
            className="secondary-button"
            href={`/api/evidence/${evidenceId}/artifacts/${artifact.id}/download`}
          >
            <ArrowDownToLine size={14} />
            {artifact.deleted ? "Recover file" : "Download file"}
          </a>
        </p>
      ) : (
        <p className="muted">{artifact.download.reason}</p>
      )}
      <pre>{JSON.stringify(artifact.details, null, 2)}</pre>
    </>
  );
}
