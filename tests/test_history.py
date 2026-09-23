import csv
import hashlib
import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cds.analysis import PARSER_VERSION, analyze
from cds.config import Settings
from cds.main import create_app
from cds.store import CaseBusyError, Store
from scripts.make_demo import partitioned_image

HEADERS = {"X-CDS-Request": "local-ui"}
LIMITS = {"max_artifacts": 20, "tool_timeout": 10}


def register(store, case_id, eid="source", content=b"sample", name="sample.txt", kind="file"):
    path = store.root / "evidence" / eid
    path.write_bytes(content)
    store.register(eid, case_id, name, len(content), path, kind, 512)
    return path


def complete(store, limits=None):
    limits = limits or LIMITS
    job = store.claim(limits, PARSER_VERSION)
    result = analyze(job, limits, str(store.root))
    assert store.finish(job, result)
    return job, result


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    case = store.create_case("History", "")
    register(store, case["id"])
    return store, case


def test_successful_runs_keep_distinct_artifacts_and_saved_settings(workspace):
    store, case = workspace
    first, _ = complete(store)
    before = store.detail("source", first["run_id"])["run"]
    artifacts = store.artifacts("source", "", 0, 50)["items"]
    assert store.retry("source")
    second, _ = complete(store, {"max_artifacts": 40, "tool_timeout": 15})
    assert first["run_id"] != second["run_id"]
    assert store.detail("source", first["run_id"])["run"] == before
    assert store.artifacts("source", "", 0, 50, first["run_id"])["items"] == artifacts
    assert store.artifact("source", artifacts[0]["id"]) == artifacts[0]
    assert store.artifacts("source", "", 0, 50)["items"][0]["id"] != artifacts[0]["id"]
    assert store.evidence(case["id"])[0]["artifact_count"] == 1
    assert store.evidence(case["id"])[0]["run_count"] == 2
    assert store.runs("source")[0]["settings"]["max_artifacts"] == 40
    assert store.runs("source")[1]["settings"]["max_artifacts"] == 20
    assert store.runs("source")[1]["parser_version"] == PARSER_VERSION
    store.initialize()
    assert store.detail("source", first["run_id"])["run"] == before
    assert len(store.runs("source")) == 2


def test_failed_reanalysis_keeps_previous_results_and_integrity(workspace):
    store, _ = workspace
    first, _ = complete(store)
    snapshot = store.detail("source", first["run_id"])["run"]
    previous = store.artifacts("source", "", 0, 50, first["run_id"])
    (store.root / "evidence" / "source").write_bytes(b"change")
    assert store.retry("source")
    second, result = complete(store)
    assert "hash changed" in result["error"]
    assert store.detail("source")["status"] == "failed"
    assert store.detail("source")["run_id"] == second["run_id"]
    assert store.detail("source")["sha256"] == snapshot["sha256"]
    assert store.artifacts("source", "", 0, 50)["total"] == 0
    assert store.detail("source", first["run_id"])["run"] == snapshot
    assert store.artifacts("source", "", 0, 50, first["run_id"]) == previous


def test_interrupted_attempts_and_late_results_cannot_replace_saved_runs(workspace):
    store, _ = workspace
    first, _ = complete(store)
    store.retry("source")
    interrupted = store.claim(LIMITS, PARSER_VERSION)
    store.progress(interrupted["job_id"], "Computing SHA-256", 37)
    store.recover_interrupted()
    history = store.runs("source")
    assert history[0]["status"] == "interrupted"
    assert history[0]["progress"] == 37
    assert history[0]["coverage_status"] == "unknown"
    assert history[0]["started_at"] and history[0]["finished_at"]
    assert history[0]["settings"]["tool_timeout"] == 10
    assert history[0]["parser_version"] == PARSER_VERSION
    assert store.detail("source")["run_id"] == first["run_id"]
    assert store.detail("source")["status"] == "queued"
    resumed = store.claim(LIMITS, PARSER_VERSION)
    late = analyze(interrupted, LIMITS, str(store.root))
    assert not store.finish(interrupted, late)
    assert store.detail("source")["active_run_id"] == resumed["run_id"]
    assert store.finish(resumed, analyze(resumed, LIMITS, str(store.root)))
    assert [run["sequence"] for run in store.runs("source")] == [3, 2, 1]
    assert [run["status"] for run in store.runs("source")] == ["completed", "interrupted", "completed"]
    assert not store.finish(resumed, late)
    assert store.artifacts("source", "", 0, 50)["total"] == 1


