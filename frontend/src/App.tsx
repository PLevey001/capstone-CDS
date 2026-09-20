import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Database,
  File,
  FileSearch,
  Fingerprint,
  Folder,
  FolderOpen,
  HardDrive,
  Layers,
  ListFilter,
  LoaderCircle,
  Pencil,
  Plus,
  Search,
  ShieldCheck,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  api,
  bytes,
  date,
  type Artifact,
  type Audit,
  type Case,
  type Detail,
  type Evidence,
  type Health,
} from "./api";
import Modal from "./components/Modal";
import { DeleteCase, RenameCase } from "./components/CaseDialogs";

type Tab = "evidence" | "activity";
type UploadItem = { name: string; status: string; failed?: boolean };

function Status({ value }: { value: Evidence["status"] }) {
  return (
    <span className={`status ${value}`}>
      {value === "running" ? (
        <LoaderCircle className="spin" size={12} />
      ) : value === "completed" ? (
        <Check size={12} />
      ) : value === "failed" ? (
        <CircleAlert size={12} />
      ) : (
        <span className="status-dot" />
      )}
      {value === "completed"
        ? "Complete"
        : value[0].toUpperCase() + value.slice(1)}
    </span>
  );
}

export default function App() {
  const [cases, setCases] = useState<Case[]>([]);
  const [caseId, setCaseId] = useState("");
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<Tab>("evidence");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [showCreate, setShowCreate] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  const [selected, setSelected] = useState("");
  const [caseAction, setCaseAction] = useState<"rename" | "delete" | null>(
    null,
  );
  const [message, setMessage] = useState("");
  const pollGeneration = useRef(0);
  const currentCase = cases.find((c) => c.id === caseId);

  useEffect(() => {
    Promise.all([api<Case[]>("/cases"), api<Health>("/health")])
      .then(([list, system]) => {
        setCases(list);
        setHealth(system);
        setCaseId(list[0]?.id || "");
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const reload = useCallback(async () => {
    const list = await api<Case[]>("/cases");
    setCases(list);
  }, []);

  useEffect(() => {
    const generation = ++pollGeneration.current;
    setEvidence([]);
    setAudit([]);
    setSelected("");
    if (!caseId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const [items, events, system] = await Promise.all([
          api<Evidence[]>(`/cases/${caseId}/evidence`),
          api<Audit[]>(`/cases/${caseId}/audit`),
          api<Health>("/health"),
        ]);
        if (!stopped && generation === pollGeneration.current) {
          setEvidence(items);
          setAudit(events);
          setHealth(system);
        }
      } catch (e) {
        if (!stopped && generation === pollGeneration.current)
          setError((e as Error).message);
      }
      if (!stopped && generation === pollGeneration.current)
        timer = setTimeout(poll, 1200);
    };
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [caseId]);

  const completed = evidence.filter((e) => e.status === "completed").length;
  const active = evidence.filter((e) => e.status === "running").length;
  const queued = evidence.filter((e) => e.status === "queued").length;
  const failed = evidence.filter((e) => e.status === "failed").length;
  const shown = evidence.filter(
    (e) =>
      e.name.toLowerCase().includes(query.toLowerCase()) &&
      (filter === "all" || e.status === filter),
  );

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="CDS home">
          <span className="brand-symbol">
            <Fingerprint size={26} />
          </span>
          <span>
            CDS<span className="brand-sub">DETECTION SOLUTIONS</span>
          </span>
        </a>
        <div className="workspace-label">INVESTIGATION WORKSPACE</div>
        <div className="nav-active">
          <Layers size={18} />
          <span>Cases</span>
          <span className="nav-count">{cases.length}</span>
        </div>
        <div className="sidebar-heading">
          <span>YOUR CASES</span>
          <button
            className="icon-button"
            title="Create case"
            onClick={() => setShowCreate(true)}
          >
            <Plus size={16} />
          </button>
        </div>
        <nav className="case-list" aria-label="Cases">
          {cases.map((c) => (
            <button
              key={c.id}
              aria-label={c.name}
              title={c.name}
              className={`case-link ${c.id === caseId ? "selected" : ""}`}
              onClick={() => {
                setCaseId(c.id);
                setQuery("");
                setFilter("all");
              }}
            >
              <Folder size={16} />
              <span>{c.name}</span>
            </button>
          ))}
        </nav>
        {!cases.length && (
          <p className="sidebar-empty">
            Your investigations
            <br />
            start here.
          </p>
        )}
        <button
          className="new-case"
          aria-label="New case"
          title="New case"
          onClick={() => setShowCreate(true)}
        >
          <Plus size={16} /> New case
        </button>
        <div className="sidebar-bottom">
          <div className="local-state">
            <span />
            Local workspace
          </div>
          <p>Evidence stays on this machine.</p>
          <div className="prototype-label">
            CAPSTONE PROTOTYPE <span>v0.1</span>
          </div>
        </div>
      </aside>

      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumbs">
            Workspace <ChevronRight size={13} />{" "}
            <span>{currentCase?.name || "Cases"}</span>
          </div>
          <span className="engine-state">
            <span
              className={
                health?.coordinator_running ? "online-dot" : "offline-dot"
              }
            />
            {health?.coordinator_running
              ? "Analysis engine online"
              : health
                ? "Analysis engine offline"
                : "Connecting…"}
          </span>
        </header>
        <main>
          {message && (
            <div className="notice" role="status">
              <span>{message}</span>
              <button
                className="icon-button"
                aria-label="Dismiss notification"
                onClick={() => setMessage("")}
              >
                <X size={16} />
              </button>
            </div>
          )}
          {error && (
            <div className="error-banner" role="alert">
              <CircleAlert size={17} />
              {error}
              <button
                className="icon-button"
                aria-label="Dismiss error"
                onClick={() => setError("")}
              >
                <X size={16} />
              </button>
            </div>
          )}
          {!health?.sleuthkit_available && health && (
            <div className="notice">
              <CircleAlert size={16} />
              The Sleuth Kit is unavailable. File analysis works; raw image
              analysis requires installation.
            </div>
          )}
          <div className="page-heading">
            <div>
              <div className="eyebrow">CASE WORKSPACE</div>
              <h1>{currentCase?.name || "A clearer view of the evidence."}</h1>
              <p>
                {currentCase?.description ||
                  "Bring your evidence together. Follow every finding back to its source."}
              </p>
            </div>
            <div className="case-actions">
              {currentCase && (
                <>
                  <button
                    className="secondary-button"
                    onClick={() => setCaseAction("rename")}
                  >
                    <Pencil size={15} />
                    Rename case
                  </button>
                  <button
                    className="secondary-button danger-text"
                    onClick={() => setCaseAction("delete")}
                  >
                    <Trash2 size={15} />
                    Delete case
                  </button>
                  <a
                    className="secondary-button"
                    href={`/api/cases/${currentCase.id}/export?format=csv`}
                  >
                    <ArrowDownToLine size={15} />
                    Export CSV
                  </a>
                </>
              )}
              <button
                className="primary-button"
                onClick={() =>
                  currentCase ? setShowUpload(true) : setShowCreate(true)
                }
              >
                {currentCase ? <Upload size={16} /> : <Plus size={16} />}
                {currentCase ? "Add evidence" : "Create a case"}
              </button>
            </div>
          </div>
          <div className="stats-grid">
            <Stat
              label="Evidence sources"
              value={evidence.length}
              icon={<Database size={19} />}
              foot={
                bytes(evidence.reduce((sum, e) => sum + e.size, 0)) +
                " imported"
              }
            />
            <Stat
              label="Analysis complete"
              value={completed}
              icon={<ShieldCheck size={19} />}
              foot="Hashed and indexed"
            />
            <Stat
              label="Processing"
              value={active}
              icon={<Activity size={19} />}
              foot={`${queued} queued · ${health?.workers || 2} worker slots`}
            />
            <Stat
              label="Artifacts indexed"
              value={evidence.reduce((sum, e) => sum + e.artifact_count, 0)}
              icon={<FileSearch size={19} />}
              foot={
                failed
                  ? `${failed} source${failed > 1 ? "s" : ""} need attention`
                  : "Ready for review"
              }
            />
          </div>
          <section className="evidence-panel">
            <div className="panel-tabs">
              <button
                className={tab === "evidence" ? "active" : ""}
                onClick={() => setTab("evidence")}
              >
                <FolderOpen size={16} />
                Evidence <span>{evidence.length}</span>
              </button>
              <button
                className={tab === "activity" ? "active" : ""}
                onClick={() => setTab("activity")}
              >
                <Activity size={16} />
                Activity log
              </button>
              <span className="panel-note">
                <ShieldCheck size={13} />
                Read-only source analysis
              </span>
            </div>
            {tab === "evidence" ? (
              <>
                <div className="table-toolbar">
                  <div className="search-input">
                    <Search size={16} />
                    <input
                      aria-label="Search evidence"
                      placeholder="Search evidence by filename…"
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                    />
                  </div>
                  <div className="filter-control">
                    <ListFilter size={15} />
                    <select
                      aria-label="Filter evidence by status"
                      value={filter}
                      onChange={(e) => setFilter(e.target.value)}
                    >
                      <option value="all">All statuses</option>
                      <option value="queued">Queued</option>
                      <option value="running">Running</option>
                      <option value="completed">Complete</option>
                      <option value="failed">Failed</option>
                    </select>
                  </div>
                </div>
                {loading ? (
                  <div className="empty-state">
                    <LoaderCircle className="spin" />
                    <h2>Opening your workspace…</h2>
                  </div>
                ) : shown.length ? (
                  <div className="table-scroll">
                    <table className="evidence-table">
                      <thead>
                        <tr>
                          <th>NAME / SOURCE</th>
                          <th>SIZE</th>
                          <th>STATUS</th>
                          <th>ARTIFACTS</th>
                          <th>IMPORTED</th>
                          <th>
                            <span className="sr-only">Open</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {shown.map((item) => (
                          <tr
                            key={item.id}
                            onClick={() => setSelected(item.id)}
                          >
                            <td>
                              <button
                                className="file-cell"
                                onClick={() => setSelected(item.id)}
                              >
                                <span
                                  className={`file-icon ${item.kind === "raw_image" ? "image-icon" : ""}`}
                                >
                                  {item.kind === "raw_image" ? (
                                    <HardDrive size={21} />
                                  ) : (
                                    <File size={21} />
                                  )}
                                </span>
                                <span>
                                  <strong>{item.name}</strong>
                                  <small>
                                    {item.kind === "raw_image"
                                      ? "Raw disk image"
                                      : "Logical file"}{" "}
                                    ·{" "}
                                    {item.sha256
                                      ? `SHA-256 ${item.sha256.slice(0, 10)}…`
                                      : "Hash pending"}
                                  </small>
                                </span>
                              </button>
                            </td>
                            <td className="mono size-cell">
                              {bytes(item.size)}
                            </td>
                            <td>
                              <Status value={item.status} />
                              {item.status === "running" && (
                                <div
                                  className="job-progress"
                                  title={item.stage}
                                >
                                  <span
                                    style={{ width: `${item.progress}%` }}
                                  />
                                </div>
                              )}
                              {item.warnings.length > 0 && (
                                <small className="warning-count">
                                  {item.warnings.length} notice
                                  {item.warnings.length > 1 ? "s" : ""}
                                </small>
                              )}
                            </td>
                            <td className="mono">
                              {item.artifact_count.toLocaleString()}
                            </td>
                            <td className="date-cell">
                              {new Date(item.imported_at).toLocaleDateString(
                                undefined,
                                { month: "short", day: "numeric" },
                              )}
                              <small>
                                {new Date(item.imported_at).toLocaleTimeString(
                                  undefined,
                                  { hour: "2-digit", minute: "2-digit" },
                                )}
                              </small>
                            </td>
                            <td>
                              <ArrowRight size={16} />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <div className="empty-state">
                    <div className="empty-icon">
                      <FolderOpen size={30} />
                      <span>
                        <Plus size={12} />
                      </span>
                    </div>
                    <h2>
                      {query || filter !== "all"
                        ? "No matching evidence"
                        : "Every investigation starts with evidence."}
                    </h2>
                    <p>
                      {query || filter !== "all"
                        ? "Try another filename or status."
                        : currentCase
                          ? "Add files or raw disk images to start hashing and analysis.\nMultiple sources can be processed at the same time."
                          : "Create your first case, then add files or disk images.\nYour analysis and findings will appear here."}
                    </p>
                    {!query && filter === "all" && (
                      <button
                        className="primary-button"
                        onClick={() =>
                          currentCase
                            ? setShowUpload(true)
                            : setShowCreate(true)
                        }
                      >
                        <Plus size={16} />
                        {currentCase
                          ? "Add your first evidence"
                          : "Create your first case"}
                      </button>
                    )}
                    <div className="supported-formats">
                      TXT <span /> CSV <span /> JSON <span /> RAW / DD / IMG
                    </div>
                  </div>
                )}
                <div className="panel-footer">
                  <span>
                    {shown.length} of {evidence.length} sources
                  </span>
                  <span>
                    <span className="online-dot" />
                    Progress updates automatically
                  </span>
                </div>
              </>
            ) : (
              <div className="activity-list">
                {audit.length ? (
                  audit.map((event) => (
                    <div className="activity-row" key={event.id}>
                      <span className="activity-icon">
                        <Activity size={15} />
                      </span>
                      <div>
                        <strong>{event.action.replaceAll("_", " ")}</strong>
                        <p>{event.detail}</p>
                      </div>
                      <time>{date(event.at)}</time>
                    </div>
                  ))
                ) : (
                  <div className="empty-state">
                    <Activity size={28} />
                    <h2>No activity yet</h2>
                    <p>Case and analysis actions will be recorded here.</p>
                  </div>
                )}
                <p className="audit-note">
                  Latest 200 CDS actions. This log is not a complete device
                  history or a tamper-proof chain of custody.
                </p>
              </div>
            )}
          </section>
          <div className="workspace-footer">
            <span>
              <Fingerprint size={16} /> CDS · Capstone Detection Solutions
            </span>
            <span>Local analysis. Traceable findings.</span>
          </div>
        </main>
      </div>
      {showCreate && (
        <CreateCase
          onClose={() => setShowCreate(false)}
          onCreated={async (item) => {
            await reload();
            setCaseId(item.id);
            setShowCreate(false);
          }}
        />
      )}
      {caseAction === "rename" && currentCase && (
        <RenameCase
          item={currentCase}
          onClose={() => setCaseAction(null)}
          onRenamed={(item) => {
            setCases((previous) =>
              previous.map((c) => (c.id === item.id ? item : c)),
            );
            setCaseAction(null);
            setMessage("Case renamed.");
          }}
        />
      )}
      {caseAction === "delete" && currentCase && (
        <DeleteCase
          item={currentCase}
          processing={active + queued > 0}
          onClose={() => setCaseAction(null)}
          onDeleted={(cleanupPending) => {
            // Ignore requests that were in flight when the case was deleted.
            pollGeneration.current++;
            const remaining = cases.filter((c) => c.id !== caseId);
            setCases(remaining);
            setCaseId(remaining[0]?.id || "");
            setEvidence([]);
            setAudit([]);
            setSelected("");
            setQuery("");
            setFilter("all");
            setTab("evidence");
            setError("");
            setCaseAction(null);
            setMessage(
              cleanupPending
                ? "Case deleted. Some stored files could not be removed. CDS will retry cleanup on restart; check the server log if this continues."
                : "Case and stored evidence deleted.",
            );
          }}
        />
      )}
      {showUpload && currentCase && (
        <UploadModal
          caseId={caseId}
          health={health}
          onClose={() => {
            setShowUpload(false);
            void reload().catch((e) => setError(e.message));
          }}
        />
      )}
      {selected && (
        <EvidenceDrawer
          id={selected}
          revision={evidence.find((e) => e.id === selected)?.status || ""}
          onClose={() => setSelected("")}
        />
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  icon,
  foot,
}: {
  label: string;
  value: number;
  icon: React.ReactNode;
  foot: string;
}) {
  return (
    <div className="stat-card">
      <div className="stat-top">
        {label}
        <span>{icon}</span>
      </div>
      <div className="stat-number">{value.toLocaleString()}</div>
      <div className="stat-foot">{foot}</div>
    </div>
  );
}

function CreateCase({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (item: Case) => Promise<void>;
}) {
  const [name, setName] = useState(""),
    [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <Modal title="Create an investigation case" onClose={onClose} busy={busy}>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          try {
            const item = await api<Case>("/cases", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ name, description }),
            });
            await onCreated(item);
          } catch (e) {
            setError((e as Error).message);
            setBusy(false);
          }
        }}
      >
        <p className="modal-description">
          Keep related evidence, findings, and analysis actions together.
        </p>
        <label>
          Case name
          <input
            autoFocus
            required
            maxLength={120}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. USB activity investigation"
          />
        </label>
        <label>
          Description <span className="muted">(optional)</span>
          <textarea
            rows={3}
            maxLength={2000}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What are you investigating?"
          />
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="modal-actions">
          <button
            type="button"
            className="secondary-button"
            disabled={busy}
            onClick={onClose}
          >
            Cancel
          </button>
          <button className="primary-button" disabled={busy || !name.trim()}>
            {busy && <LoaderCircle className="spin" size={15} />}Create case
          </button>
        </div>
      </form>
    </Modal>
  );
}

function UploadModal({
  caseId,
  health,
  onClose,
}: {
  caseId: string;
  health: Health | null;
  onClose: () => void;
}) {
  const [files, setFiles] = useState<File[]>([]),
    [uploads, setUploads] = useState<UploadItem[]>([]);
  const [kind, setKind] = useState("auto"),
    [sector, setSector] = useState("512");
  const [busy, setBusy] = useState(false),
    [done, setDone] = useState(false),
    [drag, setDrag] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const max = health?.max_upload_bytes || 4 * 1024 ** 3;
  const add = (list: FileList | null) => {
    if (list && !busy && !done) {
      setFiles((previous) => [...previous, ...Array.from(list)]);
      setDone(false);
      setUploads([]);
    }
  };
  const submit = async () => {
    setBusy(true);
    setUploads(
      files.map((f) => ({ name: f.name, status: "Waiting to upload" })),
    );
    let cursor = 0;
    const update = (index: number, status: string, failed = false) =>
      setUploads((items) =>
        items.map((item, i) =>
          i === index ? { ...item, status, failed } : item,
        ),
      );
    await Promise.all(
      Array.from({ length: Math.min(3, files.length) }, async () => {
        while (cursor < files.length) {
          const index = cursor++,
            file = files[index];
          if (file.size > max) {
            update(index, `Exceeds ${bytes(max)} limit`, true);
            continue;
          }
          update(index, "Uploading…");
          try {
            const params = new URLSearchParams({
              filename: file.name,
              kind,
              sector_size: sector,
            });
            await api(`/cases/${caseId}/evidence?${params}`, {
              method: "POST",
              headers: { "Content-Type": "application/octet-stream" },
              body: file,
            });
            update(index, "Imported · analysis queued");
          } catch (e) {
            update(index, (e as Error).message, true);
          }
        }
      }),
    );
    setBusy(false);
    setDone(true);
  };
  return (
    <Modal title="Add evidence" onClose={onClose} busy={busy}>
      <p className="modal-description">
        Sources are copied into this case and analyzed read-only. Select
        multiple files to queue them together.
      </p>
      <input
        ref={input}
        className="sr-only"
        type="file"
        multiple
        aria-label="Choose evidence files"
        onChange={(e) => add(e.target.files)}
      />
      <button
        className={`dropzone ${drag ? "dragging" : ""}`}
        disabled={busy || done}
        onClick={() => input.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          add(e.dataTransfer.files);
        }}
      >
        <span className="upload-symbol">
          <ArrowDownToLine size={25} />
        </span>
        <strong>Drop evidence here, or browse files</strong>
        <small>
          Raw images, JSON, CSV, text · other files receive hash and metadata
        </small>
        <small>Up to {bytes(max)} per source</small>
      </button>
      <div className="input-row">
        <label>
          Analysis mode
          <select
            value={kind}
            disabled={busy || done}
            onChange={(e) => setKind(e.target.value)}
          >
            <option value="auto">Detect from extension</option>
            <option value="file">Logical file</option>
            <option value="raw_image">Raw disk image</option>
          </select>
        </label>
        <label>
          Image sector size
          <select
            value={sector}
            disabled={busy || done}
            onChange={(e) => setSector(e.target.value)}
          >
            <option value="512">512 bytes (default)</option>
            <option value="4096">4096 bytes</option>
          </select>
        </label>
      </div>
      <div className="upload-list">
        {files.map((file, i) => (
          <div className="upload-row" key={i}>
            <File size={17} />
            <div>
              <strong>{file.name}</strong>
              <small className={uploads[i]?.failed ? "form-error" : ""}>
                {uploads[i]?.status || bytes(file.size)}
              </small>
            </div>
            {!busy && !done && (
              <button
                className="icon-button"
                aria-label={`Remove ${file.name}`}
                onClick={() => setFiles(files.filter((_, j) => i !== j))}
              >
                <X size={15} />
              </button>
            )}
          </div>
        ))}
      </div>
      <div className="notice subtle">
        <ShieldCheck size={16} />
        SHA-256 is computed from the stored source. Raw images are never
        mounted.
      </div>
      <div className="modal-actions">
        <button className="secondary-button" disabled={busy} onClick={onClose}>
          {done ? "Close" : "Cancel"}
        </button>
        {!done && (
          <button
            className="primary-button"
            disabled={busy || !files.length}
            onClick={() => void submit()}
          >
            {busy ? (
              <LoaderCircle className="spin" size={16} />
            ) : (
              <Upload size={16} />
            )}
            {busy
              ? "Importing…"
              : `Import ${files.length || ""} source${files.length === 1 ? "" : "s"}`}
          </button>
        )}
        {done && (
          <button className="primary-button" onClick={onClose}>
            View analysis <ArrowRight size={16} />
          </button>
        )}
      </div>
    </Modal>
  );
}

function EvidenceDrawer({
  id,
  revision,
  onClose,
}: {
  id: string;
  revision: string;
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
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const [item, rows] = await Promise.all([
          api<Detail>(`/evidence/${id}`),
          api<{ total: number; items: Artifact[] }>(
            `/evidence/${id}/artifacts?${new URLSearchParams({ q: query, offset: String(offset) })}`,
          ),
        ]);
        if (!stopped) {
          setDetail(item);
          setArtifacts(rows.items);
          setTotal(rows.total);
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
      clearTimeout(timer);
    };
  }, [id, revision, query, offset]);
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
          <Status value={detail.status} />
          <p className="stage-label">
            {detail.stage}
            {detail.status === "running" ? ` · ${detail.progress}%` : ""}
          </p>
          {detail.error && (
            <div className="error-detail">
              <p>{detail.error}</p>
              <button
                className="secondary-button"
                disabled={retrying}
                onClick={async () => {
                  setRetrying(true);
                  try {
                    await api(`/evidence/${id}/retry`, { method: "POST" });
                    setDetail({
                      ...detail,
                      status: "queued",
                      stage: "Queued",
                      error: null,
                    });
                  } catch (e) {
                    setError((e as Error).message);
                  } finally {
                    setRetrying(false);
                  }
                }}
              >
                Retry analysis
              </button>
            </div>
          )}
          <div className="hash-block">
            <label>
              <Fingerprint size={14} />
              SHA-256 · stored source
            </label>
            <code>{detail.sha256 || "Available after hashing completes"}</code>
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
          <div className="artifact-heading">
            <h3>
              Indexed artifacts <span>{total}</span>
            </h3>
            <span>Source-linked records</span>
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
                  onClick={() => setExpanded(expanded === a.id ? null : a.id)}
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
                    <p>
                      Partition sector: {a.partition_offset ?? "N/A"} · Metadata
                      address: {a.metadata_address ?? "N/A"}
                    </p>
                    <pre>{JSON.stringify(a.details, null, 2)}</pre>
                  </div>
                )}
              </div>
            ))}
            {!artifacts.length && (
              <p className="muted">
                {detail.status === "running" || detail.status === "queued"
                  ? "Findings will appear when this source finishes."
                  : "No matching artifacts."}
              </p>
            )}
          </div>
          <div className="pagination">
            <span>
              {total ? offset + 1 : 0}–{Math.min(offset + 50, total)} of {total}
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
      ) : (
        <div className="empty-state">
          <LoaderCircle className="spin" />
          Loading evidence…
        </div>
      )}
    </dialog>
  );
}
