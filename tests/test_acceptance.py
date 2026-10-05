"""Stage 10: one imported case, source-side answers, and real worker/API boundaries."""

import csv
import hashlib
import io
import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cds.analysis as analysis
from cds.config import CONTENT_LIMITS, Settings
from cds.main import create_app
from scripts.make_demo import DELETED, NOTE, make_acceptance
from tests.filesystem_fixtures import CHROME, FIREFOX, missing_tools

HEADERS = {"X-CDS-Request": "local-ui"}
LIMITS = {"max_artifacts": 20000, "tool_timeout": 90}
LABELS = {"browser_visit": "Browser visit", "registry_key": "Registry key last-write",
          "windows_event": "Event creation time", "created": "Created", "modified": "Modified",
          "accessed": "Accessed", "metadata_changed": "Metadata changed"}


@pytest.fixture(scope="module")
def acceptance_files(tmp_path_factory):
    missing = missing_tools()
    if missing:
        pytest.skip("Missing acceptance image tools: " + ", ".join(missing))
    directory = tmp_path_factory.mktemp("acceptance") / "sources"
    manifest = make_acceptance(directory)
    assert json.loads((directory / "manifest.json").read_text()) == manifest
    return directory, manifest


def wait_done(client, case):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        rows = client.get(f"/api/cases/{case}/evidence").json()
        if rows and all(row["status"] not in ("running", "queued") for row in rows):
            return {row["name"]: row for row in rows}
        time.sleep(0.05)
    pytest.fail("Acceptance workers did not finish within 30 seconds")


@pytest.fixture(scope="module")
def acceptance(acceptance_files, tmp_path_factory):
    directory, manifest = acceptance_files
    settings = Settings(tmp_path_factory.mktemp("acceptance-workspace"))
    app = create_app(settings)
    with TestClient(app) as client:
        case = client.post("/api/cases", json={"name": manifest["name"]}, headers=HEADERS).json()["id"]
        for name, expected in manifest["sources"].items():
            response = client.post(f"/api/cases/{case}/evidence", headers=HEADERS,
                                   params={"filename": name, "kind": expected["kind"], "sector_size": expected["sector_size"]},
                                   content=(directory / name).read_bytes())
            assert response.status_code == 202
        sources = wait_done(client, case)
        yield client, app.state.store, case, sources, manifest, directory


def all_records(client, source, **params):
    items = []
    while True:
        page = client.get(f"/api/evidence/{source}/records", params={**params, "offset": len(items), "limit": 200}).json()
        items.extend(page["items"])
        if len(items) == page["total"]:
            return items


def record_fact(record, inner):
    detail = record["details"]
    kind = record["kind"]
    fact = {"kind": kind, "parser": record["parser"], "at": record["at"]}
    if kind == "browser_visit":
        fact.update(identity=record["source_key"], url=detail["url"])
    elif kind == "registry_key":
        fact["identity"] = detail["key_path"]
    elif kind == "registry_value":
        value = detail["data"]
        if "data" in detail.get("integer_text_fields", []):
            value = int(value)
        fact.update(identity=detail["value_name"], data=value)
    else:
        fact.update(identity=detail["event_record_id"], event_id=detail["event_id"])
    if inner:
        fact["path"] = record["artifact_path"]
    return fact