def test_run_results_publish_atomically(workspace):
    store, _ = workspace
    first, _ = complete(store)
    store.retry("source")
    job = store.claim(LIMITS)
    result = analyze(job, LIMITS, str(store.root))
    result["artifacts"].append({"path": "invalid artifact without kind"})
    with pytest.raises(KeyError):
        store.finish(job, result)
    assert store.detail("source")["run_id"] == first["run_id"]
    assert store.detail("source")["status"] == "running"
    assert store.artifacts("source", "", 0, 50, job["run_id"])["total"] == 0
    result["artifacts"].pop()
    assert store.finish(job, result)


def old_workspace(tmp_path, status="completed", coverage=True, results=True):
    """The pre-history schema, including existing artifact IDs and cached results."""
    tmp_path.mkdir(exist_ok=True)
    db = sqlite3.connect(tmp_path / "cds.sqlite3")
    db.executescript("""
        CREATE TABLE cases(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE evidence(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),name TEXT NOT NULL,
            size INTEGER NOT NULL,source_path TEXT NOT NULL,kind TEXT NOT NULL,sector_size INTEGER NOT NULL,
            imported_at TEXT NOT NULL,sha256 TEXT,metadata TEXT NOT NULL DEFAULT '{}',warnings TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE jobs(id TEXT PRIMARY KEY,evidence_id TEXT UNIQUE NOT NULL REFERENCES evidence(id),
            status TEXT NOT NULL,stage TEXT NOT NULL,progress INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
            started_at TEXT,finished_at TEXT,error TEXT);
        CREATE TABLE artifacts(id INTEGER PRIMARY KEY,evidence_id TEXT NOT NULL REFERENCES evidence(id),
            path TEXT NOT NULL,kind TEXT NOT NULL,size INTEGER,deleted INTEGER NOT NULL,partition_offset INTEGER,
            metadata_address TEXT,details TEXT NOT NULL);
        CREATE TABLE partitions(id INTEGER PRIMARY KEY,evidence_id TEXT NOT NULL REFERENCES evidence(id),
            slot TEXT NOT NULL,start_sector INTEGER NOT NULL,length_sectors INTEGER NOT NULL,sector_size INTEGER NOT NULL,
            description TEXT NOT NULL);
    """)
    path = tmp_path / "original.txt"
    path.write_bytes(b"sample")
    db.execute("INSERT INTO cases VALUES('case','Old case','','2026-01-01')")
    db.execute("INSERT INTO evidence VALUES('source','case','original.txt',6,?,'file',512,'2026-01-01',?,?,?)",
               (str(path), hashlib.sha256(b"sample").hexdigest() if results else None,
                '{"parser_version":"previous-version"}' if results else '{}', '["previous warning"]' if results else '[]'))
    db.execute("INSERT INTO jobs VALUES('job','source',?,'Previous stage',100,'2026-01-01','2026-02-01','2026-02-02',?)",
               (status, "Previous failure" if status == "failed" else None))
    if results:
        db.execute("INSERT INTO artifacts VALUES(101,'source','/original','file',6,0,2048,'1','{}')")
        db.execute("INSERT INTO partitions VALUES(42,'source','0',2048,100,512,'Original partition')")
    if coverage:
        db.execute("ALTER TABLE evidence ADD COLUMN coverage TEXT")
        if results:
            db.execute("UPDATE evidence SET coverage=?", (json.dumps({
                "schema_version": 1, "run_id": "prior-coverage-run", "status": "partial",
                "started_at": "2026-01-10", "finished_at": "2026-01-11", "steps": [],
                "scope": "Original limited scope", "limits": {"max_artifacts": 2}}),))
    db.commit()
    db.close()
    return Store(tmp_path)


