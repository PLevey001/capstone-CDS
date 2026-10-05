import hashlib
import json
import struct
from pathlib import Path

import pytest

import cds.analysis as analysis
from cds.config import CONTENT_LIMITS
from cds.registry_records import SCOPE
from cds.store import Store
from scripts.make_browser_demo import make_chrome_history
from tests.registry_fixtures import checksum, make_hive
from tests.test_image_history import CHROME, LIMITS, TSK, analyze_image, browser_image, summary

NTUSER = "/Users/examiner/NTUSER.DAT"
SYSTEM = "/Windows/System32/config/SYSTEM"
SOFTWARE = "/Windows/System32/config/SOFTWARE"


@pytest.fixture
def hives():
    return {path: make_hive(path.rsplit("/", 1)[-1])[0] for path in (NTUSER, SYSTEM, SOFTWARE)}


@TSK
def test_image_hives_save_inner_provenance_and_only_keys_on_timeline(tmp_path, hives):
    store = Store(tmp_path / "workspace")
    store.initialize()
    case = store.create_case("Image Registry", "")
    source = store.root / "evidence" / "image"
    image = browser_image(hives, partitions=2)
    source.write_bytes(image)
    store.register("image", case["id"], "registry.img", len(image), source, "raw_image", 512)
    job = store.claim(LIMITS, analysis.PARSER_VERSION)
    work = store.root / "work" / job["run_id"]
    work.mkdir()
    result = analysis.analyze(job, LIMITS, str(work))
    assert "error" not in result and store.finish(job, result)
    records = store.records("image", "", 0, 100)["items"]
    assert len(records) == 72 and len({row["artifact_id"] for row in records}) == 6
    assert summary(result)["parsed"] == summary(result)["candidates_found"] == 6
    assert summary(result)["sidecars_absent"] == 0 and result["coverage"]["status"] == "complete"
    for row in records:
        artifact = store.artifact("image", row["artifact_id"])
        assert artifact["partition_offset"] in (2048, 4928)
        assert artifact["path"] in hives and artifact["kind"] == "file"
        content = artifact["details"]["content"]
        assert content["sha256"] == hashlib.sha256(hives[artifact["path"]]).hexdigest()
        assert content["parser"] == row["parser"] == "windows-registry/1"
        assert content["run_id"] == row["run_id"] == job["run_id"]
        assert content["evidence_id"] == row["evidence_id"] == "image"
        assert "sidecars" not in content
        assert analysis.extract_artifact(store.extraction_target("image", artifact["id"])) == hives[artifact["path"]]
    events = store.timeline_rows(case["id"])["events"]
    events = [event for event in events if event["origin"] == "record"]
    assert len(events) == 18 and all(event["timestamp_kind"] == "registry_key" for event in events)
    assert all(event["artifact_path"] in hives for event in events)
    assert all(event["at"] == "2023-11-14T22:13:20.123456+00:00" for event in events)
    assert SCOPE in result["coverage"]["scope"] and not list(work.glob("content-*"))
    assert source.read_bytes() == image
    store.retry("image")
    second = store.claim()
    assert store.finish(second, result)
    assert store.records("image", "", 0, 100, job["run_id"])["items"] == records
    store.initialize()
    assert store.records("image", "", 0, 100, job["run_id"])["items"] == records


@TSK
def test_mixed_candidates_continue_after_dirty_and_non_hive_files(tmp_path, hives):
    dirty = bytearray(hives[SYSTEM])
    struct.pack_into("<I", dirty, 4, 2)
    checksum(dirty)
    files = {**hives, SYSTEM: dirty, SOFTWARE: b"not a hive", SYSTEM + ".LOG1": b"unexamined log",
             CHROME: make_chrome_history(tmp_path / "History").read_bytes(), "/Documents/NTUSER.DAT": hives[NTUSER]}
    result = analyze_image(tmp_path, browser_image(files))
    assert len(result["records"]) == 15
    steps = {step["label"]: step for step in result["coverage"]["steps"]}
    assert steps[SYSTEM]["reason"] == "hive_recovery_required" and "gap" in steps[SYSTEM]["detail"]
    assert steps[SOFTWARE]["status"] == "unsupported" and steps[SOFTWARE]["reason"] == "not_registry_hive"
    assert steps[NTUSER]["status"] == "complete" and steps[CHROME]["status"] == "complete"
    assert summary(result)["candidates_found"] == 4 and summary(result)["parsed"] == 2
    assert summary(result)["failed"] == summary(result)["unsupported"] == 1
    assert result["coverage"]["status"] == "partial" and summary(result)["sidecars_absent"] == 1
    assert all("content" not in item["details"] for item in result["artifacts"] if item["path"].endswith(".LOG1"))