def test_import_hash_inventory_records_and_coverage(acceptance):
    client, store, case, sources, manifest, directory = acceptance
    assert len(sources) == len(manifest["sources"]) == 14
    assert sum(row["record_count"] for row in sources.values()) == manifest["record_count"] == 469
    for name, expected in manifest["sources"].items():
        source = sources[name]
        detail = client.get(f"/api/evidence/{source['id']}").json()
        assert detail["sha256"] == expected["sha256"]
        assert hashlib.sha256((store.root / "evidence" / source["id"]).read_bytes()).hexdigest() == expected["sha256"]
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == expected["sha256"]
        assert detail["coverage"]["run_id"] == detail["run_id"]
        assert detail["coverage"]["steps"][0]["status"] == "complete"
        records = all_records(client, source["id"])
        actual = [record_fact(row, name in manifest["filesystems"]) for row in records]
        key = lambda fact: (fact.get("path", ""), fact["kind"], fact["identity"])
        assert sorted(actual, key=key) == sorted(expected["records"], key=key), name
        assert all(row["run_id"] == detail["run_id"] and row["evidence_id"] == source["id"] for row in records)
        assert not list((store.root / "work" / detail["run_id"]).glob("content-*"))
    for name, filesystem in manifest["filesystems"].items():
        artifacts = store.artifacts(sources[name]["id"], "", 0, 200)["items"]
        by_path = {row["path"]: row for row in artifacts}
        for path, expected in filesystem["files"].items():
            listed = path + (" (deleted)" if expected["deleted"] else "")
            if "symlink_target" in expected:
                listed += " -> " + expected["symlink_target"]
            item = by_path[listed]
            assert item["details"]["timestamps_unix"] == expected["timestamps"]
            assert item["size"] == expected.get("inventory_size", expected["size"])
            assert item["partition_offset"] == filesystem["partition_offset"]
        steps = {step["label"]: step for step in store.detail(sources[name]["id"])["coverage"]["steps"]}
        for path in (CHROME, FIREFOX):
            assert steps[path]["sidecar_status"] == "included"
            assert steps[path]["processed"] == 61
            assert all(part["included"] and part["extracted"] for part in steps[path]["sidecars"])
            for part in steps[path]["sidecars"]:
                assert part["sha256"] == filesystem["files"][part["path"]]["sha256"]
    fat = store.artifacts(sources["fat.img"]["id"], "", 0, 200)["items"]
    for path, expected in manifest["fat_files"].items():
        artifact = next(row for row in fat if row["path"] == path)
        assert artifact["size"] == expected["size"] and artifact["deleted"] == expected["deleted"]
        response = client.get(f"/api/evidence/{artifact['evidence_id']}/artifacts/{artifact['id']}/download")
        assert hashlib.sha256(response.content).hexdigest() == expected["sha256"]
    # Standalone snapshots see only 60 visits even with WAL uploaded alongside.
    for name in ("Chrome-History.sqlite", "Firefox-places.sqlite"):
        assert sources[name]["record_count"] == 60
        assert "Separate WAL" in store.detail(sources[name]["id"])["coverage"]["scope"]