@pytest.mark.parametrize("status", ["completed", "failed", "queued", "running"])
@pytest.mark.parametrize("coverage", [True, False])
def test_migration_preserves_saved_results_without_inventing_old_attempts(tmp_path, status, coverage):
    store = old_workspace(tmp_path, status, coverage)
    store.initialize()
    saved = store.runs("source")
    assert len(saved) == 1
    first = saved[0]
    assert first["legacy"]
    assert first["status"] == (status if status in {"completed", "failed"} else "unknown")
    assert first["settings"] == ({"max_artifacts": 2} if coverage else {})
    assert first["coverage_status"] == ("partial" if coverage else "unknown")
    if status in {"queued", "running"}:
        assert first["started_at"] == ("2026-01-10" if coverage else None)
        assert first["finished_at"] == ("2026-01-11" if coverage else None)
    detail = store.detail("source", first["id"])
    assert detail["metadata"]["parser_version"] == "previous-version"
    assert detail["warnings"] == ["previous warning"]
    assert detail["partitions"][0]["id"] == 42
    assert store.artifact("source", 101)["run_id"] == first["id"]
    store.initialize()
    assert store.runs("source") == saved
    with store.connect() as db:
        assert list(db.execute("PRAGMA foreign_key_check")) == []
    if status == "running":
        store.recover_interrupted()
        assert len(store.runs("source")) == 2
        assert store.runs("source")[0]["status"] == "interrupted"
        assert store.runs("source")[1] == first


@pytest.mark.parametrize("status", ["queued", "running"])
def test_migration_does_not_create_results_for_never_completed_jobs(tmp_path, status):
    store = old_workspace(tmp_path, status, coverage=False, results=False)
    store.initialize()
    assert store.runs("source") == []
    assert store.detail("source")["run_id"] is None
    store.recover_interrupted()
    assert len(store.runs("source")) == (1 if status == "running" else 0)


def test_migration_failure_rolls_back_and_can_be_retried(tmp_path, monkeypatch):
    import cds.migrations as migrations
    store = old_workspace(tmp_path, coverage=False)
    original = migrations.add_column

    def fail(db, table, name, declaration):
        original(db, table, name, declaration)
        if name == "active_run_id":
            raise RuntimeError("Simulated upgrade failure")

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "add_column", fail)
        with pytest.raises(RuntimeError, match="Simulated"):
            store.initialize()
    with store.connect() as db:
        assert "result_run_id" not in {row["name"] for row in db.execute("PRAGMA table_info(evidence)")}
        assert db.execute("SELECT id FROM artifacts").fetchone()[0] == 101
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='analysis_runs'").fetchall()
    store.initialize()
    assert len(store.runs("source")) == 1


