"""Pagination and provenance for the case timeline; range validation lives in test_cases."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from cds.config import Settings
from cds.main import create_app

HEADERS = {"X-CDS-Request": "local-ui"}


@pytest.fixture
def timeline(tmp_path):
    app = create_app(Settings(tmp_path), start_workers=False)
    with TestClient(app) as client:
        case = client.post("/api/cases", json={"name": "Timeline"}, headers=HEADERS).json()
        source = client.post(f"/api/cases/{case['id']}/evidence", params={"filename": "fixture.img"},
                             content=b"synthetic", headers=HEADERS).json()["id"]
        store = app.state.store
        job = store.claim()
        store.finish(job, {"artifacts": [
            {"path": f"/file-{i}.txt", "kind": "file", "details": {
                "timestamps_unix": {"created": 1700000000 + i // 3}}}
            for i in range(205)
        ]})
        yield client, store, case["id"], source, job["run_id"]


def test_pages_are_stable_complete_and_run_scoped(timeline):
    client, _, case, source, run = timeline
    route = f"/api/cases/{case}/timeline"
    first = client.get(route).json()
    assert (first["total"], first["offset"], first["limit"]) == (205, 0, 100)
    assert len(first["events"]) == 100
    assert client.get(route).json() == first
    events = list(first["events"])
    for offset, expected in ((100, 100), (200, 5)):
        page = client.get(route, params={"offset": offset, "revision": first["revision"]}).json()
        assert page["offset"] == offset and page["revision"] == first["revision"]
        assert page["total"] == 205 and len(page["events"]) == expected
        events.extend(page["events"])
    assert len({event["id"] for event in events}) == 205
    order = [(event["at"], event["id"]) for event in events]
    assert order == sorted(order)
    assert all(event["run_id"] == run and event["source_id"] == source for event in events)
    artifact_ids = {item["id"] for item in client.get(
        f"/api/evidence/{source}/artifacts", params={"offset": 200}).json()["items"]}
    assert artifact_ids <= {event["artifact_id"] for event in events}
    assert len(client.get(route, params={"limit": 200}).json()["events"]) == 200


def test_filter_count_precedes_pagination(timeline):
    client, _, case, _, _ = timeline
    bound = datetime.fromtimestamp(1700000001, timezone.utc).isoformat()
    data = client.get(f"/api/cases/{case}/timeline", params={
        "start": bound, "end": bound, "offset": 1, "limit": 1}).json()
    assert data["total"] == 3 and data["offset"] == 1 and len(data["events"]) == 1
    assert data["events"][0]["at"] == bound


def test_record_timestamp_labels_are_specific_and_unknown_kinds_stay_neutral(timeline):
    client, store, case, source, _ = timeline
    labels = {"browser_visit": "Browser visit", "registry_key": "Registry key last-write",
              "windows_event": "Event creation time"}
    unknown_kind = "future_record"
    store.retry(source)
    store.finish(store.claim(), {
        "artifacts": [{"key": "source", "path": "/records", "kind": "file"}],
        "records": [{"artifact_key": "source", "kind": kind, "source_key": kind,
                     "event_time_us": 1700000000123456, "summary": kind,
                     "parser": "fixture/1", "details": {}}
                    for kind in [*labels, unknown_kind]],
    })
    events = client.get(f"/api/cases/{case}/timeline").json()["events"]
    actual = {event["timestamp_kind"]: event["timestamp_label"] for event in events}
    assert actual == {**labels, unknown_kind: f"Record timestamp ({unknown_kind})"}
    assert actual[unknown_kind] not in labels.values()


@pytest.mark.parametrize("params", [{"offset": -1}, {"limit": 0}, {"limit": 201}, {"revision": "x" * 65}])
def test_invalid_page_parameters(timeline, params):
    client, _, case, _, _ = timeline
    assert client.get(f"/api/cases/{case}/timeline", params=params).status_code == 422


def test_new_run_resets_page_but_old_event_still_opens_its_artifact(timeline):
    client, store, case, source, run = timeline
    route = f"/api/cases/{case}/timeline"
    before = client.get(route).json()
    selected = before["events"][0]
    store.retry(source)
    job = store.claim()
    # An in-progress run has not changed the saved result set.
    assert client.get(route).json()["revision"] == before["revision"]
    store.finish(job, {"artifacts": [{"path": "/new.txt", "kind": "file", "details": {
        "timestamps_unix": {"created": 1700000500}}}]})
    after = client.get(route, params={"offset": 100, "revision": before["revision"]}).json()
    assert after["revision"] != before["revision"]
    assert (after["offset"], after["total"]) == (0, 1)
    assert after["events"][0]["run_id"] == job["run_id"]
    assert after["events"][0]["id"] != selected["id"]
    artifact = client.get(f"/api/evidence/{source}/artifacts/{selected['artifact_id']}").json()
    assert artifact["run_id"] == run and artifact["path"] == selected["artifact_path"]
    assert not artifact["download"]["available"]
    detail = client.get(f"/api/evidence/{source}", params={"run_id": run}).json()
    assert detail["run_id"] == run and detail["artifact_count"] == 205


def test_import_changes_revision_and_other_cases_are_isolated(timeline):
    client, store, case, _, _ = timeline
    route = f"/api/cases/{case}/timeline"
    before = client.get(route).json()
    other = client.post("/api/cases", json={"name": "Other"}, headers=HEADERS).json()["id"]
    client.post(f"/api/cases/{other}/evidence", params={"filename": "other.txt"}, content=b"other", headers=HEADERS)
    assert client.get(route).json() == before
    source = client.post(f"/api/cases/{case}/evidence", params={"filename": "notes.txt"},
                         content=b"notes", headers=HEADERS).json()["id"]
    after = client.get(route, params={"offset": 200, "revision": before["revision"]}).json()
    assert after["offset"] == 0 and after["total"] == 206 and after["revision"] != before["revision"]
    event = client.get(route, params={"offset": 205}).json()["events"][0]
    assert event["origin"] == "import" and event["run_id"] is None and event["artifact_id"] is None
    # Finish the other source before this one; each import fallback pins its own saved run.
    store.finish(store.claim(), {"artifacts": []})
    job = store.claim()
    assert job["id"] == source
    store.finish(job, {"artifacts": []})
    saved = client.get(route, params={"offset": 205}).json()["events"][0]
    assert saved["run_id"] == job["run_id"] and saved["id"] != event["id"]