def test_four_families_interleave_and_share_inclusive_utc_boundaries(acceptance):
    client, store, case, sources, manifest, _ = acceptance
    route = f"/api/cases/{case}/timeline"
    events = []
    while True:
        page = client.get(route, params={"offset": len(events), "limit": 200}).json()
        events.extend(page["events"])
        if len(events) == page["total"]:
            break
    order = [(datetime.fromisoformat(event["at"]), event["id"]) for event in events]
    assert order == sorted(order) and len({event["id"] for event in events}) == len(events)
    # Expected record times and known-file MAC times come exclusively from inputs.
    expected = []
    for name, source in manifest["sources"].items():
        for record in source["records"]:
            if record["at"] is not None:
                expected.append((name, record.get("path", name), record["kind"], record["identity"], record["at"]))
    for name, filesystem in manifest["filesystems"].items():
        for path, file in filesystem["files"].items():
            listed = path + (" (deleted)" if file["deleted"] else "")
            if "symlink_target" in file:
                listed += " -> " + file["symlink_target"]
            for kind, stamp in file["timestamps"].items():
                expected.append((name, listed, kind, "", datetime.fromtimestamp(stamp, timezone.utc).isoformat()))
    known_files = {(name, path) for name, path, kind, _, _ in expected if kind in ("created", "modified", "accessed", "metadata_changed")}
    facts = {}
    navigated = set()
    for event in events:
        if event["origin"] == "record":
            record = store.record(event["source_id"], event["record_id"])
            if record["kind"] not in navigated and event["source_name"] == "ntfs.img":
                saved = client.get(f"/api/evidence/{event['source_id']}/records/{event['record_id']}").json()
                original = client.get(f"/api/evidence/{event['source_id']}/artifacts/{saved['artifact_id']}").json()
                assert event["artifact_id"] == original["id"]
                assert event["run_id"] == saved["run_id"] == original["run_id"]
                assert original["path"] == event["artifact_path"] and original["partition_offset"] == 2048
                assert original["metadata_address"]
                navigated.add(record["kind"])
            fact = record_fact(record, False)
            identity = fact["identity"]
        elif (event["source_name"], event["artifact_path"]) in known_files:
            identity = ""
        else:
            continue
        assert event["timestamp_label"] == LABELS[event["timestamp_kind"]]
        facts[event["id"]] = (event["source_name"], event["artifact_path"], event["timestamp_kind"], identity, event["at"])
    assert sorted(facts.values()) == sorted(expected)
    assert not any(event["timestamp_kind"] == "registry_value" for event in events)
    assert navigated == {"browser_visit", "registry_key", "windows_event"}

    for instant in manifest["timeline_anchors"]:
        bound = datetime.fromisoformat(instant)
        offset_bound = bound.astimezone(timezone(timedelta(hours=-5))).isoformat()
        exact = client.get(route, params={"start": instant, "end": instant, "limit": 200}).json()
        equivalent = client.get(route, params={"start": offset_bound, "end": offset_bound, "limit": 200}).json()
        assert exact == equivalent
        # The shared anchor spans multiple pages: filesystem directory times
        # and records must not be lost at the page boundary.
        exact_events = list(exact["events"])
        for offset in range(200, exact["total"], 200):
            exact_events.extend(client.get(route, params={"start": instant, "end": instant,
                                "offset": offset, "limit": 200}).json()["events"])
        assert sorted(facts[event["id"]] for event in exact_events if event["id"] in facts) == sorted(
            fact for fact in expected if datetime.fromisoformat(fact[-1]) == bound)
        # Both inclusive edges, and one-microsecond exclusion, apply to every family.
        for params in ({"start": instant}, {"end": instant},
                       {"start": (bound + timedelta(microseconds=1)).isoformat()},
                       {"end": (bound - timedelta(microseconds=1)).isoformat()}):
            filtered_events = []
            while True:
                filtered = client.get(route, params={**params, "offset": len(filtered_events), "limit": 200}).json()
                filtered_events.extend(filtered["events"])
                if len(filtered_events) == filtered["total"]:
                    break
            low = datetime.fromisoformat(params["start"]) if "start" in params else None
            high = datetime.fromisoformat(params["end"]) if "end" in params else None
            assert sorted(facts[event["id"]] for event in filtered_events if event["id"] in facts) == sorted(
                fact for fact in expected if (low is None or datetime.fromisoformat(fact[-1]) >= low)
                and (high is None or datetime.fromisoformat(fact[-1]) <= high))
    tie = [fact for fact in expected if fact[-1] == manifest["timeline_anchors"][0]]
    assert {kind for _, _, kind, _, _ in tie} >= {"browser_visit", "registry_key", "windows_event", "created"}
    # These adjacent instants expose microsecond ordering that string sorting could get wrong.
    controlled = [event for event in events if event["id"] in facts and
                  "2023-11-14T22:14:20" in event["at"]]
    assert {event["at"] for event in controlled} == {"2023-11-14T22:14:20+00:00",
            "2023-11-14T22:14:20.000001+00:00", "2023-11-14T22:14:20.123456+00:00"}


