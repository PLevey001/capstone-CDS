import asyncio
import csv
import hashlib
import io
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cds.config import Settings
from cds.main import create_app
from cds.store import Store

HEADERS = {"X-CDS-Request": "local-ui"}


def create_case(client, name="Case to manage"):
    response = client.post("/api/cases", json={"name": name, "description": "Keep this description"}, headers=HEADERS)
    assert response.status_code == 201
    return response.json()


def upload(client, case_id):
    response = client.post(f"/api/cases/{case_id}/evidence", params={"filename": "source.txt"},
                           content=b"test evidence", headers=HEADERS)
    assert response.status_code == 202
    return response.json()["id"]


def finish_job(store, failed=False):
    job = store.claim()
    work = store.root / "work" / job["job_id"]
    work.mkdir()
    (work / "progress.json").write_text('{"progress": 100}')
    result = {"sha256": hashlib.sha256(b"test evidence").hexdigest(),
              "artifacts": [{"path": "/source.txt", "kind": "file"}],
              "partitions": [{"slot": "0", "start_sector": 2048, "length_sectors": 32,
                              "sector_size": 512, "description": "Synthetic partition"}]}
    if failed:
        result["error"] = "Synthetic failure"
    store.finish(job, result)
    return work


def test_rename_preserves_case_and_evidence_across_restart(tmp_path):
    settings = Settings(tmp_path)
    with TestClient(create_app(settings, start_workers=False)) as client:
        case = create_case(client)
        eid = upload(client, case["id"])
        path = tmp_path / "evidence" / eid
        response = client.patch(f"/api/cases/{case['id']}", json={"name": "  Renamed investigation  "}, headers=HEADERS)
        assert response.status_code == 200
        updated = response.json()
        assert updated == {**case, "name": "Renamed investigation", "evidence_count": 1}
        assert path.read_bytes() == b"test evidence"
        assert client.get(f"/api/evidence/{eid}").json()["case_id"] == case["id"]
        events = client.get(f"/api/cases/{case['id']}/audit").json()
        assert events[0]["action"] == "case_renamed"
        assert case["name"] in events[0]["detail"] and updated["name"] in events[0]["detail"]
        # Saving the existing name is a no-op rather than a duplicate activity entry.
        client.patch(f"/api/cases/{case['id']}", json={"name": updated["name"]}, headers=HEADERS)
        assert client.get(f"/api/cases/{case['id']}/audit").json() == events
    with TestClient(create_app(settings, start_workers=False)) as client:
        assert client.get("/api/cases").json() == [updated]


def test_case_mutation_validation_and_request_guard(tmp_path):
    with TestClient(create_app(Settings(tmp_path), start_workers=False)) as client:
        case = create_case(client)
        route = f"/api/cases/{case['id']}"
        assert client.patch(route, json={"name": "New"}).status_code == 403
        assert client.delete(route).status_code == 403
        for body in ({}, {"name": ""}, {"name": "  \t"}, {"name": "x" * 121}, {"name": None}):
            assert client.patch(route, json=body, headers=HEADERS).status_code == 422
        assert client.patch("/api/cases/missing", json={"name": "New"}, headers=HEADERS).status_code == 404
        assert client.delete("/api/cases/missing", headers=HEADERS).status_code == 404
        assert client.get("/api/cases").json()[0]["name"] == case["name"]


@pytest.mark.parametrize("failed", [False, True])
def test_delete_removes_only_selected_case_records_and_files(tmp_path, failed):
    settings = Settings(tmp_path)
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        case = create_case(client)
        eid = upload(client, case["id"])
        work = finish_job(app.state.store, failed)
        survivor = create_case(client, "Keep me")
        keep_eid = upload(client, survivor["id"])
        keep_work = finish_job(app.state.store)
        keep_detail = client.get(f"/api/evidence/{keep_eid}").json()
        keep_audit = client.get(f"/api/cases/{survivor['id']}/audit").json()
        # Unrelated files and upload originals are outside the cleanup scope.
        original = tmp_path / "original.txt"
        original.write_bytes(b"test evidence")
        unreferenced = tmp_path / "evidence" / "unreferenced"
        unreferenced.write_bytes(b"leave alone")
        response = client.delete(f"/api/cases/{case['id']}", headers=HEADERS)
        assert response.status_code == 200
        assert response.json() == {"id": case["id"], "cleanup_pending": 0}
        assert not (tmp_path / "evidence" / eid).exists()
        assert not work.exists()
        assert original.read_bytes() == b"test evidence"
        assert unreferenced.read_bytes() == b"leave alone"
        assert keep_work.exists() and (tmp_path / "evidence" / keep_eid).read_bytes() == b"test evidence"
        assert client.get(f"/api/evidence/{keep_eid}").json() == keep_detail
        assert client.get(f"/api/cases/{survivor['id']}/audit").json() == keep_audit
        for path in (f"/api/evidence/{eid}", f"/api/evidence/{eid}/artifacts",
                     f"/api/cases/{case['id']}/evidence", f"/api/cases/{case['id']}/audit"):
            assert client.get(path).status_code == 404
        assert client.post(f"/api/evidence/{eid}/retry", headers=HEADERS).status_code == 404
        assert client.delete(f"/api/cases/{case['id']}", headers=HEADERS).status_code == 404
        with app.state.store.connect() as db:
            for table in ("jobs", "artifacts", "partitions"):
                assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE evidence_id=?", (eid,)).fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM audit WHERE case_id=?", (case["id"],)).fetchone()[0] == 0
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with TestClient(create_app(settings, start_workers=False)) as client:
        assert [c["id"] for c in client.get("/api/cases").json()] == [survivor["id"]]


