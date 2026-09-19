import json
import logging
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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
            paths = [f"evidence/{row['id']}" for row in sources] + [f"work/{job['id']}" for job in jobs]
            db.executemany("INSERT INTO pending_deletions(path,case_id) VALUES(?,?)",
                           [(path, case_id) for path in paths])
            for table in ("artifacts", "partitions", "jobs"):
                db.execute(f"DELETE FROM {table} WHERE evidence_id IN (SELECT id FROM evidence WHERE case_id=?)",
                           (case_id,))
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
        for key in ("metadata", "warnings", "details"):
            if key in item:
                item[key] = json.loads(item[key])
        item.pop("source_path", None)
        return item

    def evidence(self, case_id):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute("""
                SELECT e.*,j.id AS job_id,j.status,j.stage,j.progress,j.started_at,j.finished_at,j.error,
                  (SELECT COUNT(*) FROM artifacts a WHERE a.evidence_id=e.id) AS artifact_count
                FROM evidence e JOIN jobs j ON j.evidence_id=e.id
                WHERE e.case_id=? ORDER BY e.imported_at DESC
            """, (case_id,))]

    def detail(self, evidence_id):
        with self.connect() as db:
            row = db.execute("""SELECT e.*,j.status,j.stage,j.progress,j.error,j.started_at,j.finished_at
                FROM evidence e JOIN jobs j ON j.evidence_id=e.id WHERE e.id=?""", (evidence_id,)).fetchone()
            if not row:
                return None
            item = self.decode(row)
            item["partitions"] = [dict(p) for p in db.execute(
                "SELECT * FROM partitions WHERE evidence_id=? ORDER BY start_sector", (evidence_id,))]
            return item

    def artifacts(self, evidence_id, query, offset, limit):
        # Treat user searches literally, including SQL LIKE wildcard characters.
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = "evidence_id=? AND path LIKE ? ESCAPE '\\'"
        with self.connect() as db:
            count = db.execute(f"SELECT COUNT(*) FROM artifacts WHERE {where}", (evidence_id, pattern)).fetchone()[0]
            rows = db.execute(f"SELECT * FROM artifacts WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
                              (evidence_id, pattern, limit, offset))
            return {"total": count, "items": [self.decode(row) for row in rows]}

    def audit(self, case_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM audit WHERE case_id=? ORDER BY id DESC LIMIT 200", (case_id,))]

    def recover_interrupted(self):
        with self.connect() as db:
            for row in db.execute("""SELECT j.id,e.id AS evidence_id,e.case_id FROM jobs j
                JOIN evidence e ON e.id=j.evidence_id WHERE j.status='running'""").fetchall():
                db.execute("UPDATE jobs SET status='queued',stage='Requeued after restart',progress=0 WHERE id=?", (row["id"],))
                self.event(db, row["case_id"], "analysis_requeued", "Previous run was interrupted", row["evidence_id"])

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT j.id AS job_id,e.* FROM jobs j JOIN evidence e ON e.id=j.evidence_id
                WHERE j.status='queued' ORDER BY j.created_at LIMIT 1""").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',stage='Starting',started_at=?,finished_at=NULL,error=NULL WHERE id=?",
                       (now(), row["job_id"]))
            self.event(db, row["case_id"], "analysis_started", f"{row['name']}: worker dispatched", row["id"])
            return dict(row)

    def progress(self, job_id, stage, progress):
        with self.connect() as db:
            db.execute("UPDATE jobs SET stage=?,progress=? WHERE id=? AND status='running'",
                       (stage, progress, job_id))

    def finish(self, job, result):
        with self.connect() as db:
            evidence_id = job["id"]
            db.execute("UPDATE evidence SET sha256=?,metadata=?,warnings=? WHERE id=?",
                       (result.get("sha256"), json.dumps(result.get("metadata", {})), json.dumps(result.get("warnings", [])), evidence_id))
            db.execute("DELETE FROM artifacts WHERE evidence_id=?", (evidence_id,))
            db.execute("DELETE FROM partitions WHERE evidence_id=?", (evidence_id,))
            for item in result.get("artifacts", []):
                db.execute("""INSERT INTO artifacts(evidence_id,path,kind,size,deleted,partition_offset,metadata_address,details)
                    VALUES(?,?,?,?,?,?,?,?)""", (evidence_id, item["path"], item["kind"], item.get("size"),
                    int(item.get("deleted", False)), item.get("partition_offset"), item.get("metadata_address"), json.dumps(item.get("details", {}))))
            for p in result.get("partitions", []):
                db.execute("""INSERT INTO partitions(evidence_id,slot,start_sector,length_sectors,sector_size,description)
                    VALUES(?,?,?,?,?,?)""", (evidence_id, p["slot"], p["start_sector"], p["length_sectors"], p["sector_size"], p["description"]))
            error = result.get("error")
            db.execute("UPDATE jobs SET status=?,stage=?,progress=?,finished_at=?,error=? WHERE id=?",
                       ("failed" if error else "completed", "Failed" if error else "Complete", 100, now(), error, job["job_id"]))
            self.event(db, job["case_id"], "analysis_failed" if error else "analysis_completed",
                       f"{job['name']}: " + (error or f"{len(result.get('artifacts', []))} artifacts indexed"), evidence_id)

    def retry(self, evidence_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if not row:
                return False
            changed = db.execute("""UPDATE jobs SET status='queued',stage='Queued',progress=0,error=NULL,
                started_at=NULL,finished_at=NULL WHERE evidence_id=? AND status='failed'""", (evidence_id,)).rowcount
            if changed:
                self.event(db, row["case_id"], "analysis_retried", "Manual retry requested", evidence_id)
            return bool(changed)