def test_navigation_extraction_recovery_and_export(acceptance):
    client, store, case, sources, manifest, directory = acceptance
    for name in ("ntfs.img", "ext4.img"):
        source = sources[name]["id"]
        for record in all_records(client, source):
            artifact = client.get(f"/api/evidence/{source}/artifacts/{record['artifact_id']}").json()
            assert artifact["run_id"] == record["run_id"]
            assert artifact["partition_offset"] == manifest["filesystems"][name]["partition_offset"]
            assert artifact["metadata_address"]
            assert artifact["details"]["content"]["parser"] == record["parser"]
        for path in (CHROME, FIREFOX, "/Users/examiner/NTUSER.DAT", "/Windows/System32/winevt/Logs/Application.evtx",
                     "/small.txt", "/deleted.txt"):
            artifacts = store.artifacts(source, path, 0, 200)["items"]
            artifact = next(row for row in artifacts if row["path"] in (path, path + " (deleted)"))
            response = client.get(f"/api/evidence/{source}/artifacts/{artifact['id']}/download")
            assert response.status_code == 200
            expected = manifest["filesystems"][name]["files"][path]
            if "recovered_hex" in expected:
                assert response.content == b"" and expected["size"] > 0
            else:
                assert hashlib.sha256(response.content).hexdigest() == expected["sha256"]
        if name == "ext4.img":
            for query in ("symlink.txt", "/Users"):
                artifact = store.artifacts(source, query, 0, 200)["items"][0]
                assert not artifact["download"]["available"]
                response = client.get(f"/api/evidence/{source}/artifacts/{artifact['id']}/download")
                assert response.status_code == 422 and response.json()["detail"] == artifact["download"]["reason"]
            retained = store.artifacts(source, "/deleted-retained.txt", 0, 1)["items"][0]
            response = client.get(f"/api/evidence/{source}/artifacts/{retained['id']}/download")
            assert response.content == b"debugfs unlink keeps these inode extents.\n"
    for query, content in (("REPORT.TXT", NOTE), ("ECRET.TXT", DELETED)):
        source = sources["fat.img"]["id"]
        artifact = store.artifacts(source, query, 0, 1)["items"][0]
        assert client.get(f"/api/evidence/{source}/artifacts/{artifact['id']}/download").content == content
    route = f"/api/cases/{case}/records/export"
    exported = client.get(route).json()
    assert len(exported["items"]) == manifest["record_count"]
    assert len(exported["evidence"]) == 14
    assert len({row["id"] for row in exported["items"]}) == manifest["record_count"]
    for row in exported["items"]:
        source = sources[row["source_name"]]
        assert row["source_sha256"] == manifest["sources"][row["source_name"]]["sha256"]
        assert row["evidence_id"] == source["id"] and row["run_id"] == source["run_id"]
        assert row["parser"] in {"chrome-history/1", "firefox-history/1", "windows-registry/1", "windows-event-log/1"}
    for query in ("", "%_", "ROOT", "Synthetic-Provider"):
        data = client.get(route, params={"q": query}).json()
        rows = list(csv.DictReader(io.StringIO(client.get(route, params={"q": query, "format": "csv"}).text)))
        expected_ids = {row["id"] for source in sources.values() for row in all_records(client, source["id"], q=query)}
        assert {row["id"] for row in data["items"]} == expected_ids
        assert {int(row["id"]) for row in rows} == expected_ids
        assert all(json.loads(row["coverage"])["run_id"] == row["run_id"] for row in rows)
    assert any(row["summary"] == "=2+2" for row in exported["items"])


def test_negative_sources_and_missing_journal_are_visible(acceptance):
    _, store, _, sources, manifest, _ = acceptance
    for name, reason in manifest["diagnostics"].items():
        detail = store.detail(sources[name]["id"])
        assert detail["coverage"]["status"] != "complete"
        assert any(step["reason"] == reason for step in detail["coverage"]["steps"]), detail["coverage"]
        assert detail["record_count"] == len(manifest["sources"][name]["records"])
    detail = store.detail(sources["ntfs.img"]["id"])
    step = next(step for step in detail["coverage"]["steps"] if "MissingJournal/History" in step["label"])
    assert step["sidecar_status"] == "absent" and step["processed"] == 60
    assert "base database only" in step["detail"] and "available inventory" in step["detail"]
    assert "partition_layout_unknown" in {step["reason"] for step in store.detail(sources["ext4.img"]["id"])["coverage"]["steps"]}


