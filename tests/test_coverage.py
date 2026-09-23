import csv
import io

import pytest
from fastapi.testclient import TestClient

from cds.analysis import ToolLimitError, analyze
from cds.config import Settings
from cds.main import create_app
from cds.store import Store, now

HEADERS = {"X-CDS-Request": "local-ui"}


@pytest.fixture
def inspect(tmp_path):
    def run(name="source.txt", content=b"example", kind="file", max_artifacts=20):
        path = tmp_path / name
        path.write_bytes(content)
        job = {"name": name, "source_path": str(path), "size": len(content),
               "kind": kind, "sector_size": 512, "imported_at": now()}
        return analyze(job, {"tool_timeout": 5, "max_artifacts": max_artifacts}, str(tmp_path))
    return run


def steps(result):
    return {step["id"]: step for step in result["coverage"]["steps"]}


@pytest.mark.parametrize("name,content,status,reason,processed", [
    ("empty.txt", b"", "complete", "text_metadata_read", 0),
    ("sample.log", b"a" * 70000, "partial", "sample_limit", 65536),
    ("bad.txt", b"\xff", "partial", "encoding_replaced", 1),
    ("file.bin", b"binary", "unsupported", "no_content_parser", 0),
    ("large.json", b" " * (4 * 1024**2 + 1), "skipped", "json_size_limit", 0),
    ("good.json", b'{"key":1}', "complete", "json_metadata_read", 9),
    ("bad.json", b"{broken", "failed", "parser_error", 0),
])
def test_file_coverage_is_independent_of_hash(inspect, name, content, status, reason, processed):
    result = inspect(name, content)
    measured = steps(result)
    assert measured["hash"]["status"] == "complete"
    assert measured["hash"]["processed"] == measured["hash"]["total"] == len(content)
    assert measured["content"]["status"] == status
    assert measured["content"]["reason"] == reason
    assert measured["content"]["processed"] == processed
    assert measured["content"]["total"] == len(content)
    assert result["coverage"]["status"] == (
        "failed" if status == "failed" else "complete" if status == "complete" else "partial")
    assert result["coverage"]["run_id"]
    assert result["coverage"]["finished_at"] >= result["coverage"]["started_at"]


def fake_image_tools(monkeypatch, listings, discovery=None):
    monkeypatch.setattr("cds.analysis.shutil.which", lambda _name: "/usr/bin/tool")
    if discovery is None:
        discovery = (0, "\n".join(f"{i:03}: 000:{i:03} {offset} {offset + 99} 100 Test partition"
                                 for i, offset in enumerate(listings)), "")

    def tool(args, _timeout):
        if args[1] == "-V":
            return 0, "The Sleuth Kit ver 4.12.1", ""
        if args[0] == "mmls":
            return discovery
        value = listings[int(args[args.index("-o") + 1])]
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr("cds.analysis.run_tool", tool)


def bodyfile(name):
    return f"0|/{name}|1|r/rrw-rw-rw-|0|0|4|0|0|0|0\n"


def test_limited_partition_does_not_hide_unexamined_partitions(inspect, monkeypatch):
    fake_image_tools(monkeypatch, {10: (0, bodyfile("a") + bodyfile("b"), ""),
                                  200: (0, bodyfile("c"), "")})
    result = inspect("disk.img", kind="raw_image", max_artifacts=2)
    measured = steps(result)
    assert "error" not in result
    assert result["coverage"]["status"] == "partial"
    assert measured["filesystem:10"]["status"] == "partial"
    assert measured["filesystem:10"]["processed"] == 1
    assert measured["filesystem:10"]["total"] is None
    assert measured["filesystem:200"]["status"] == "skipped"
    assert measured["filesystem:200"]["reason"] == "artifact_limit"


