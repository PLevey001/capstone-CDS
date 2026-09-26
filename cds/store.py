import json
import logging
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cds.migrations import migrate_history

logger = logging.getLogger(__name__)


class CaseBusyError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.db_path = root / "cds.sqlite3"
        self.cleanup_lock = threading.Lock()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        for directory in (self.root, self.root / "evidence", self.root / "work"):
            directory.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    description TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    name TEXT NOT NULL, size INTEGER NOT NULL, source_path TEXT NOT NULL,
                    kind TEXT NOT NULL, sector_size INTEGER NOT NULL,
                    imported_at TEXT NOT NULL, sha256 TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}', warnings TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, evidence_id TEXT UNIQUE NOT NULL REFERENCES evidence(id),
                    status TEXT NOT NULL, stage TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY, evidence_id TEXT NOT NULL REFERENCES evidence(id),
                    path TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER,
                    deleted INTEGER NOT NULL, partition_offset INTEGER,
                    metadata_address TEXT, details TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS artifacts_evidence ON artifacts(evidence_id);
                CREATE TABLE IF NOT EXISTS partitions (
                    id INTEGER PRIMARY KEY, evidence_id TEXT NOT NULL REFERENCES evidence(id),
                    slot TEXT NOT NULL, start_sector INTEGER NOT NULL, length_sectors INTEGER NOT NULL,
                    sector_size INTEGER NOT NULL, description TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    evidence_id TEXT, action TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_deletions (
                    path TEXT PRIMARY KEY, case_id TEXT NOT NULL
                );
            """)
            migrate_history(db)
        self.cleanup_deleted_files()

    @staticmethod
    def event(db, case_id, action, detail, evidence_id=None):
        db.execute("INSERT INTO audit(case_id,evidence_id,action,detail,at) VALUES(?,?,?,?,?)",
                   (case_id, evidence_id, action, detail, now()))

    def create_case(self, name, description):
        item = {"id": str(uuid4()), "name": name, "description": description, "created_at": now()}
        with self.connect() as db:
            db.execute("INSERT INTO cases VALUES(:id,:name,:description,:created_at)", item)
            self.event(db, item["id"], "case_created", name)
        return item

    def cases(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT c.*, COUNT(e.id) AS evidence_count FROM cases c
                LEFT JOIN evidence e ON e.case_id=c.id GROUP BY c.id ORDER BY c.created_at DESC
            """)]

    def case_exists(self, case_id):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is not None

    def rename_case(self, case_id, name):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                return None
            if row["name"] != name:
                db.execute("UPDATE cases SET name=? WHERE id=?", (name, case_id))
                self.event(db, case_id, "case_renamed", f"{row['name']} → {name}")
            count = db.execute("SELECT COUNT(*) FROM evidence WHERE case_id=?", (case_id,)).fetchone()[0]
            return {**dict(row), "name": name, "evidence_count": count}

    def delete_case(self, case_id):
        """Remove metadata and durably schedule owned files for deletion together."""
        with self.connect() as db:
            # Serialize with claim/retry/register so no new worker can start
            # between the active-job check and deleting its source.
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is None:
                return False
            jobs = db.execute("""SELECT j.id,j.status FROM jobs j JOIN evidence e ON e.id=j.evidence_id
                WHERE e.case_id=?""", (case_id,)).fetchall()
            if any(job["status"] in {"queued", "running"} for job in jobs):
                raise CaseBusyError("Wait for queued and running analyses to finish before deleting this case.")
            sources = db.execute("SELECT id FROM evidence WHERE case_id=?", (case_id,)).fetchall()
            # Paths come from generated IDs, never from a displayed case/file name.
            runs = db.execute("SELECT id FROM analysis_runs WHERE evidence_id IN (SELECT id FROM evidence WHERE case_id=?)",
                              (case_id,)).fetchall()
            paths = list(dict.fromkeys([f"evidence/{row['id']}" for row in sources]
                         + [f"work/{job['id']}" for job in jobs] + [f"work/{run['id']}" for run in runs]))
            db.executemany("INSERT INTO pending_deletions(path,case_id) VALUES(?,?)",
                           [(path, case_id) for path in paths])
            for table in ("artifacts", "partitions", "jobs"):
                db.execute(f"DELETE FROM {table} WHERE evidence_id IN (SELECT id FROM evidence WHERE case_id=?)",
                           (case_id,))
            db.execute("UPDATE evidence SET result_run_id=NULL WHERE case_id=?", (case_id,))
            db.execute("DELETE FROM analysis_runs WHERE evidence_id IN (SELECT id FROM evidence WHERE case_id=?)", (case_id,))
            db.execute("DELETE FROM audit WHERE case_id=?", (case_id,))
            db.execute("DELETE FROM evidence WHERE case_id=?", (case_id,))
            db.execute("DELETE FROM cases WHERE id=?", (case_id,))
        return True

    def cleanup_deleted_files(self, case_id=None):
        # Filesystem changes cannot share a SQLite transaction. Keep a durable
        # queue so a crash or permission error does not silently orphan evidence.
        with self.cleanup_lock:
            with self.connect() as db:
                rows = db.execute("SELECT * FROM pending_deletions").fetchall()
            for row in rows:
                if case_id is not None and row["case_id"] != case_id:
                    continue
                relative = Path(row["path"])
                try:
                    if (len(relative.parts) != 2 or relative.parts[0] not in {"evidence", "work"}
                            or relative.parts[1] in {".", ".."}):
                        raise ValueError("Invalid cleanup path")
                    target = self.root / relative
                    if target.parent.is_symlink():
                        raise ValueError("Cleanup directory must not be a symlink")
                    if relative.parts[0] == "work" and target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink(missing_ok=True)
                except (OSError, ValueError):
                    logger.warning("Case file cleanup pending for %s", relative, exc_info=True)
                    continue
                with self.connect() as db:
                    db.execute("DELETE FROM pending_deletions WHERE path=?", (row["path"],))
            with self.connect() as db:
                if case_id is None:
                    return db.execute("SELECT COUNT(*) FROM pending_deletions").fetchone()[0]
                return db.execute("SELECT COUNT(*) FROM pending_deletions WHERE case_id=?", (case_id,)).fetchone()[0]

    def register(self, evidence_id, case_id, name, size, path, kind, sector_size):
        with self.connect() as db:
            db.execute("""INSERT INTO evidence
                (id,case_id,name,size,source_path,kind,sector_size,imported_at) VALUES(?,?,?,?,?,?,?,?)""",
                       (evidence_id, case_id, name, size, str(path), kind, sector_size, now()))
            db.execute("INSERT INTO jobs(id,evidence_id,status,stage,created_at) VALUES(?,?,'queued','Queued',?)",
                       (str(uuid4()), evidence_id, now()))
            self.event(db, case_id, "evidence_imported", f"{name} ({size} bytes), mode={kind}", evidence_id)

    @staticmethod
    def decode(row):
        item = dict(row)
        for key in ("metadata", "warnings", "details", "coverage", "settings"):
            if key in item and item[key] is not None:
                item[key] = json.loads(item[key])
        if "legacy" in item:
            item["legacy"] = bool(item["legacy"])
        item.pop("source_path", None)
        return item

    def evidence(self, case_id):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute("""
                SELECT e.*,e.result_run_id AS run_id,j.id AS job_id,j.status,j.stage,j.progress,
                  j.started_at,j.finished_at,j.error,j.active_run_id,j.status AS current_job_status,
                  (SELECT COUNT(*) FROM artifacts a WHERE a.evidence_id=e.id AND a.run_id=e.result_run_id) AS artifact_count,
                  (SELECT COUNT(*) FROM analysis_runs r WHERE r.evidence_id=e.id) AS run_count
                FROM evidence e JOIN jobs j ON j.evidence_id=e.id
                WHERE e.case_id=? ORDER BY e.imported_at DESC
            """, (case_id,))]

    def detail(self, evidence_id, run_id=None):
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("""SELECT e.*,e.result_run_id AS run_id,j.status,j.stage,j.progress,j.error,
                j.started_at,j.finished_at,j.active_run_id,j.status AS current_job_status,
                (SELECT COUNT(*) FROM analysis_runs r WHERE r.evidence_id=e.id) AS run_count
                FROM evidence e JOIN jobs j ON j.evidence_id=e.id WHERE e.id=?""", (evidence_id,)).fetchone()
            if not row:
                return None
            item = self.decode(row)
            selected = run_id or item["result_run_id"]
            run = db.execute("SELECT * FROM analysis_runs WHERE evidence_id=? AND id=?",
                             (evidence_id, selected)).fetchone()
            if run_id and run is None:
                return None
            item["run"] = self.decode(run) if run else None
            if run_id:
                for key in ("status", "stage", "progress", "error", "started_at", "finished_at",
                            "sha256", "metadata", "warnings", "coverage"):
                    item[key] = item["run"][key]
                item["run_id"] = run_id
            item["partitions"] = [dict(p) for p in db.execute(
                "SELECT * FROM partitions WHERE evidence_id=? AND run_id=? ORDER BY start_sector", (evidence_id, selected))]
            item["artifact_count"] = db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE evidence_id=? AND run_id=?", (evidence_id, selected)).fetchone()[0]
            return item

    def runs(self, evidence_id):
        with self.connect() as db:
            rows = db.execute("""SELECT r.*,
                (SELECT COUNT(*) FROM artifacts a WHERE a.evidence_id=r.evidence_id AND a.run_id=r.id) AS artifact_count
                FROM analysis_runs r WHERE evidence_id=? ORDER BY sequence DESC""", (evidence_id,)).fetchall()
            items = []
            for row in rows:
                item = self.decode(row)
                item["coverage_status"] = (item.pop("coverage") or {}).get("status", "unknown")
                metadata = item.pop("metadata")
                item["parser_version"] = metadata.get("parser_version")
                item["tool_version"] = metadata.get("sleuthkit_version")
                item.pop("warnings")
                items.append(item)
            return items

    def artifacts(self, evidence_id, query, offset, limit, run_id=None):
        # Treat user searches literally, including SQL LIKE wildcard characters.
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.connect() as db:
            db.execute("BEGIN")
            evidence = db.execute("SELECT result_run_id FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            selected = run_id or (evidence["result_run_id"] if evidence else None)
            where = "evidence_id=? AND run_id=? AND path LIKE ? ESCAPE '\\'"
            params = (evidence_id, selected, pattern)
            count = db.execute(f"SELECT COUNT(*) FROM artifacts WHERE {where}", params).fetchone()[0]
            rows = db.execute(f"SELECT * FROM artifacts WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
                              (*params, limit, offset))
            return {"run_id": selected, "total": count, "items": [self.decode(row) for row in rows]}

    def artifact(self, evidence_id, artifact_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE evidence_id=? AND id=?", (evidence_id, artifact_id)).fetchone()
            return self.decode(row) if row else None

    def export_rows(self, case_id, run_id=None):
        """Export the latest results per source, or one selected historical run."""
        with self.connect() as db:
            db.execute("BEGIN")
            case = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if case is None:
                return None
            selection = "r.id=?" if run_id else "r.id=e.result_run_id"
            params = (run_id, case_id) if run_id else (case_id,)
            sources = db.execute(f"""SELECT e.id,e.name,e.kind,e.imported_at,e.sha256 AS recorded_sha256,
                    j.status AS current_job_status,r.id AS run_id,r.sequence,r.status AS run_status,
                    r.started_at,r.finished_at,r.sha256,r.metadata,r.warnings,r.coverage,r.settings,r.error,r.legacy
                FROM evidence e JOIN jobs j ON j.evidence_id=e.id
                LEFT JOIN analysis_runs r ON r.evidence_id=e.id AND {selection}
                WHERE e.case_id=? {"AND r.id IS NOT NULL" if run_id else ""}
                ORDER BY e.imported_at DESC""", params).fetchall()
            if run_id and not sources:
                return None
            evidence = []
            items = []
            for source in sources:
                entry = self.decode(source)
                entry["job_status"] = entry["run_status"] or entry["current_job_status"]
                if entry["run_id"] is None:
                    entry["sha256"] = entry["recorded_sha256"]
                entry["partitions"] = [dict(row) for row in db.execute(
                    "SELECT * FROM partitions WHERE evidence_id=? AND run_id=? ORDER BY start_sector",
                    (entry["id"], entry["run_id"]))]
                evidence.append(entry)
                coverage = entry["coverage"] or {}
                for row in db.execute("SELECT * FROM artifacts WHERE evidence_id=? AND run_id=? ORDER BY id",
                                      (entry["id"], entry["run_id"])):
                    details = json.loads(row["details"])
                    items.append({
                        "evidence_id": entry["id"], "run_id": entry["run_id"], "run_number": entry["sequence"],
                        "artifact_id": row["id"], "job_status": entry["job_status"],
                        "current_job_status": entry["current_job_status"],
                        "coverage_status": coverage.get("status", "unknown"),
                        "coverage_run_id": coverage.get("run_id"),
                        "source_name": entry["name"], "source_kind": entry["kind"],
                        "source_sha256": entry["sha256"], "imported_at": entry["imported_at"],
                        "artifact_path": row["path"], "artifact_kind": row["kind"],
                        "size": row["size"], "deleted": bool(row["deleted"]),
                        "partition_offset": row["partition_offset"], "metadata_address": row["metadata_address"],
                        "artifact_sha256": details.get("sha256"), "parser": details.get("parser"), "details": details,
                    })
            return {"case": dict(case), "selection": "selected_run" if run_id else "latest_saved_results",
                    "items": items, "evidence": evidence}

    def timeline_rows(self, case_id, start=None, end=None):
        """Chronological filesystem timestamps across a case's latest results.

        Emits one event per non-zero MAC time (accessed, modified, metadata
        changed, created) on each artifact in each source's result run. Zero
        means unavailable, so it is skipped. Sources with no filesystem
        timestamps still appear, anchored to when they were imported.
        """
        labels = {"accessed": "Accessed", "modified": "Modified",
                  "metadata_changed": "Metadata changed", "created": "Created"}
        with self.connect() as db:
            db.execute("BEGIN")
            case = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if case is None:
                return None
            sources = db.execute("""SELECT id,name,kind,imported_at,result_run_id
                FROM evidence WHERE case_id=? ORDER BY imported_at DESC""", (case_id,)).fetchall()
            events = []
            for source in sources:
                run_id = source["result_run_id"]
                found = False
                if run_id:
                    for row in db.execute("""SELECT id,path,kind,deleted,details FROM artifacts
                        WHERE evidence_id=? AND run_id=? ORDER BY id""", (source["id"], run_id)):
                        stamps = json.loads(row["details"]).get("timestamps_unix") or {}
                        for key, label in labels.items():
                            unix = stamps.get(key)
                            if not unix:  # zero or missing means the timestamp is unavailable
                                continue
                            try:
                                when = datetime.fromtimestamp(unix, timezone.utc).isoformat()
                            except (OverflowError, OSError, ValueError):
                                continue
                            found = True
                            events.append({"at": when, "timestamp_kind": key, "timestamp_label": label,
                                "origin": "filesystem", "source_id": source["id"], "source_name": source["name"],
                                "artifact_id": row["id"], "artifact_path": row["path"],
                                "artifact_kind": row["kind"], "deleted": bool(row["deleted"])})
                if not found:
                    # Logical files and un-analyzed sources still belong on the timeline.
                    events.append({"at": source["imported_at"], "timestamp_kind": "imported",
                        "timestamp_label": "Imported", "origin": "import", "source_id": source["id"],
                        "source_name": source["name"], "artifact_id": None,
                        "artifact_path": source["name"], "artifact_kind": source["kind"], "deleted": False})
            if start:
                events = [event for event in events if event["at"] >= start]
            if end:
                events = [event for event in events if event["at"] <= end]
            events.sort(key=lambda event: event["at"])
            return {"case": dict(case), "total": len(events), "events": events}

    def audit(self, case_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM audit WHERE case_id=? ORDER BY id DESC LIMIT 200", (case_id,))]

    def recover_interrupted(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("""SELECT j.*,e.id AS evidence_id,e.case_id FROM jobs j
                JOIN evidence e ON e.id=j.evidence_id WHERE j.status='running'""").fetchall():
                run_id = row["active_run_id"]
                if not run_id:
                    run_id = str(uuid4())
                    sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM analysis_runs WHERE evidence_id=?",
                                          (row["evidence_id"],)).fetchone()[0]
                    db.execute("""INSERT INTO analysis_runs
                        (id,evidence_id,sequence,status,stage,progress,started_at,legacy)
                        VALUES(?,?,?,'running',?,?,?,1)""",
                        (run_id, row["evidence_id"], sequence, row["stage"], row["progress"], row["started_at"]))
                db.execute("""UPDATE analysis_runs SET status='interrupted',stage='Interrupted',finished_at=?,
                    error='Analysis stopped before results were saved; a new attempt was queued.'
                    WHERE id=? AND status='running'""", (now(), run_id))
                db.execute("""UPDATE jobs SET status='queued',stage='Requeued after restart',progress=0,
                    active_run_id=NULL,started_at=NULL,finished_at=NULL,error=NULL WHERE id=?""", (row["id"],))
                self.event(db, row["case_id"], "analysis_requeued", f"Run {run_id} was interrupted", row["evidence_id"])

    def claim(self, settings=None, parser_version=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT j.id AS job_id,e.* FROM jobs j JOIN evidence e ON e.id=j.evidence_id
                WHERE j.status='queued' ORDER BY j.created_at LIMIT 1""").fetchone()
            if row is None:
                return None
            run_id, started = str(uuid4()), now()
            sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM analysis_runs WHERE evidence_id=?",
                                  (row["id"],)).fetchone()[0]
            run_settings = {**(settings or {}), "source_kind": row["kind"], "sector_size": row["sector_size"]}
            metadata = {"parser_version": parser_version} if parser_version else {}
            db.execute("""INSERT INTO analysis_runs(id,evidence_id,sequence,status,stage,started_at,settings,metadata)
                VALUES(?,?,?,'running','Starting',?,?,?)""",
                (run_id, row["id"], sequence, started, json.dumps(run_settings), json.dumps(metadata)))
            db.execute("""UPDATE jobs SET status='running',stage='Starting',progress=0,started_at=?,
                finished_at=NULL,error=NULL,active_run_id=? WHERE id=?""", (started, run_id, row["job_id"]))
            self.event(db, row["case_id"], "analysis_started", f"{row['name']}: run {sequence} ({run_id}) dispatched", row["id"])
            return {**dict(row), "run_id": run_id}

    def progress(self, job_id, stage, progress):
        with self.connect() as db:
            db.execute("UPDATE jobs SET stage=?,progress=? WHERE id=? AND status='running'", (stage, progress, job_id))
            db.execute("""UPDATE analysis_runs SET stage=?,progress=? WHERE status='running' AND id=(
                SELECT active_run_id FROM jobs WHERE id=? AND status='running')""", (stage, progress, job_id))

    def finish(self, job, result):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            evidence_id, run_id = job["id"], job["run_id"]
            current = db.execute("""SELECT r.* FROM jobs j JOIN analysis_runs r ON r.id=j.active_run_id
                WHERE j.id=? AND j.status='running' AND r.id=? AND r.status='running'""",
                (job["job_id"], run_id)).fetchone()
            if current is None:
                return False  # Ignore duplicate or late results from an interrupted attempt.
            coverage = result.get("coverage") or {
                "schema_version": 1, "run_id": run_id, "status": "unknown",
                "started_at": current["started_at"], "finished_at": now(), "steps": [], "limits": {},
                "scope": "No coverage measurements were returned for this analysis.",
            }
            coverage = {**coverage, "run_id": run_id}
            digest = result.get("sha256", job.get("sha256"))
            metadata = json.dumps(result.get("metadata", json.loads(current["metadata"])))
            warnings = json.dumps(result.get("warnings", []))
            saved_coverage = json.dumps(coverage)
            error, finished = result.get("error"), now()
            status, stage = ("failed", "Failed") if error else ("completed", "Complete")
            settings = json.dumps({**json.loads(current["settings"]), **coverage.get("limits", {})})
            db.execute("""UPDATE analysis_runs SET status=?,stage=?,progress=100,finished_at=?,sha256=?,
                metadata=?,warnings=?,coverage=?,settings=?,error=? WHERE id=?""",
                (status, stage, finished, digest, metadata, warnings, saved_coverage, settings, error, run_id))
            db.execute("UPDATE evidence SET sha256=?,metadata=?,warnings=?,coverage=?,result_run_id=? WHERE id=?",
                       (digest, metadata, warnings, saved_coverage, run_id, evidence_id))
            for item in result.get("artifacts", []):
                db.execute("""INSERT INTO artifacts(evidence_id,run_id,path,kind,size,deleted,partition_offset,metadata_address,details)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (evidence_id, run_id, item["path"], item["kind"], item.get("size"),
                    int(item.get("deleted", False)), item.get("partition_offset"), item.get("metadata_address"), json.dumps(item.get("details", {}))))
            for p in result.get("partitions", []):
                db.execute("""INSERT INTO partitions(evidence_id,run_id,slot,start_sector,length_sectors,sector_size,description)
                    VALUES(?,?,?,?,?,?,?)""", (evidence_id, run_id, p["slot"], p["start_sector"], p["length_sectors"], p["sector_size"], p["description"]))
            db.execute("""UPDATE jobs SET status=?,stage=?,progress=100,finished_at=?,error=?,active_run_id=NULL
                WHERE id=?""", (status, stage, finished, error, job["job_id"]))
            self.event(db, job["case_id"], "analysis_failed" if error else "analysis_completed",
                       f"{job['name']}: run {current['sequence']} ({run_id}): " +
                       (error or f"{len(result.get('artifacts', []))} artifacts indexed"), evidence_id)
            return True

    def retry(self, evidence_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if not row:
                return False
            changed = db.execute("""UPDATE jobs SET status='queued',stage='Queued',progress=0,error=NULL,
                started_at=NULL,finished_at=NULL WHERE evidence_id=? AND status IN ('failed','completed')""", (evidence_id,)).rowcount
            if changed:
                self.event(db, row["case_id"], "analysis_retried", "Manual retry requested", evidence_id)
            return bool(changed)