@pytest.mark.parametrize("limit,value,reason", [
    ("max_artifacts", 2, "artifact_limit"),
    ("image_content_candidates", 1, "image_candidate_limit"),
    ("image_content_bytes", 8192, "image_content_limit"),
    ("image_content_timeout", 0, "image_content_timeout"),
    ("content_file_bytes", 10, "content_file_limit"),
    ("content_timeout", 0, "content_timeout"),
    ("parsed_records", 13, "record_limit"),
    ("record_payload_bytes", 4000, "record_payload_limit"),
    ("record_text_chars", 8, "record_quality"),
])
def test_combined_image_budget_reporting_and_cleanup(acceptance_files, tmp_path, monkeypatch, limit, value, reason):
    directory, manifest = acceptance_files
    settings = dict(LIMITS)
    if limit in settings:
        settings[limit] = value
    else:
        monkeypatch.setitem(analysis.IMAGE_CONTENT_LIMITS if limit.startswith("image_") else CONTENT_LIMITS, limit, value)
    app = create_app(Settings(tmp_path / "workspace"), start_workers=False)
    with TestClient(app) as client:
        case = client.post("/api/cases", json={"name": "Acceptance budget"}, headers=HEADERS).json()["id"]
        source = client.post(f"/api/cases/{case}/evidence", params={"filename": "ntfs.img"},
                             content=(directory / "ntfs.img").read_bytes(), headers=HEADERS).json()["id"]
        store = app.state.store
        job = store.claim(settings, analysis.PARSER_VERSION)
        work = store.root / "work" / job["run_id"]
        work.mkdir()
        result = analysis.analyze(job, settings, str(work))
        assert result["sha256"] == manifest["sources"]["ntfs.img"]["sha256"]
        assert result["coverage"]["status"] == "partial"
        assert any(step["reason"] == reason for step in result["coverage"]["steps"]), result["coverage"]
        assert result["coverage"]["limits"][limit] == value
        assert len(result["records"]) <= CONTENT_LIMITS["parsed_records"]
        assert store.finish(job, result)
        detail = client.get(f"/api/evidence/{source}").json()
        assert detail["coverage"] == result["coverage"]
        exported = client.get(f"/api/cases/{case}/records/export").json()
        assert len(exported["items"]) == len(result["records"])
        assert exported["evidence"][0]["coverage"]["status"] == "partial"
        assert not list(work.glob("content-*"))


def test_upload_page_extraction_and_tool_limits(acceptance_files, tmp_path, monkeypatch):
    import sys

    directory, _ = acceptance_files
    app = create_app(Settings(tmp_path / "workspace", max_upload_bytes=100), start_workers=False)
    with TestClient(app) as client:
        case = client.post("/api/cases", json={"name": "Acceptance upload limit"}, headers=HEADERS).json()["id"]
        response = client.post(f"/api/cases/{case}/evidence", params={"filename": "NTUSER.DAT"},
                               content=(directory / "NTUSER.DAT").read_bytes(), headers=HEADERS)
        assert response.status_code == 413 and "100-byte" in response.json()["detail"]
        assert list((app.state.store.root / "evidence").iterdir()) == []
        assert client.get(f"/api/cases/{case}/timeline", params={"limit": 201}).status_code == 422
    # Exercise real bounded subprocesses at the configured stderr limit, and
    # reduced output/time limits, without waiting ninety seconds in the suite.
    for code, timeout, size, reason in (
        ("import os; os.write(2, b'x'*65537)", 5, 16 * 1024**2, "tool_output_limit"),
        ("import os; os.write(1, b'x'*1025)", 5, 1024, "tool_output_limit"),
        ("import time; time.sleep(5)", 0.01, 1024, "tool_timeout"),
    ):
        with pytest.raises(analysis.ToolLimitError) as caught:
            analysis._capture([sys.executable, "-c", code], timeout, size)
        assert caught.value.reason == reason
    target = {"path": "NTUSER.DAT", "source_path": str(directory / "NTUSER.DAT"), "source_kind": "file",
              "kind": "source", "run_id": "run", "result_run_id": "run", "deleted": False}
    with pytest.raises(analysis.ToolLimitError, match="extraction size limit"):
        analysis.extract_artifact(target, max_bytes=100)
    # This guard precedes filesystem inspection. Fault-inject discovery output;
    # building 129 real partitions would add no format coverage to this check.
    def many_partitions(args, timeout):
        if args[0] == "mmls":
            return 0, "\n".join(f"{index}: 000:000 {index + 1} {index + 1} 1 Synthetic" for index in range(129)), ""
        return 0, "The Sleuth Kit acceptance stub", ""
    monkeypatch.setattr(analysis, "run_tool", many_partitions)
    path = directory / "fat.img"
    job = {"id": "source", "name": path.name, "source_path": str(path), "size": path.stat().st_size,
           "kind": "raw_image", "sector_size": 512, "imported_at": "2026-01-01T00:00:00Z"}
    result = analysis.analyze(job, LIMITS, str(tmp_path))
    assert result["coverage"]["status"] == "failed"
    assert sum(step["reason"] == "partition_limit" for step in result["coverage"]["steps"]) == 129