@pytest.mark.parametrize("failure,reason", [
    ((1, "", "Cannot determine filesystem type"), "filesystem_unreadable"),
    (ToolLimitError("tool_timeout", "fls exceeded timeout"), "tool_timeout"),
    (ToolLimitError("tool_output_limit", "Output too large"), "tool_output_limit"),
])
def test_unreadable_partition_does_not_stop_other_partitions(inspect, monkeypatch, failure, reason):
    fake_image_tools(monkeypatch, {10: failure, 200: (0, bodyfile("readable"), "")})
    result = inspect("disk.img", kind="raw_image")
    measured = steps(result)
    assert "error" not in result
    assert result["coverage"]["status"] == "partial"
    assert measured["filesystem:10"]["reason"] == reason
    assert measured["filesystem:200"]["status"] == "complete"


def test_empty_listing_is_complete_only_for_declared_scope(inspect, monkeypatch):
    fake_image_tools(monkeypatch, {10: (0, "", "")})
    result = inspect("disk.img", kind="raw_image")
    assert result["coverage"]["status"] == "complete"
    assert steps(result)["filesystem:10"]["processed"] == 0
    assert steps(result)["filesystem:10"]["total"] is None
    assert "File contents, unallocated space" in result["coverage"]["scope"]


def test_filesystem_fallback_does_not_claim_partition_layout_known(inspect, monkeypatch):
    fake_image_tools(monkeypatch, {0: (0, bodyfile("a"), "")}, discovery=(1, "", "No partition table"))
    result = inspect("disk.img", kind="raw_image")
    assert result["coverage"]["status"] == "partial"
    assert steps(result)["partitions"]["status"] == "unknown"
    assert steps(result)["filesystem:0"]["status"] == "complete"


def test_malformed_records_and_tool_warnings_limit_coverage(inspect, monkeypatch):
    fake_image_tools(monkeypatch, {10: (0, bodyfile("a") + "bad record\n", ""),
                                  200: (0, bodyfile("b"), "Read warning")})
    result = inspect("disk.img", kind="raw_image")
    assert steps(result)["filesystem:10"]["malformed_records"] == 1
    assert steps(result)["filesystem:10"]["reason"] == "malformed_records"
    assert steps(result)["filesystem:200"]["reason"] == "tool_warning"
    assert result["coverage"]["status"] == "partial"


def test_missing_tool_preserves_hash_and_unattempted_scope(inspect, monkeypatch):
    monkeypatch.setattr("cds.analysis.shutil.which", lambda _name: None)
    result = inspect("disk.img", kind="raw_image")
    assert result["coverage"]["status"] == "failed"
    assert steps(result)["hash"]["status"] == "complete"
    assert steps(result)["partitions"]["reason"] == "missing_tool"
    assert steps(result)["inventory"]["status"] == "skipped"


