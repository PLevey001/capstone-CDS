import copy
import csv
import io
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cds.analysis import analyze
from cds.config import Settings
from cds.main import create_app, csv_record_value
from cds.migrations import migrate_records
from cds.store import Store
from scripts.make_browser_demo import make_chrome_history, make_firefox_history

HEADERS = {"X-CDS-Request": "local-ui"}
LIMITS = {"max_artifacts": 20, "tool_timeout": 5}


@pytest.fixture(params=[("History", make_chrome_history), ("places.sqlite", make_firefox_history)], ids=["chrome", "firefox"])
def records(tmp_path, request):
    app = create_app(Settings(tmp_path / "workspace"), start_workers=False)
    with TestClient(app) as client:
        case = client.post("/api/cases", json={"name": "Browser records"}, headers=HEADERS).json()["id"]
        name, make_history = request.param
        path = make_history(tmp_path / name, count=120)
        source = client.post(f"/api/cases/{case}/evidence", params={"filename": name},
                             content=path.read_bytes(), headers=HEADERS).json()["id"]
        store = app.state.store
        job = store.claim()
        result = analyze(job, LIMITS, str(store.root / "work"))
        assert store.finish(job, result)
        yield client, store, case, source, job, result


def test_record_pages_counts_literal_search_and_source_provenance(records):
    client, _, case, source, job, result = records
    route = f"/api/evidence/{source}/records"
    page = client.get(route).json()
    assert len(page["items"]) == 50 and page["total"] == 120 and page["run_id"] == job["run_id"]
    first = page["items"][0]
    assert first["at"] == "2023-11-14T22:13:20.123456+00:00"
    assert first["evidence_id"] == source and first["run_id"] == job["run_id"]
    artifact = client.get(f"/api/evidence/{source}/artifacts/{first['artifact_id']}").json()
    assert artifact["run_id"] == first["run_id"] and artifact["details"]["sha256"] == result["sha256"]
    assert client.get(f"{route}/{first['id']}").json() == first
    last = client.get(route, params={"offset": 100}).json()
    assert len(last["items"]) == 20 and last["items"][-1]["source_key"] == "120"
    for query in ("%_", "100%_done", "=2+2"):
        match = client.get(route, params={"q": query}).json()
        assert match["total"] == 1 and match["items"][0]["source_key"] == "120"
    evidence = client.get(f"/api/cases/{case}/evidence").json()[0]
    assert evidence["artifact_count"] == 1 and evidence["record_count"] == 120
    assert client.get(f"/api/evidence/{source}").json()["record_count"] == 120
    assert client.get(f"/api/evidence/{source}/runs").json()[0]["record_count"] == 120
    for params in ({"offset": -1}, {"limit": 0}, {"limit": 201}):
        assert client.get(route, params=params).status_code == 422


def test_records_cannot_cross_case_source_or_run(records):
    client, _, case, source, job, _ = records
    first = client.get(f"/api/evidence/{source}/records").json()["items"][0]
    other = client.post("/api/cases", json={"name": "Other"}, headers=HEADERS).json()["id"]
    other_source = client.post(f"/api/cases/{other}/evidence", params={"filename": "other.txt"},
                               content=b"other", headers=HEADERS).json()["id"]
    assert client.get(f"/api/evidence/{other_source}/records/{first['id']}").status_code == 404
    assert client.get(f"/api/evidence/{other_source}/records", params={"run_id": job["run_id"]}).status_code == 404
    assert client.get(f"/api/cases/{other}/records/export", params={"run_id": job["run_id"]}).status_code == 404
    assert client.get(f"/api/cases/{case}/records/export", params={"run_id": "missing"}).status_code == 404
    assert client.get("/api/cases/missing/records/export").status_code == 404


def test_record_timeline_uses_real_times_and_never_invents_imports_after_filtering(records):
    client, _, case, source, job, _ = records
    route = f"/api/cases/{case}/timeline"
    first = client.get(route).json()["events"][0]
    assert first["origin"] == "record" and first["timestamp_label"] == "Browser visit"
    record = client.get(f"/api/evidence/{source}/records/{first['record_id']}").json()
    assert first["artifact_id"] == record["artifact_id"] and first["run_id"] == job["run_id"]
    assert first["at"] == record["at"] == "2023-11-14T22:13:20.123456+00:00"
    exact = client.get(route, params={"start": first["at"], "end": first["at"]}).json()
    assert exact["total"] == 1
    assert client.get(route, params={"start": "2040-01-01T00:00:00Z"}).json()["total"] == 0