def test_reanalysis_restart_interruption_and_case_owned_cleanup(acceptance, tmp_path):
    import sqlite3

    _, original, case, sources, manifest, directory = acceptance
    # Clone the completed case with SQLite backup so lifecycle mutations cannot
    # affect the shared read-only assertions or anyone's development workspace.
    root = tmp_path / "restart-workspace"
    root.mkdir()
    with original.connect() as db, sqlite3.connect(root / "cds.sqlite3") as backup:
        db.backup(backup)
    for folder in ("evidence", "work"):
        shutil.copytree(original.root / folder, root / folder)
    app = create_app(Settings(root), start_workers=False)
    with TestClient(app) as client:
        store = app.state.store
        with store.connect() as db:
            for source in sources.values():
                db.execute("UPDATE evidence SET source_path=? WHERE id=?", (str(root / "evidence" / source["id"]), source["id"]))
        saved = {source["run_id"]: client.get(f"/api/cases/{case}/records/export",
                                            params={"run_id": source["run_id"]}).json() for source in sources.values()}
        before_timeline = client.get(f"/api/cases/{case}/timeline").json()
        for source in sources.values():
            assert client.post(f"/api/evidence/{source['id']}/retry", headers=HEADERS).status_code == 202
            job = store.claim(LIMITS, analysis.PARSER_VERSION)
            work = root / "work" / job["run_id"]
            work.mkdir()
            assert store.finish(job, analysis.analyze(job, LIMITS, str(work)))
            current = store.detail(source["id"])
            assert current["sha256"] == manifest["sources"][source["name"]]["sha256"]
            assert current["record_count"] == len(manifest["sources"][source["name"]]["records"])
        for run, expected in saved.items():
            assert client.get(f"/api/cases/{case}/records/export", params={"run_id": run}).json() == expected
            for record in expected["items"]:
                assert client.get(f"/api/evidence/{record['evidence_id']}/records/{record['id']}").json()["at"] == record["at"]
        reset = client.get(f"/api/cases/{case}/timeline", params={"offset": 100, "revision": before_timeline["revision"]}).json()
        assert reset["offset"] == 0 and reset["revision"] != before_timeline["revision"]
        old = saved[sources["ntfs.img"]["run_id"]]["items"][0]
        response = client.get(f"/api/evidence/{old['evidence_id']}/artifacts/{old['artifact_id']}/download")
        assert response.status_code == 422 and "current saved run only" in response.json()["detail"]

        keep_case = store.create_case("Preserve this case", "Independent original, parsed records and work files")
        keep_source = client.post(f"/api/cases/{keep_case['id']}/evidence", params={"filename": "NTUSER.DAT"},
                                  content=(directory / "NTUSER.DAT").read_bytes(), headers=HEADERS).json()["id"]
        keep_job = store.claim(LIMITS, analysis.PARSER_VERSION)
        keep_work = root / "work" / keep_job["run_id"]
        keep_work.mkdir()
        assert store.finish(keep_job, analysis.analyze(keep_job, LIMITS, str(keep_work)))
        protected = keep_work / "content-preserve"
        protected.mkdir()
        (protected / "sentinel").write_bytes(b"another case owns this")
        keep_records = store.records(keep_source, "", 0, 200)
        # Leave a genuine running job and disposable WAL set at the restart
        # boundary; only the active run's temporary copies may be removed.
        store.retry(sources["ntfs.img"]["id"])
        interrupted = store.claim(LIMITS, analysis.PARSER_VERSION)
        work = root / "work" / interrupted["run_id"]
        scratch = work / "content-interrupted"
        scratch.mkdir(parents=True)
        for suffix in ("", "-wal", "-shm"):
            (scratch / ("History" + suffix)).write_bytes(b"interrupted copy")
        (work / "content-link").symlink_to(protected, target_is_directory=True)
        assert client.delete(f"/api/cases/{case}", headers=HEADERS).status_code == 409

    # New application/coordinator: requeues the interrupted run and removes its
    # working set. A new successful attempt must preserve every historical row.
    with TestClient(create_app(Settings(root))) as client:
        wait_done(client, case)
        assert not scratch.exists() and not (work / "content-link").exists()
        assert (protected / "sentinel").read_bytes() == b"another case owns this"
        runs = client.get(f"/api/evidence/{sources['ntfs.img']['id']}/runs").json()
        assert next(run for run in runs if run["id"] == interrupted["run_id"])["status"] == "interrupted"
        for run, expected in saved.items():
            assert client.get(f"/api/cases/{case}/records/export", params={"run_id": run}).json() == expected
        response = client.delete(f"/api/cases/{case}", headers=HEADERS)
        assert response.status_code == 200 and response.json()["cleanup_pending"] == 0
        assert client.get(f"/api/cases/{case}/timeline").status_code == 404
        assert all(not (root / "evidence" / source["id"]).exists() for source in sources.values())
        assert {path.name for path in (root / "work").iterdir()} == {keep_job["run_id"]}
        assert (root / "evidence" / keep_source).read_bytes() == (directory / "NTUSER.DAT").read_bytes()
        assert client.get(f"/api/evidence/{keep_source}/records", params={"limit": 200}).json() == keep_records
        assert (protected / "sentinel").exists()
        with store.connect() as db:
            assert list(db.execute("PRAGMA foreign_key_check")) == []
            assert db.execute("SELECT COUNT(*) FROM parsed_records").fetchone()[0] == 12
        # Crash between metadata deletion and file cleanup is recovered on init.
        assert store.delete_case(keep_case["id"])
        assert (root / "evidence" / keep_source).exists()
    with TestClient(create_app(Settings(root), start_workers=False)) as client:
        assert not (root / "evidence" / keep_source).exists() and not keep_work.exists()
        assert client.get("/api/cases").json() == []


