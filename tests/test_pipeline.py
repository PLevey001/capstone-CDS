import hashlib
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cds.analysis import analyze, parse_bodyfile, run_tool
from cds.config import Settings
from cds.coordinator import Coordinator
from cds.main import create_app
from cds.instance_lock import InstanceLock
from cds.store import Store, now
from scripts.make_demo import fat12_image, partitioned_image

HEADERS = {"X-CDS-Request": "local-ui"}


def job_for(path, kind="file"):
    return {"id": "test", "name": path.name, "source_path": str(path), "size": path.stat().st_size,
            "kind": kind, "sector_size": 512, "imported_at": now()}


def analyze_file(tmp_path, name, data, kind="file", max_artifacts=20000):
    path = tmp_path / name
    path.write_bytes(data)
    return analyze(job_for(path, kind), {"tool_timeout": 10, "max_artifacts": max_artifacts}, str(tmp_path))


def wait_done(client, case_id):
    until = time.monotonic() + 20
    while time.monotonic() < until:
        items = client.get(f"/api/cases/{case_id}/evidence").json()
        if items and all(e["status"] in {"completed", "failed"} for e in items):
            return items
        time.sleep(0.1)
    pytest.fail("Jobs did not finish")


def test_hash_and_json_metadata(tmp_path):
    data = b'{"device": "demo", "events": [1, 2]}'
    result = analyze_file(tmp_path, "demo.json", data)
    assert "error" not in result
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["metadata"]["top_level_keys"] == ["device", "events"]


def test_invalid_json_retains_source_hash(tmp_path):
    result = analyze_file(tmp_path, "bad.json", b"{broken")
    assert "JSONDecodeError" in result["error"]
    assert result["sha256"] == hashlib.sha256(b"{broken").hexdigest()


def test_empty_file_and_csv_metadata(tmp_path):
    result = analyze_file(tmp_path, "empty.txt", b"")
    assert result["sha256"] == hashlib.sha256(b"").hexdigest()
    assert result["metadata"]["sample_line_count"] == 0
    result = analyze_file(tmp_path, "events.csv", b"time,event\n10:00,created\n")
    assert result["metadata"]["columns"] == ["time", "event"]


@pytest.mark.skipif(not shutil.which("fls") or not shutil.which("mmls"), reason="Install The Sleuth Kit for image integration tests")
@pytest.mark.parametrize("partitioned", [False, True])
def test_actual_disk_image_inventory_and_deleted_entry(tmp_path, partitioned):
    data = partitioned_image() if partitioned else fat12_image()
    result = analyze_file(tmp_path, "disk.img", data, "raw_image")
    assert "error" not in result, result.get("error")
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    files = {a["path"]: a for a in result["artifacts"]}
    assert "/REPORT.TXT" in files
    assert files["/REPORT.TXT"]["partition_offset"] == (2048 if partitioned else 0)
    assert any(item.get("deleted") and "ECRET.TXT" in item["path"] for item in result["artifacts"])
    if partitioned:
        assert result["partitions"][0]["start_sector"] == 2048


def test_bodyfile_handles_pipe_names_and_flags_bad_records():
    output = "0|/a|b.txt|12|r/rrw-r--r--|0|0|12|1|2|3|4\ninvalid\n"
    items, malformed, truncated = parse_bodyfile(output, 2048, 20)
    assert items[0]["path"] == "/a|b.txt"
    assert items[0]["details"]["timestamps_unix"]["modified"] == 2
    assert malformed == 1 and not truncated
    assert parse_bodyfile(output, 2048, 0)[2]


def test_tool_timeout_terminates_process():
    import sys
    with pytest.raises(ValueError, match="exceeded"):
        run_tool([sys.executable, "-c", "import time; time.sleep(5)"], 0.1)


def test_missing_image_tools_is_an_explicit_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("cds.analysis.shutil.which", lambda name: None)
    result = analyze_file(tmp_path, "disk.img", fat12_image(), "raw_image")
    assert "requires The Sleuth Kit" in result["error"]
    assert result["sha256"]


@pytest.mark.skipif(not shutil.which("fls") or not shutil.which("mmls"), reason="Requires TSK")
def test_malformed_image_and_explicit_inventory_limit(tmp_path):
    result = analyze_file(tmp_path, "bad.img", b"not a disk", "raw_image")
    assert "No supported filesystem" in result["error"]
    result = analyze_file(tmp_path, "limited.img", fat12_image(), "raw_image", max_artifacts=2)
    assert len(result["artifacts"]) == 2
    assert any("inventory is incomplete" in warning for warning in result["warnings"])


def test_retry_detects_same_size_source_tampering(tmp_path):
    path = tmp_path / "test.txt"
    path.write_bytes(b"first")
    job = job_for(path)
    job["sha256"] = hashlib.sha256(b"first").hexdigest()
    path.write_bytes(b"other")
    result = analyze(job, {"tool_timeout": 10, "max_artifacts": 20}, str(tmp_path))
    assert "hash changed" in result["error"]
    assert result["sha256"] == job["sha256"]
    assert result["metadata"]["observed_sha256"] == hashlib.sha256(b"other").hexdigest()