def test_exports_include_all_matches_and_escape_only_csv(records):
    client, _, case, source, job, result = records
    route = f"/api/cases/{case}/records/export"
    data = client.get(route).json()
    assert len(data["items"]) == 120
    assert data["items"][-1]["summary"] == "=2+2"
    assert all(row["source_sha256"] == result["sha256"] for row in data["items"])
    response = client.get(route, params={"format": "csv", "run_id": job["run_id"]})
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 120 and rows[-1]["title"] == rows[-1]["summary"] == "'=2+2"
    assert rows[0]["at"] == "2023-11-14T22:13:20.123456+00:00"
    for format in ("csv", "json"):
        response = client.get(route, params={"format": format, "q": "%_"})
        filtered = response.json()["items"] if format == "json" else list(csv.DictReader(io.StringIO(response.text)))
        assert len(filtered) == 1
        assert filtered[0]["source_key"] == "120"
    # The original file-artifact export remains a separate contract.
    artifacts = client.get(f"/api/cases/{case}/export", params={"format": "json"}).json()["items"]
    assert len(artifacts) == 1 and artifacts[0]["evidence_id"] == source


@pytest.mark.parametrize("value,escaped", [
    ("=SUM(A1)", "'=SUM(A1)"), ("  @function", "'  @function"), ("\ttext", "'\ttext"),
    ("\r=1", "'\r=1"), ("\ufeff+2", "'\ufeff+2"), ("-1", "'-1"), (-1, -1),
    ("https://example.test", "https://example.test"), (None, None),
])
def test_spreadsheet_cells_keep_text_from_becoming_formulas(value, escaped):
    assert csv_record_value(value) == escaped


def test_reanalysis_keeps_old_records_and_resets_timeline_pages(records):
    client, store, case, source, first_job, result = records
    route = f"/api/evidence/{source}/records"
    before = client.get(route, params={"run_id": first_job["run_id"], "limit": 200}).json()
    timeline = client.get(f"/api/cases/{case}/timeline").json()
    store.retry(source)
    job = store.claim()
    assert store.finish(job, result)
    after = client.get(route).json()
    assert after["total"] == 120 and after["run_id"] == job["run_id"]
    assert after["items"][0]["id"] != before["items"][0]["id"]
    assert client.get(route, params={"run_id": first_job["run_id"], "limit": 200}).json() == before
    original = before["items"][0]
    assert client.get(f"{route}/{original['id']}").json() == original
    assert not client.get(f"/api/evidence/{source}/artifacts/{original['artifact_id']}").json()["download"]["available"]
    page = client.get(f"/api/cases/{case}/timeline", params={"offset": 100, "revision": timeline["revision"]}).json()
    assert page["offset"] == 0 and page["events"][0]["run_id"] == job["run_id"]
    historical = client.get(f"/api/cases/{case}/records/export", params={"run_id": first_job["run_id"]}).json()
    assert len(historical["items"]) == 120 and historical["items"][0]["id"] == original["id"]


@pytest.mark.parametrize("invalid", ["unknown_artifact", "duplicate_visit", "noninteger_time", "outside_date_range"])
def test_invalid_record_rolls_back_the_entire_saved_result(records, invalid):
    _, store, _, source, old_job, result = records
    before = store.records(source, "", 0, 200)
    store.retry(source)
    job = store.claim()
    broken = copy.deepcopy(result)
    if invalid == "unknown_artifact":
        broken["records"][0]["artifact_key"] = before["items"][0]["artifact_id"]
    elif invalid == "duplicate_visit":
        broken["records"].append(broken["records"][0])
    elif invalid == "noninteger_time":
        broken["records"][0]["event_time_us"] = 1.5
    else:
        broken["records"][0]["event_time_us"] = 9223372036854775807
    with pytest.raises((ValueError, sqlite3.IntegrityError, OverflowError)):
        store.finish(job, broken)
    assert store.detail(source)["run_id"] == old_job["run_id"]
    assert store.records(source, "", 0, 200) == before
    assert store.records(source, "", 0, 200, job["run_id"])["total"] == 0
    assert store.artifacts(source, "", 0, 50, job["run_id"])["total"] == 0
    assert store.finish(job, result)


def test_database_constraints_reject_a_record_from_another_run(records):
    _, store, _, source, _, result = records
    original = store.records(source, "", 0, 1)["items"][0]
    store.retry(source)
    job = store.claim()
    store.finish(job, result)
    with pytest.raises(sqlite3.IntegrityError), store.connect() as db:
        db.execute("UPDATE parsed_records SET run_id=? WHERE id=?", (job["run_id"], original["id"]))


def test_case_deletion_removes_records_without_touching_other_cases(records):
    client, store, case, source, _, _ = records
    other = store.create_case("Keep", "")
    assert client.delete(f"/api/cases/{case}", headers=HEADERS).status_code == 200
    assert not (store.root / "evidence" / source).exists()
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM parsed_records").fetchone()[0] == 0
        assert list(db.execute("PRAGMA foreign_key_check")) == []
    assert store.case_exists(other["id"])


