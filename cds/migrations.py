"""Add saved runs without discarding results from older workspaces."""

import json
from uuid import uuid4


def add_column(db, table, name, declaration):
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def migrate_history(db):
    # SQLite DDL participates in this transaction; a failed upgrade rolls back.
    db.execute("BEGIN IMMEDIATE")
    db.execute("""CREATE TABLE IF NOT EXISTS analysis_runs (
        id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL REFERENCES evidence(id),
        sequence INTEGER NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0, started_at TEXT, finished_at TEXT,
        sha256 TEXT, metadata TEXT NOT NULL DEFAULT '{}', warnings TEXT NOT NULL DEFAULT '[]',
        coverage TEXT, settings TEXT NOT NULL DEFAULT '{}', error TEXT,
        legacy INTEGER NOT NULL DEFAULT 0, UNIQUE(evidence_id,sequence)
    )""")
    add_column(db, "evidence", "coverage", "TEXT")
    add_column(db, "evidence", "result_run_id", "TEXT REFERENCES analysis_runs(id)")
    add_column(db, "jobs", "active_run_id", "TEXT REFERENCES analysis_runs(id)")
    for table in ("artifacts", "partitions"):
        add_column(db, table, "run_id", "TEXT REFERENCES analysis_runs(id)")
        db.execute(f"CREATE INDEX IF NOT EXISTS {table}_run ON {table}(evidence_id,run_id,id)")

    rows = db.execute("""SELECT e.*,j.status,j.stage,j.progress,j.started_at,j.finished_at,j.error
        FROM evidence e JOIN jobs j ON j.evidence_id=e.id
        WHERE NOT EXISTS (SELECT 1 FROM analysis_runs r WHERE r.evidence_id=e.id)
          AND (j.status IN ('completed','failed') OR e.sha256 IS NOT NULL OR e.coverage IS NOT NULL
               OR EXISTS (SELECT 1 FROM artifacts a WHERE a.evidence_id=e.id)
               OR EXISTS (SELECT 1 FROM partitions p WHERE p.evidence_id=e.id))""").fetchall()
    for row in rows:
        coverage = json.loads(row["coverage"]) if row["coverage"] else None
        run_id = (coverage or {}).get("run_id") or str(uuid4())
        if db.execute("SELECT 1 FROM analysis_runs WHERE id=?", (run_id,)).fetchone():
            run_id = str(uuid4())
        if coverage:
            coverage["run_id"] = run_id
        saved_coverage = json.dumps(coverage) if coverage is not None else None
        # A queued/running job may be a retry. Its timestamps and error do not
        # describe the saved results, so do not attribute them to that snapshot.
        terminal = row["status"] in {"completed", "failed"}
        status = row["status"] if terminal else "unknown"
        started = row["started_at"] if terminal else (coverage or {}).get("started_at")
        finished = row["finished_at"] if terminal else (coverage or {}).get("finished_at")
        db.execute("""INSERT INTO analysis_runs
            (id,evidence_id,sequence,status,stage,progress,started_at,finished_at,sha256,
             metadata,warnings,coverage,settings,error,legacy)
            VALUES(?,?,1,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (run_id, row["id"], status, row["stage"] if terminal else "Imported saved results",
             row["progress"] if terminal else 0, started, finished, row["sha256"],
             row["metadata"], row["warnings"], saved_coverage,
             json.dumps((coverage or {}).get("limits", {})), row["error"] if terminal else None))
        for table in ("artifacts", "partitions"):
            db.execute(f"UPDATE {table} SET run_id=? WHERE evidence_id=? AND run_id IS NULL", (run_id, row["id"]))
        db.execute("UPDATE evidence SET result_run_id=?,coverage=? WHERE id=?", (run_id, saved_coverage, row["id"]))