def test_old_database_migration_is_idempotent_and_does_not_invent_coverage(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    with store.connect() as db:
        db.execute("ALTER TABLE evidence DROP COLUMN coverage")
    case = store.create_case("Old case", "")
    path = tmp_path / "legacy"
    path.write_bytes(b"legacy")
    store.register("legacy", case["id"], "legacy.txt", 6, path, "file", 512)
    with store.connect() as db:
        db.execute("UPDATE jobs SET status='completed',progress=100")
    store.initialize()
    store.initialize()
    item = store.detail("legacy")
    assert item["status"] == "completed"
    assert item["coverage"] is None
    assert store.retry("legacy")


def test_coverage_persists_through_api_retry_restart_and_exports(tmp_path):
    settings = Settings(tmp_path)
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        case = client.post("/api/cases", json={"name": "Coverage"}, headers=HEADERS).json()
        route = f"/api/cases/{case['id']}"
        eid = client.post(route + "/evidence", params={"filename": "large.log"},
                          content=b"a" * 70000, headers=HEADERS).json()["id"]
        store = app.state.store
        job = store.claim()
        result = analyze(job, {"tool_timeout": 5, "max_artifacts": 20}, str(tmp_path))
        store.finish(job, result)
        coverage = result["coverage"]
        assert coverage["status"] == "partial"
        assert client.get(f"/api/evidence/{eid}").json()["coverage"] == coverage
        assert client.get(route + "/evidence").json()[0]["coverage"] == coverage
        payload = client.get(route + "/export?format=json").json()
        assert payload["evidence"][0]["coverage"] == coverage
        rows = list(csv.DictReader(io.StringIO(client.get(route + "/export").text)))
        assert rows[0]["coverage_status"] == "partial"
        assert rows[0]["coverage_run_id"] == coverage["run_id"]
        assert rows[0]["job_status"] == "completed"
        assert client.post(f"/api/evidence/{eid}/retry", headers=HEADERS).status_code == 202
        assert client.post(f"/api/evidence/{eid}/retry", headers=HEADERS).status_code == 409
        assert store.detail(eid)["coverage"] == coverage
        store.claim()
        store.recover_interrupted()
        assert store.detail(eid)["status"] == "queued"
        assert store.detail(eid)["coverage"] == coverage
        retried = store.claim()
        refreshed = analyze(retried, {"tool_timeout": 5, "max_artifacts": 20}, str(tmp_path))
        store.finish(retried, refreshed)
        assert refreshed["coverage"]["run_id"] != coverage["run_id"]
        assert steps(refreshed)["hash"]["reason"] == "hash_verified"
    with TestClient(create_app(settings, start_workers=False)) as client:
        assert client.get(f"/api/evidence/{eid}").json()["coverage"] == refreshed["coverage"]


def test_worker_failure_retains_known_hash_and_exports_empty_scope(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    case = store.create_case("Failure", "")
    path = tmp_path / "source"
    path.write_bytes(b"x")
    store.register("source", case["id"], "source.txt", 1, path, "file", 512)
    with store.connect() as db:
        db.execute("UPDATE evidence SET sha256='original'")
    job = store.claim()
    store.finish(job, {"error": "Worker stopped unexpectedly"})
    assert store.detail("source")["sha256"] == "original"
    payload = store.export_rows(case["id"])
    assert payload["items"] == []
    assert payload["evidence"][0]["coverage"]["status"] == "unknown"
    assert payload["evidence"][0]["job_status"] == "failed"


def test_malformed_partition_records_remain_a_coverage_gap(inspect, monkeypatch):
    fake_image_tools(monkeypatch, {10: (0, bodyfile("a"), "")}, discovery=(
        0, "000: 000:000 10 109 100 Partition\n001: invalid partition row", ""))
    result = inspect("disk.img", kind="raw_image")
    assert result["coverage"]["status"] == "partial"
    assert steps(result)["partitions"]["reason"] == "malformed_partition_records"
    assert steps(result)["partitions"]["total"] is None


@pytest.mark.parametrize("name,content", [
    ("keys.json", ('{"' + 'k' * 257 + '":1}').encode()),
    ("columns.csv", (",".join(["column"] * 101) + "\n").encode()),
])
def test_metadata_clipping_is_not_reported_as_complete(inspect, name, content):
    result = inspect(name, content)
    assert steps(result)["content"]["reason"] == "metadata_limit"
    assert result["coverage"]["status"] == "partial"


def test_source_change_prevents_parsing_and_never_claims_integrity(tmp_path):
    path = tmp_path / "source.txt"
    path.write_bytes(b"changed")
    job = {"name": path.name, "source_path": str(path), "size": 7,
           "kind": "file", "sector_size": 512, "imported_at": now(), "sha256": "previous"}
    result = analyze(job, {"tool_timeout": 5, "max_artifacts": 20}, str(tmp_path))
    assert result["sha256"] == "previous"
    assert result["coverage"]["status"] == "failed"
    assert steps(result)["hash"]["reason"] == "source_hash_changed"
    assert steps(result)["content"]["status"] == "skipped"
    assert result["artifacts"] == []