def test_performance_fixture_shape_and_worker_bounds(tmp_path):
    from scripts.benchmark_acceptance import benchmark

    report = benchmark(tmp_path)
    assert report["source_count"] == 5
    assert report["filesystem_events"] == 50000 and report["parsed_records"] == 10000
    assert report["worker"]["probes"][0]["saved_records"] == 10000
    path = tmp_path / "performance.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nPerformance report: {path}")
    # CI hardware is not the development machine. Report latency, never make
    # an otherwise correct CI run fail because a shared runner was slow.


def test_documented_import_command_builds_and_verifies_case(tmp_path, monkeypatch, capsys):
    import sys
    from scripts import import_demo

    missing = missing_tools()
    if missing:
        pytest.skip("Missing acceptance image tools: " + ", ".join(missing))
    # Exercise the CLI's actual generation, upload, polling and verification
    # through HTTP routes. TestClient supplies transport without a bound port.
    app = create_app(Settings(tmp_path / "workspace"))
    with TestClient(app) as client:
        def local_urlopen(request, timeout):
            if isinstance(request, str):
                response = client.get(request)
            else:
                response = client.request(request.method, request.full_url,
                                          content=request.data, headers=dict(request.header_items()))
            response.raise_for_status()
            return io.BytesIO(response.content)
        monkeypatch.setattr(import_demo, "urlopen", local_urlopen)
        monkeypatch.setattr(sys, "argv", ["scripts/import_demo.py", "--acceptance", "--url", "http://testserver",
                                         "--output", str(tmp_path / "demo-evidence")])
        import_demo.main()
        receipt_path = next((tmp_path / "demo-evidence").glob("acceptance-*/sources/import.json"))
        receipt = json.loads(receipt_path.read_text())
        assert len(receipt["sources"]) == 14
        assert len(client.get(f"/api/cases/{receipt['case_id']}/records/export").json()["items"]) == 469
    assert "Verified 14 sources and 469 records" in capsys.readouterr().out