def pre_records_workspace(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    case = store.create_case("Existing workspace", "")
    source = tmp_path / "evidence" / "source"
    source.write_bytes(b"old")
    store.register("source", case["id"], "old.txt", 3, source, "file", 512)
    store.finish(store.claim(), {"artifacts": [{"path": "old.txt", "kind": "source"}]})
    before = store.detail("source")
    with store.connect() as db:
        db.execute("DROP TABLE parsed_records")
        db.execute("DROP INDEX artifacts_owner")
    return store, before


def test_existing_workspace_migration_is_additive_and_repeatable(tmp_path):
    store, before = pre_records_workspace(tmp_path)
    store.initialize()
    store.initialize()
    assert store.detail("source") == before
    assert store.records("source", "", 0, 50)["total"] == 0
    assert store.detail("source")["coverage"]["status"] == "unknown"
    assert (store.root / "evidence" / "source").read_bytes() == b"old"


def test_failed_migration_rolls_back_and_can_be_retried(tmp_path, monkeypatch):
    store, before = pre_records_workspace(tmp_path)
    def fail(db):
        migrate_records(db)
        raise RuntimeError("Simulated interruption")
    with monkeypatch.context() as patch:
        patch.setattr("cds.store.migrate_records", fail)
        with pytest.raises(RuntimeError):
            store.initialize()
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='parsed_records'").fetchone()
        assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='artifacts_owner'").fetchone()
        assert db.execute("SELECT result_run_id FROM evidence WHERE id='source'").fetchone()[0] == before["run_id"]
    store.initialize()
    assert store.detail("source") == before


def test_empty_record_export_retains_run_coverage(records):
    client, _, case, _, job, result = records
    data = client.get(f'/api/cases/{case}/records/export', params={'q': 'no such URL'}).json()
    assert data['items'] == []
    assert data['evidence'][0]['run_id'] == job['run_id']
    assert data['evidence'][0]['coverage']['scope'] == result['coverage']['scope']
    assert data['evidence'][0]['coverage']['limits']['parsed_records'] == 10000


def test_mixed_browser_timeline_and_exports_use_one_utc_scale(tmp_path):
    app = create_app(Settings(tmp_path / "workspace"), start_workers=False)
    with TestClient(app) as client:
        case = client.post("/api/cases", json={"name": "Mixed browsers"}, headers=HEADERS).json()["id"]
        originals = {}
        for name, make_history in (("History", make_chrome_history), ("places.sqlite", make_firefox_history)):
            path = make_history(tmp_path / name)
            before = path.read_bytes()
            source = client.post(f"/api/cases/{case}/evidence", params={"filename": name}, content=before, headers=HEADERS).json()["id"]
            job = app.state.store.claim()
            result = analyze(job, LIMITS, str(app.state.store.root / "work"))
            app.state.store.finish(job, result)
            originals[source] = (job["run_id"], result["sha256"])
            assert path.read_bytes() == before
        timeline = client.get(f"/api/cases/{case}/timeline").json()
        assert timeline["total"] == 6
        assert [event["at"] for event in timeline["events"]] == [
            "2023-11-14T22:13:20.123456+00:00", "2023-11-14T22:13:20.123456+00:00",
            "2023-11-14T22:14:20.123456+00:00", "2023-11-14T22:14:20.123456+00:00",
            "2023-11-14T22:15:20.123456+00:00", "2023-11-14T22:15:20.123456+00:00",
        ]
        assert all(event["origin"] == "record" for event in timeline["events"])
        for event in timeline["events"]:
            record = client.get(f"/api/evidence/{event['source_id']}/records/{event['record_id']}").json()
            assert record["run_id"] == originals[event["source_id"]][0]
            assert record["artifact_id"] == event["artifact_id"]
        instant = "2023-11-14T22:13:20.123456Z"
        exact = client.get(f"/api/cases/{case}/timeline", params={"start": instant, "end": instant, "limit": 1}).json()
        assert exact["total"] == 2 and len(exact["events"]) == 1
        second = client.get(f"/api/cases/{case}/timeline", params={"start": instant, "end": instant, "limit": 1, "offset": 1}).json()
        assert second["events"][0]["id"] != exact["events"][0]["id"]
        exported = client.get(f"/api/cases/{case}/records/export").json()
        assert len(exported["items"]) == 6 and len(exported["evidence"]) == 2
        assert {item["parser"] for item in exported["items"]} == {"chrome-history/1", "firefox-history/1"}
        assert {item["details"]["browser"] for item in exported["items"]} == {"Chrome/Chromium", "Firefox"}
        assert all(item["source_sha256"] == originals[item["evidence_id"]][1] for item in exported["items"])
