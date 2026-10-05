import hashlib
import json
import struct
from pathlib import Path

import pytest

import cds.analysis as analysis
from cds.config import CONTENT_LIMITS
from cds.evtx_records import SCOPE
from cds.store import Store
from scripts.make_browser_demo import make_chrome_history
from tests.evtx_fixtures import EVENTS, chunk_checksums, make_evtx
from tests.registry_fixtures import make_hive
from tests.test_image_history import CHROME, LIMITS, TSK, analyze_image, browser_image, summary
from tests.test_image_registry import NTUSER

APPLICATION = "/Windows/System32/winevt/Logs/Application.evtx"
SYSTEM = "/Windows/System32/winevt/Logs/System.evtx"
RENAMED = "/wInNt/sYsTeM32/WINEVT/lOgS/renamed.bin"


@TSK
def test_image_logs_preserve_all_records_across_logs_and_partitions(tmp_path):
    files = {path: make_evtx()[0] for path in (APPLICATION, SYSTEM, RENAMED)}
    store = Store(tmp_path / "workspace")
    store.initialize()
    case = store.create_case("Image events", "")
    source = store.root / "evidence" / "image"
    image = browser_image(files, partitions=2)
    source.write_bytes(image)
    store.register("image", case["id"], "events.img", len(image), source, "raw_image", 512)
    job = store.claim(LIMITS, analysis.PARSER_VERSION)
    work = store.root / "work" / job["run_id"]
    work.mkdir()
    result = analysis.analyze(job, LIMITS, str(work))
    assert "error" not in result and store.finish(job, result)
    records = store.records("image", "", 0, 100)["items"]
    assert len(records) == 18 and len({row["artifact_id"] for row in records}) == 6
    assert summary(result)["parsed"] == summary(result)["candidates_found"] == 6
    assert summary(result)["sidecars_absent"] == 0 and result["coverage"]["status"] == "complete"
    assert sum(row["details"]["event_id"] == "1001" for row in records) == 12
    for row in records:
        artifact = store.artifact("image", row["artifact_id"])
        assert artifact["partition_offset"] in (2048, 4928) and artifact["path"] in files
        content = artifact["details"]["content"]
        assert content["sha256"] == hashlib.sha256(files[artifact["path"]]).hexdigest()
        assert content["parser"] == row["parser"] == "windows-event-log/1"
        assert content["run_id"] == row["run_id"] == job["run_id"]
        assert content["evidence_id"] == row["evidence_id"] == "image" and "sidecars" not in content
        assert analysis.extract_artifact(store.extraction_target("image", artifact["id"])) == files[artifact["path"]]
    events = [event for event in store.timeline_rows(case["id"])["events"] if event["origin"] == "record"]
    assert len(events) == 18 and len({event["id"] for event in events}) == 18
    for event in events:
        record = store.record("image", event["record_id"])
        assert record["artifact_id"] == event["artifact_id"] and record["artifact_path"] == event["artifact_path"]
        assert event["timestamp_label"] == "Event creation time" and event["timestamp_kind"] == "windows_event"
        assert event["at"] == next(row["at"] for row in EVENTS if row["record_id"] == record["details"]["event_record_id"])
    assert len(store.export_records(case["id"])["items"]) == 18
    assert SCOPE in result["coverage"]["scope"] and not list(work.glob("content-*"))
    assert source.read_bytes() == image


@TSK
def test_partial_and_failed_logs_do_not_discard_browser_hive_or_other_logs(tmp_path):
    data, manifest = make_evtx()
    partial = bytearray(data)
    partial[manifest["offsets"][1]] = 0
    chunk_checksums(partial)
    broken = bytearray(data)
    struct.pack_into("<I", broken, manifest["offsets"][0] + 4, 0)
    chunk_checksums(broken)
    files = {APPLICATION: partial, SYSTEM: broken, RENAMED: data,
             "/Windows/System32/winevt/Logs/Imposter.evtx": b"not EVTX",
             CHROME: make_chrome_history(tmp_path / "History").read_bytes(), NTUSER: make_hive()[0],
             "/Documents/Unexamined.evtx": data}
    result = analyze_image(tmp_path, browser_image(files))
    steps = {step["label"]: step for step in result["coverage"]["steps"]}
    assert steps[APPLICATION]["status"] == "partial" and steps[APPLICATION]["processed"] == 2
    assert steps[APPLICATION]["counts"]["malformed_records"] == 1 and steps[APPLICATION]["total"] == 3
    assert steps[SYSTEM]["status"] == "failed" and steps[SYSTEM]["reason"] == "evtx_unreadable"
    assert steps[RENAMED]["status"] == steps[CHROME]["status"] == steps[NTUSER]["status"] == "complete"
    assert len(result["records"]) == 20 and result["coverage"]["status"] == "partial"
    assert summary(result)["candidates_found"] == 6 and summary(result)["parsed"] == 4
    assert summary(result)["failed"] == summary(result)["unsupported"] == 1
    assert "/Documents/Unexamined.evtx" not in steps