def test_history_api_scoping_and_exports(tmp_path):
    app = create_app(Settings(tmp_path), start_workers=False)
    with TestClient(app) as client:
        store = app.state.store
        first_case = store.create_case("One", "")
        other_case = store.create_case("Two", "")
        register(store, first_case["id"])
        first, _ = complete(store)
        original = store.artifacts("source", "", 0, 50)["items"][0]
        store.retry("source")
        second, _ = complete(store)
        register(store, first_case["id"], "sibling")
        sibling, _ = complete(store)
        register(store, other_case["id"], "other")
        other, _ = complete(store)
        route = "/api/evidence/source"
        runs = client.get(route + "/runs").json()
        assert [run["id"] for run in runs] == [second["run_id"], first["run_id"]]
        detail = client.get(route, params={"run_id": first["run_id"]}).json()
        assert detail["run_id"] == detail["coverage"]["run_id"] == first["run_id"]
        assert client.get(route + "/artifacts", params={"run_id": first["run_id"]}).json()["items"] == [original]
        assert client.get(route + f"/artifacts/{original['id']}").json() == original
        assert client.get(f"/api/evidence/other/artifacts/{original['id']}").status_code == 404
        for run_id in [other["run_id"], sibling["run_id"], "missing"]:
            assert client.get(route, params={"run_id": run_id}).status_code == 404
            assert client.get(route + "/artifacts", params={"run_id": run_id}).status_code == 404
        assert client.get("/api/evidence/missing/runs").status_code == 404
        assert client.get(route, params={"run_id": ""}).status_code == 422
        export = f"/api/cases/{first_case['id']}/export"
        selected = client.get(export, params={"run_id": first["run_id"], "format": "json"})
        assert selected.status_code == 200
        assert first["run_id"] in selected.headers["content-disposition"]
        payload = selected.json()
        assert payload["selection"] == "selected_run"
        assert len(payload["evidence"]) == len(payload["items"]) == 1
        assert payload["items"][0]["artifact_id"] == original["id"]
        assert payload["evidence"][0]["run_id"] == first["run_id"]
        rows = list(csv.DictReader(io.StringIO(client.get(export, params={"run_id": first["run_id"]}).text)))
        assert rows[0]["run_id"] == first["run_id"]
        assert rows[0]["artifact_id"] == str(original["id"])
        latest = client.get(export, params={"format": "json"}).json()
        assert {item["run_id"] for item in latest["items"]} == {second["run_id"], sibling["run_id"]}
        assert client.get(export, params={"run_id": other["run_id"]}).status_code == 404
        store.retry("source")
        while_queued = client.get(export, params={"run_id": first["run_id"], "format": "json"}).json()
        assert while_queued["evidence"][0]["job_status"] == "completed"
        assert while_queued["evidence"][0]["current_job_status"] == "queued"
        store.claim(LIMITS)
        store.recover_interrupted()
        interrupted = client.get(export, params={"run_id": store.runs("source")[0]["id"], "format": "json"}).json()
        assert interrupted["items"] == []
        assert interrupted["evidence"][0]["run_status"] == "interrupted"
        assert interrupted["evidence"][0]["sha256"] is None
        assert interrupted["evidence"][0]["coverage"] is None


def test_case_deletion_removes_all_its_history_and_preserves_other_cases(workspace):
    store, first_case = workspace
    complete(store)
    store.retry("source")
    with pytest.raises(CaseBusyError):
        store.delete_case(first_case["id"])
    complete(store)
    other = store.create_case("Keep", "")
    register(store, other["id"], "other")
    complete(store)
    survivor = store.runs("other")
    folders = []
    for source in ("source", "other"):
        for run in store.runs(source):
            folder = store.root / "work" / run["id"]
            folder.mkdir()
            (folder / "progress.json").write_text('{"progress":100}')
            folders.append((source, folder))
    assert store.delete_case(first_case["id"])
    store.cleanup_deleted_files()
    assert store.runs("source") == []
    assert store.runs("other") == survivor
    assert all(folder.exists() == (source == "other") for source, folder in folders)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts WHERE evidence_id='source'").fetchone()[0] == 0
        assert list(db.execute("PRAGMA foreign_key_check")) == []


def test_real_image_rerun_preserves_limited_inventory(tmp_path):
    import shutil
    if not shutil.which("fls") or not shutil.which("mmls"):
        pytest.skip("Requires Sleuth Kit")
    store = Store(tmp_path)
    store.initialize()
    case = store.create_case("Image history", "")
    register(store, case["id"], content=partitioned_image(), name="disk.img", kind="raw_image")
    limited, _ = complete(store, {"max_artifacts": 2, "tool_timeout": 10})
    snapshot = store.detail("source", limited["run_id"])
    assert snapshot["coverage"]["status"] == "partial"
    assert snapshot["artifact_count"] == 2
    assert snapshot["partitions"]
    store.retry("source")
    expanded, _ = complete(store)
    current = store.detail("source")
    assert current["coverage"]["status"] == "complete"
    assert current["artifact_count"] > 2
    previous = store.detail("source", limited["run_id"])
    assert previous["coverage"] == snapshot["coverage"]
    assert previous["partitions"] == snapshot["partitions"]
    assert previous["artifact_count"] == 2
    exported = store.export_rows(case["id"], limited["run_id"])
    assert len(exported["items"]) == 2
    assert exported["evidence"][0]["partitions"] == snapshot["partitions"]
    assert expanded["run_id"] != limited["run_id"]