@TSK
@pytest.mark.parametrize("limit,value,reason", [
    ("image_content_candidates", 1, "image_candidate_limit"),
    ("image_content_timeout", 0, "image_content_timeout"),
    ("image_content_bytes", 8192, "image_content_limit"),
    ("content_file_bytes", 10, "content_file_limit"),
    ("parsed_records", 13, "record_limit"),
    ("record_payload_bytes", 2000, "record_payload_limit"),
])
def test_registry_and_browser_share_image_budgets(tmp_path, hives, monkeypatch, limit, value, reason):
    limits = analysis.IMAGE_CONTENT_LIMITS if limit.startswith("image_") else CONTENT_LIMITS
    monkeypatch.setitem(limits, limit, value)
    files = {NTUSER: hives[NTUSER], CHROME: make_chrome_history(tmp_path / "History").read_bytes(), SYSTEM: hives[SYSTEM]}
    result = analyze_image(tmp_path, browser_image(files))
    assert summary(result)["budget_stops"] > 0 and summary(result)["status"] == "partial"
    assert any(step["reason"] == reason for step in result["coverage"]["steps"])
    assert len(result["records"]) <= CONTENT_LIMITS["parsed_records"]
    size = 2 + sum(len(json.dumps(row, ensure_ascii=True).encode()) + 2 for row in result["records"])
    assert size <= CONTENT_LIMITS["record_payload_bytes"]
    if limit == "parsed_records":
        assert sum(row["kind"].startswith("registry_") for row in result["records"]) == 12
        assert sum(row["kind"] == "browser_visit" for row in result["records"]) == 1


@TSK
@pytest.mark.parametrize("phase", ["extract", "short_read", "parser"])
def test_registry_failure_preserves_completed_candidates_and_cleans_copies(tmp_path, hives, monkeypatch, phase):
    icat = analysis._icat
    capture = analysis._capture
    def fail_extract(path, sector, artifact, timeout, max_bytes):
        if artifact["path"] == SYSTEM:
            if phase == "extract":
                raise ValueError("Synthetic extraction failure")
            if phase == "short_read":
                return hives[SYSTEM][:-1]
        return icat(path, sector, artifact, timeout, max_bytes)
    def fail_parser(args, timeout, max_bytes):
        if phase == "parser" and "cds.content_parser" in args:
            if Path(args[4]).read_bytes() == hives[SYSTEM]:
                raise analysis.ToolLimitError("tool_timeout", "Synthetic parser timeout")
        return capture(args, timeout, max_bytes)
    monkeypatch.setattr(analysis, "_icat", fail_extract)
    monkeypatch.setattr(analysis, "_capture", fail_parser)
    result = analyze_image(tmp_path, browser_image(hives))
    assert summary(result)["failed"] == 1 and summary(result)["parsed"] == 2
    assert len(result["records"]) == 24


@TSK
def test_truncated_inventory_qualifies_registry_coverage(tmp_path, hives):
    image = browser_image(hives)
    full = analyze_image(tmp_path, image)
    limit = next(index + 1 for index, row in enumerate(full["artifacts"]) if row["path"] == NTUSER)
    result = analyze_image(tmp_path, image, max_artifacts=limit)
    assert len(result["records"]) == 12
    assert summary(result)["status"] == "partial" and summary(result)["reason"] == "inventory_incomplete"
    assert "additional candidates may be missing" in summary(result)["detail"]