@TSK
@pytest.mark.parametrize("limit,value,reason", [
    ("image_content_candidates", 1, "image_candidate_limit"),
    ("image_content_timeout", 0, "image_content_timeout"),
    ("image_content_bytes", 69632, "image_content_limit"),
    ("content_file_bytes", 10, "content_file_limit"),
    ("parsed_records", 17, "record_limit"),
    ("record_payload_bytes", 4000, "record_payload_limit"),
])
def test_all_three_families_share_image_budgets(tmp_path, monkeypatch, limit, value, reason):
    limits = analysis.IMAGE_CONTENT_LIMITS if limit.startswith("image_") else CONTENT_LIMITS
    monkeypatch.setitem(limits, limit, value)
    files = {APPLICATION: make_evtx()[0], NTUSER: make_hive()[0],
             CHROME: make_chrome_history(tmp_path / "History").read_bytes()}
    result = analyze_image(tmp_path, browser_image(files))
    assert summary(result)["budget_stops"] > 0 and summary(result)["status"] == "partial"
    assert any(step["reason"] == reason for step in result["coverage"]["steps"])
    assert len(result["records"]) <= CONTENT_LIMITS["parsed_records"]
    size = 2 + sum(len(json.dumps(row, ensure_ascii=True).encode()) + 2 for row in result["records"])
    assert size <= CONTENT_LIMITS["record_payload_bytes"]
    if limit == "parsed_records":
        assert sum(row["kind"].startswith("registry_") for row in result["records"]) == 12
        assert sum(row["kind"] == "browser_visit" for row in result["records"]) == 3
        assert sum(row["kind"] == "windows_event" for row in result["records"]) == 2
    if limit == "image_content_timeout":
        step = next(step for step in result["coverage"]["steps"] if step["label"] == APPLICATION)
        assert SCOPE in step["detail"]


@TSK
@pytest.mark.parametrize("phase", ["extract", "short_read", "parser"])
def test_evtx_failures_preserve_completed_candidates_and_cleanup(tmp_path, monkeypatch, phase):
    first = make_evtx()[0]
    second = make_evtx([EVENTS[2]])[0]
    icat = analysis._icat
    capture = analysis._capture
    def fail_extract(path, sector, artifact, timeout, max_bytes):
        if artifact["path"] == SYSTEM:
            if phase == "extract":
                raise ValueError("Synthetic extraction failure")
            if phase == "short_read":
                return second[:-1]
        return icat(path, sector, artifact, timeout, max_bytes)
    def fail_parser(args, timeout, max_bytes):
        if phase == "parser" and "cds.content_parser" in args and Path(args[4]).read_bytes() == second:
            raise analysis.ToolLimitError("tool_timeout", "Synthetic parser timeout")
        return capture(args, timeout, max_bytes)
    monkeypatch.setattr(analysis, "_icat", fail_extract)
    monkeypatch.setattr(analysis, "_capture", fail_parser)
    result = analyze_image(tmp_path, browser_image({APPLICATION: first, SYSTEM: second, NTUSER: make_hive()[0]}))
    assert summary(result)["failed"] == 1 and summary(result)["parsed"] == 2
    assert len(result["records"]) == 15
    step = next(step for step in result["coverage"]["steps"] if step["label"] == SYSTEM)
    assert step["reason"] == ("tool_timeout" if phase == "parser" else "content_parser_error")


@TSK
def test_incomplete_inventory_qualifies_event_coverage(tmp_path):
    image = browser_image({APPLICATION: make_evtx()[0], SYSTEM: make_evtx()[0], NTUSER: make_hive()[0]})
    full = analyze_image(tmp_path, image)
    limit = next(index + 1 for index, row in enumerate(full["artifacts"]) if row["path"] == APPLICATION)
    result = analyze_image(tmp_path, image, max_artifacts=limit)
    assert sum(row["kind"] == "windows_event" for row in result["records"]) == 3
    assert summary(result)["status"] == "partial" and summary(result)["reason"] == "inventory_incomplete"
    assert "additional candidates may be missing" in summary(result)["detail"]