def test_delete_last_empty_case(tmp_path):
    with TestClient(create_app(Settings(tmp_path), start_workers=False)) as client:
        case = create_case(client)
        assert client.delete(f"/api/cases/{case['id']}", headers=HEADERS).json()["cleanup_pending"] == 0
        assert client.get("/api/cases").json() == []


@pytest.mark.parametrize("running", [False, True])
def test_delete_rejects_active_jobs_without_changing_case(tmp_path, running):
    app = create_app(Settings(tmp_path), start_workers=False)
    with TestClient(app) as client:
        case = create_case(client)
        eid = upload(client, case["id"])
        if running:
            app.state.store.claim()
        before = client.get(f"/api/cases/{case['id']}/audit").json()
        response = client.delete(f"/api/cases/{case['id']}", headers=HEADERS)
        assert response.status_code == 409
        assert "analyses" in response.json()["detail"]
        assert app.state.store.case_exists(case["id"])
        assert (tmp_path / "evidence" / eid).read_bytes() == b"test evidence"
        assert client.get(f"/api/cases/{case['id']}/audit").json() == before


def test_delete_rejects_inflight_upload_and_releases_guard(tmp_path):
    app = create_app(Settings(tmp_path), start_workers=False)
    with TestClient(app) as client:
        case = create_case(client)

        async def exercise():
            started, release = asyncio.Event(), asyncio.Event()

            async def chunks():
                yield b"first"
                started.set()
                await release.wait()
                yield b"last"

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                         headers=HEADERS) as http:
                upload_task = asyncio.create_task(http.post(f"/api/cases/{case['id']}/evidence?filename=source.txt",
                                                           content=chunks()))
                try:
                    await asyncio.wait_for(started.wait(), timeout=5)
                    response = await http.delete(f"/api/cases/{case['id']}")
                    assert response.status_code == 409
                    assert "uploads" in response.json()["detail"]
                finally:
                    release.set()
                    uploaded = await asyncio.wait_for(upload_task, timeout=5)
                assert uploaded.status_code == 202

        asyncio.run(exercise())
        finish_job(app.state.store)
        assert client.delete(f"/api/cases/{case['id']}", headers=HEADERS).status_code == 200


def test_failed_file_cleanup_is_reported_and_retried_on_restart(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        case = create_case(client)
        eid = upload(client, case["id"])
        finish_job(app.state.store)
        source = tmp_path / "evidence" / eid
        unlink = Path.unlink

        def denied(path, *args, **kwargs):
            if path == source:
                raise PermissionError("Synthetic cleanup error")
            return unlink(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", denied)
            response = client.delete(f"/api/cases/{case['id']}", headers=HEADERS)
        assert response.status_code == 200 and response.json()["cleanup_pending"] == 1
        assert source.exists()
        assert client.get("/api/cases").json() == []
    with TestClient(create_app(settings, start_workers=False)) as client:
        assert not source.exists()
        assert client.get("/api/cases").json() == []


def test_restart_completes_deletion_after_metadata_commit(tmp_path):
    app = create_app(Settings(tmp_path), start_workers=False)
    with TestClient(app) as client:
        case = create_case(client)
        eid = upload(client, case["id"])
        work = finish_job(app.state.store)
        # Simulate termination after the database commit, before file cleanup.
        assert app.state.store.delete_case(case["id"])
        assert (tmp_path / "evidence" / eid).exists()
    restarted = Store(tmp_path)
    restarted.initialize()
    assert not (tmp_path / "evidence" / eid).exists() and not work.exists()
    assert restarted.cleanup_deleted_files() == 0


def test_cleanup_does_not_follow_links_or_escape_storage(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    original = tmp_path / "original.txt"
    original.write_bytes(b"original")
    link = tmp_path / "evidence" / "link"
    link.symlink_to(original)
    with store.connect() as db:
        db.executemany("INSERT INTO pending_deletions VALUES(?,?)",
                       [("evidence/link", "deleted-case"), ("evidence/../../original.txt", "deleted-case")])
    assert store.cleanup_deleted_files() == 1
    assert original.read_bytes() == b"original"
    assert not link.exists()

def test_export_returns_csv_and_json_with_provenance(tmp_path):
    app = create_app(Settings(tmp_path), start_workers=False)
    with TestClient(app) as client:
        case = create_case(client, "Export case")
        upload(client, case["id"])
        finish_job(app.state.store)
        route = f"/api/cases/{case['id']}/export"
        digest = hashlib.sha256(b"test evidence").hexdigest()

        response = client.get(route, params={"format": "csv"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert f'filename="cds-case-{case["id"][:8]}.csv"' in response.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == 1
        assert rows[0]["source_name"] == "source.txt"
        assert rows[0]["source_sha256"] == digest
        assert rows[0]["artifact_path"] == "/source.txt"

        payload = client.get(route, params={"format": "json"}).json()
        assert payload["case"]["name"] == "Export case"
        assert [item["artifact_path"] for item in payload["items"]] == ["/source.txt"]
        assert payload["items"][0]["source_sha256"] == digest

        assert client.get(route, params={"format": "xml"}).status_code == 422
        assert client.get("/api/cases/missing/export").status_code == 404