def test_only_one_instance_owns_data_directory(tmp_path):
    first = InstanceLock(tmp_path)
    second = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="Another CDS server"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_api_validation_limits_and_readonly_storage(tmp_path):
    app = create_app(Settings(tmp_path, max_upload_bytes=20), start_workers=False)
    with TestClient(app) as client:
        assert client.post("/api/cases", json={"name": "bad"}).status_code == 403
        assert client.post("/api/cases", json={"name": "   "}, headers=HEADERS).status_code == 422
        case = client.post("/api/cases", json={"name": "Test case"}, headers=HEADERS).json()
        route = f"/api/cases/{case['id']}/evidence"
        assert client.post(route, params={"filename": "too-big.txt"}, content=b"x" * 21, headers=HEADERS).status_code == 413
        assert list((tmp_path / "evidence").iterdir()) == []
        response = client.post(route, params={"filename": "../../safe.txt"}, content=b"sample", headers=HEADERS)
        assert response.status_code == 202
        eid = response.json()["id"]
        item = client.get(f"/api/evidence/{eid}").json()
        assert item["name"] == "safe.txt" and "source_path" not in item
        assert (tmp_path / "evidence" / eid).read_bytes() == b"sample"
        assert (tmp_path / "evidence" / eid).stat().st_mode & 0o222 == 0
        assert client.post(f"/api/evidence/{eid}/retry", headers=HEADERS).status_code == 409
        assert client.post(route, params={"filename": "x", "sector_size": 123}, content=b"", headers=HEADERS).status_code == 422
        assert client.get("/api/cases/missing/evidence").status_code == 404
        assert client.get("/api/health", headers={"Host": "attacker.invalid"}).status_code == 400


def test_pipeline_failure_isolation_retry_and_persistence(tmp_path):
    settings = Settings(tmp_path, workers=2)
    with TestClient(create_app(settings)) as client:
        case = client.post("/api/cases", json={"name": "Mixed inputs"}, headers=HEADERS).json()
        route = f"/api/cases/{case['id']}/evidence"
        for name, content in [("good.json", b'{"ok": true}'), ("bad.json", b"{invalid"), ("notes.txt", b"known contents")]:
            assert client.post(route, params={"filename": name}, content=content, headers=HEADERS).status_code == 202
        items = wait_done(client, case["id"])
        assert sorted(item["status"] for item in items) == ["completed", "completed", "failed"]
        good = next(item for item in items if item["name"] == "notes.txt")
        assert good["sha256"] == hashlib.sha256(b"known contents").hexdigest()
        assert client.get(f"/api/evidence/{good['id']}/artifacts", params={"q": "%"}).json()["total"] == 0
        failed = next(item for item in items if item["status"] == "failed")
        assert client.post(f"/api/evidence/{failed['id']}/retry", headers=HEADERS).status_code == 202
        wait_done(client, case["id"])
        assert client.get(f"/api/evidence/{failed['id']}/artifacts").json()["total"] == 1
        assert any(event["action"] == "analysis_retried" for event in client.get(f"/api/cases/{case['id']}/audit").json())
    with TestClient(create_app(settings, start_workers=False)) as client:
        assert len(client.get(f"/api/cases/{case['id']}/evidence").json()) == 3


def test_restart_requeues_interrupted_jobs(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    case = store.create_case("Interrupted", "")
    source = tmp_path / "evidence" / "source"
    source.write_bytes(b"test")
    store.register("source", case["id"], "test.txt", 4, source, "file", 512)
    job = store.claim()
    assert job
    store.recover_interrupted()
    assert store.evidence(case["id"])[0]["status"] == "queued"
    assert store.audit(case["id"])[0]["action"] == "analysis_requeued"


def blocked_worker(job, settings, work_dir):
    """A real process barrier proves two jobs can run before either completes."""
    root = Path(work_dir).parents[1]
    (root / f"started-{job['id']}").write_text("started")
    until = time.monotonic() + 10
    while not (root / "release").exists():
        if time.monotonic() > until:
            raise TimeoutError("Test did not release workers")
        time.sleep(0.02)
    return {"sha256": "synthetic-test-result", "metadata": {}, "artifacts": [], "warnings": []}


def test_worker_limit_and_actual_parallel_processes(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.initialize()
    case = store.create_case("Concurrency", "")
    for index in range(3):
        path = tmp_path / "evidence" / str(index)
        path.write_bytes(b"test")
        store.register(str(index), case["id"], f"{index}.txt", 4, path, "file", 512)
    monkeypatch.setattr("cds.coordinator.analyze", blocked_worker)
    coordinator = Coordinator(store, Settings(tmp_path, workers=2))
    coordinator.start()
    try:
        until = time.monotonic() + 10
        while time.monotonic() < until and len(list(tmp_path.glob("started-*"))) < 2:
            time.sleep(0.05)
        assert len(list(tmp_path.glob("started-*"))) == 2
        items = store.evidence(case["id"])
        assert sorted(item["status"] for item in items) == ["queued", "running", "running"]
    finally:
        (tmp_path / "release").touch()
        coordinator.stop()
