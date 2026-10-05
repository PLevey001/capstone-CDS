import csv
import hashlib
import io
import json
import struct

import pytest
from Evtx.Evtx import Evtx
from fastapi.testclient import TestClient

import cds.analysis as analysis
import cds.evtx_records as evtx_records
from cds.config import CONTENT_LIMITS, Settings
from cds.evtx_records import SCOPE, TIME_NOTE, event_record, read_evtx
from cds.main import create_app
from tests.evtx_fixtures import EVENTS, NAMESPACE, chunk_checksums, file_checksum, make_evtx
from tests.test_chrome_history import analyze_file

HEADERS = {"X-CDS-Request": "local-ui"}


def parse(path, **limits):
    return read_evtx(path, {**CONTENT_LIMITS, **limits})


@pytest.fixture
def log(tmp_path):
    data, manifest = make_evtx()
    path = tmp_path / "Application.evtx"
    path.write_bytes(data)
    return path, manifest


@pytest.mark.parametrize("namespace,prefix", [(True, ""), (True, "e:"), (False, "")])
def test_records_match_independent_manifest_and_dependency(tmp_path, namespace, prefix):
    data, manifest = make_evtx(namespace=namespace, prefix=prefix)
    path = tmp_path / "event-log"
    path.write_bytes(data)
    result = parse(path)
    assert result["status"] == "complete" and result["total"] == result["processed"] == 3
    assert result["counts"] == {"examined_records": 3, "malformed_records": 0, "corrupt_chunks": 0,
                                "shortened_records": 0, "missing_times": 0}
    for row, expected, offset in zip(result["records"], manifest["events"], manifest["offsets"]):
        detail = row["details"]
        for name in ("provider", "channel", "event_id", "level", "computer"):
            assert detail[name] == expected[name]
        assert detail["event_record_id"] == detail["record_number"] == expected["record_id"]
        assert detail["original_timestamp"] == expected["time"] and detail["timestamp_note"] == TIME_NOTE
        assert row["event_time_us"] == expected["event_time_us"]
        assert row["source_key"] == f"record:{expected['record_id']}@{offset:x}"
        assert row["kind"] == "windows_event" and row["artifact_key"] == "source"
        nodes = detail["event_data"][1:]
        assert [(dict(node["attributes"]).get("Name"), node["text"] or "") for node in nodes] == expected["data"]
        assert all(node["parent"] == 0 for node in nodes)
        assert all(node["tag"] == ("{" + NAMESPACE + "}" if namespace else "") + "Data" for node in nodes)
        assert detail["user_data"] is None and "message" not in detail
    with Evtx(str(path)) as source:
        assert source.get_file_header().verify() and all(chunk.verify() for chunk in source.chunks())
        originals = list(source.records())
        assert [row.record_num() for row in originals] == [41, 42, 43]
        assert [row.xml() for row in originals] == [row["details"]["raw_xml"] for row in result["records"]]
    assert path.read_bytes() == data and SCOPE in result["detail"]


@pytest.mark.parametrize("name", ["renamed.bin", "misleading.sqlite", "NTUSER.DAT", "Application.evtx"])
def test_direct_subprocess_recognizes_structure_and_preserves_source(log, tmp_path, name):
    path, _ = log
    renamed = path.with_name(name)
    path.rename(renamed)
    before = renamed.read_bytes()
    result = analyze_file(renamed, tmp_path / "work")
    assert "error" not in result and len(result["records"]) == 3
    assert result["sha256"] == hashlib.sha256(before).hexdigest()
    step = result["coverage"]["steps"][-1]
    assert step["status"] == "complete" and step["id"] == "windows-event-log" and step["counts"]["examined_records"] == 3
    assert result["metadata"]["format"] == "Windows event log"
    assert result["records"][0]["artifact_key"] == result["artifacts"][0]["key"]
    assert SCOPE in result["coverage"]["scope"] and SCOPE in result["warnings"]
    assert renamed.read_bytes() == before and not list((tmp_path / "work").glob("content-*"))


def test_extension_is_only_a_candidate_hint_and_empty_is_distinct(tmp_path):
    path = tmp_path / "System.evtx"
    path.write_bytes(b"not an event log")
    result = analyze_file(path, tmp_path / "work")
    step = result["coverage"]["steps"][-1]
    assert step["status"] == "unsupported" and step["reason"] == "not_evtx" and step["total"] is None
    assert not result["records"] and result["coverage"]["status"] == "partial"
    path.write_bytes(make_evtx([])[0])
    empty = parse(path)
    assert empty["status"] == "complete" and empty["processed"] == empty["total"] == 0


@pytest.mark.parametrize("original,expected", [(None, None), ("", None), ("bad", None),
    ("2023-11-14T22:13:20", None), ("2023-02-30T01:00:00Z", None), ("10000-01-01T00:00:00Z", None),
    ("0001-01-01T00:00:00+01:00", None), ("2023-11-14T22:13:20+00:60", None),
    ("2023-11-14T22:13:20+15:00", None), ("1970-01-01T00:00:00Z", 0),
    ("1969-12-31T23:59:59.9999999Z", -1), ("2023-11-14T22:13:20.1234567Z", 1700000000123456)])
def test_creation_time_keeps_raw_form_and_never_uses_header_time(tmp_path, original, expected):
    path = tmp_path / "log"
    path.write_bytes(make_evtx([{**EVENTS[0], "time": original}])[0])
    result = parse(path)
    row = result["records"][0]
    assert row["event_time_us"] == expected and row["details"]["original_timestamp"] == original
    assert result["status"] == ("partial" if expected is None else "complete")
    assert result["counts"]["missing_times"] == int(expected is None)
    assert "fallback" in row["details"]["timestamp_note"]


def test_missing_fields_are_distinct_from_empty_and_zero(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(make_evtx([{"record_id": None, "provider": None, "channel": "", "level": "0", "data": None}])[0])
    detail = parse(path)["records"][0]["details"]
    assert detail["provider"] is detail["event_id"] is detail["computer"] is detail["event_record_id"] is None
    assert detail["event_data"] is detail["original_timestamp"] is None
    assert detail["channel"] == "" and detail["level"] == "0" and detail["record_number"] == "1"


@pytest.mark.parametrize("original,expected", [(133_444_736_001_234_567, 1700000000123456),
    (116_444_736_000_000_000, 0), (116_444_735_999_999_990, -1), (0, -11644473600000000), (2**64 - 1, None)])
def test_binary_creation_time_preserves_ticks_without_float_rounding(tmp_path, original, expected):
    path = tmp_path / "log"
    path.write_bytes(make_evtx([{**EVENTS[0], "filetime": original}], prefix="e:")[0])
    result = parse(path)
    row = result["records"][0]
    assert row["event_time_us"] == expected and row["details"]["original_filetime"] == str(original)
    assert result["status"] == ("partial" if expected is None else "complete")
    assert "binary FILETIME" in row["details"]["timestamp_note"]
    with Evtx(str(path)) as source:
        xml = next(source.records()).xml()
    assert row["details"]["raw_xml"] == xml
    assert row["details"]["original_timestamp"] in xml


@pytest.mark.parametrize("namespace,expected", [(NAMESPACE, 1700000000123456), ("urn:foreign", None)])
def test_binary_time_resolves_namespace_and_does_not_adopt_lookalike_elements(tmp_path, namespace, expected):
    path = tmp_path / "log"
    path.write_bytes(make_evtx([{**EVENTS[0], "filetime": 133_444_736_001_234_567,
                                 "time_namespace": namespace}], prefix="e:")[0])
    result = parse(path)
    row = result["records"][0]
    assert row["event_time_us"] == expected
    assert ("original_filetime" in row["details"]) == (expected is not None)
    assert result["status"] == ("complete" if expected is not None else "partial")
    if expected is None:
        assert row["details"]["original_timestamp"] is None


def test_repeated_event_and_record_ids_do_not_collapse(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(make_evtx([EVENTS[0]] * 4, records_per_chunk=2)[0])
    result = parse(path)
    assert result["status"] == "complete" and result["processed"] == 4
    assert len({row["source_key"] for row in result["records"]}) == 4
    assert {row["details"]["event_id"] for row in result["records"]} == {"1001"}
    assert {row["details"]["event_record_id"] for row in result["records"]} == {"41"}


def test_long_fields_and_structured_data_have_visible_bounds(tmp_path):
    path = tmp_path / "log"
    event = {**EVENTS[0], "provider": "P" * 300, "computer": "C" * 300,
             "data": [("long", "東京" * 500)], "user_data": "U" * 500}
    path.write_bytes(make_evtx([event])[0])
    result = parse(path, record_text_chars=120)
    row = result["records"][0]
    detail = row["details"]
    assert result["status"] == "partial" and result["reason"] == "record_quality"
    assert detail["provider"] == "P" * 120 and detail["computer"] == "C" * 120
    assert {"raw_xml", "provider", "computer", "event_data", "user_data", "summary"} <= set(detail["truncated_fields"])
    assert len(detail["raw_xml"]) == len(row["summary"]) == 120
    assert result["counts"]["shortened_records"] == 1 and "shortened" in result["detail"]
    full = parse(path)["records"][0]["details"]
    assert full["user_data"][1]["tag"] == "{urn:synthetic}Payload"
    assert full["user_data"][1]["text"] == "U" * 500 and full["user_data"][1]["parent"] == 0


@pytest.mark.parametrize("damage", ["magic", "binxml", "malformed_xml"])
def test_safely_bounded_bad_record_is_counted_and_later_records_survive(log, damage):
    path, manifest = log
    data = bytearray(path.read_bytes())
    offset = manifest["offsets"][1]
    if damage == "magic":
        data[offset] = 0
    elif damage == "binxml":
        struct.pack_into("<I", data, offset + 34, 0xFFFFFFF0)
    else:
        # Quotes are not escaped by this version's literal attribute renderer; report invalid XML.
        before = "Synthetic-Provider".encode("utf-16le")
        start = data.index(before, offset)
        data[start:start + 2] = b'"\0'
    chunk_checksums(data)
    path.write_bytes(data)
    result = parse(path)
    assert result["status"] == "partial" and result["reason"] == "evtx_corruption"
    assert result["processed"] == 2 and result["total"] == 3
    assert result["counts"]["malformed_records"] == 1 and result["counts"]["examined_records"] == 3
    assert [row["details"]["event_record_id"] for row in result["records"]] == ["41", "43"]
    assert "skipped" in result["detail"] and "1 malformed records" in result["detail"]


@pytest.mark.parametrize("damage", ["size", "footer", "last_offset", "count"])
def test_unsafe_structure_stops_log_without_discarding_completed_records(log, damage):
    path, manifest = log
    data = bytearray(path.read_bytes())
    offset = manifest["offsets"][1]
    if damage == "size":
        struct.pack_into("<I", data, offset + 4, 0)
    elif damage == "footer":
        size = struct.unpack_from("<I", data, offset + 4)[0]
        struct.pack_into("<I", data, offset + size - 4, size - 8)
    elif damage == "last_offset":
        struct.pack_into("<I", data, 4096 + 44, 512)
    else:
        struct.pack_into("<Q", data, 4096 + 16, 4)
    chunk_checksums(data)
    path.write_bytes(data)
    result = parse(path)
    assert result["status"] == "partial" and result["reason"] == "evtx_unreadable"
    assert result["total"] is None and result["processed"] == (1 if damage in ("size", "footer") else 3)
    assert "Parser stopped" in result["detail"] and "Remaining records were not examined" in result["detail"]


@pytest.mark.parametrize("damage", ["checksum", "header", "extent"])
def test_bad_chunk_is_visible_and_other_chunks_survive(tmp_path, damage):
    data, _ = make_evtx(records_per_chunk=1)
    data = bytearray(data)
    offset = 4096 + 65536
    if damage == "checksum":
        data[offset + 600] ^= 1
    elif damage == "header":
        data[offset] = 0
    else:
        struct.pack_into("<I", data, offset + 48, 65537)
    path = tmp_path / "log"
    path.write_bytes(data)
    result = parse(path)
    assert result["status"] == "partial" and result["reason"] == "evtx_corruption"
    assert result["counts"]["corrupt_chunks"] == 1 and result["counts"]["malformed_records"] == 0
    assert result["total"] is None and result["processed"] == 2
    assert [row["details"]["event_record_id"] for row in result["records"]] == ["41", "43"]


def test_truncated_declared_chunk_does_not_look_like_clean_eof(tmp_path):
    data, _ = make_evtx(records_per_chunk=1)
    path = tmp_path / "log"
    path.write_bytes(data[:-1])
    result = parse(path)
    assert result["status"] == "partial" and result["processed"] == 2 and result["total"] is None
    assert "truncated or missing" in result["detail"]


def test_chunk_record_counts_cannot_silently_hide_every_record(log):
    path, _ = log
    data = bytearray(path.read_bytes())
    struct.pack_into("<II", data, 4096 + 44, 0, 512)
    chunk_checksums(data)
    path.write_bytes(data)
    result = parse(path)
    assert result["status"] == "failed" and result["reason"] == "evtx_unreadable"
    assert result["counts"]["corrupt_chunks"] == 1 and result["total"] is None
    assert "inconsistent record counts" in result["detail"]


def test_full_width_chunk_count_cannot_silently_hide_missing_chunks(log):
    path, _ = log
    data = bytearray(path.read_bytes())
    struct.pack_into("<I", data, 42, 65537)
    file_checksum(data)
    path.write_bytes(data)
    result = parse(path)
    assert result["status"] == "partial" and result["processed"] == 3 and result["total"] is None
    assert result["counts"]["corrupt_chunks"] == 1 and "truncated or missing" in result["detail"]


def test_archive_zero_padding_is_not_a_missing_record(log):
    path, _ = log
    data = bytearray(path.read_bytes())
    struct.pack_into("<I", data, 4096 + 48, 65536)
    chunk_checksums(data)
    path.write_bytes(data)
    result = parse(path)
    assert result["status"] == "complete" and result["total"] == result["processed"] == 3


@pytest.mark.parametrize("damage", ["header", "checksum", "version", "dirty"])
def test_header_problems_never_look_like_clean_logs(log, damage):
    path, _ = log
    data = bytearray(path.read_bytes())
    if damage == "header":
        data = data[:100]
    elif damage == "checksum":
        data[124] ^= 1
    elif damage == "version":
        struct.pack_into("<H", data, 38, 9)
        file_checksum(data)
    else:
        struct.pack_into("<I", data, 120, 1)
    path.write_bytes(data)
    result = parse(path)
    assert result["status"] == ("unsupported" if damage == "version" else "partial" if damage == "dirty" else "failed")
    assert result["total"] is None and len(result["records"]) == (3 if damage == "dirty" else 0)


def test_record_payload_file_and_time_bounds_are_explicit(log, monkeypatch):
    path, _ = log
    complete = parse(path)
    limited = parse(path, parsed_records=2)
    assert limited["records"] == complete["records"][:2]
    assert limited["reason"] == "record_limit" and limited["total"] is None
    size = 2 + sum(len(json.dumps(row, ensure_ascii=True).encode()) + 2 for row in complete["records"][:2])
    limited = parse(path, record_payload_bytes=size)
    assert limited["records"] == complete["records"][:2] and limited["reason"] == "record_payload_limit"
    assert not parse(path, record_payload_bytes=2)["records"]
    assert parse(path, parsed_records=3)["status"] == "complete"
    assert parse(path, content_file_bytes=10)["reason"] == "content_file_limit"
    ticks = iter([0, 0, 0, 91])
    monkeypatch.setattr(evtx_records.time, "monotonic", lambda: next(ticks))
    limited = parse(path)
    assert limited["reason"] == "content_timeout" and limited["status"] == "partial" and limited["total"] is None
    assert limited["records"] == complete["records"][:1]


def test_unexpected_decoder_failure_retains_completed_work(log, monkeypatch):
    path, _ = log
    decode = evtx_records.Evtx.Record.xml
    def fail(record):
        if record.record_num() == 42:
            raise RuntimeError("Synthetic decoder failure")
        return decode(record)
    monkeypatch.setattr(evtx_records.Evtx.Record, "xml", fail)
    result = parse(path)
    assert result["status"] == "partial" and result["reason"] == "evtx_unreadable"
    assert result["processed"] == 1 and result["total"] is None and "Synthetic decoder failure" in result["detail"]


@pytest.mark.parametrize("failure", [analysis.ToolLimitError("tool_timeout", "Timed out"), ValueError("Parser failed")])
def test_direct_parser_failure_preserves_hash_and_cleans_copy(log, tmp_path, monkeypatch, failure):
    path, _ = log
    def fail(*args):
        raise failure
    monkeypatch.setattr(analysis, "_capture", fail)
    result = analyze_file(path, tmp_path / "work")
    assert "error" not in result and result["sha256"] and len(result["artifacts"]) == 1
    step = result["coverage"]["steps"][-1]
    assert step["status"] == "failed" and step["reason"] == getattr(failure, "reason", "content_parser_error")
    assert result["coverage"]["status"] == "partial" and not result["records"]
    assert not list((tmp_path / "work").glob("content-*"))


def test_xml_entities_are_not_expanded():
    with pytest.raises(ValueError, match="DTD"):
        event_record('<!DOCTYPE Event [<!ENTITY field "invented">]><Event><System>&field;</System></Event>', 1, 512, 4096)


def test_upload_exports_navigation_ownership_history_and_xml_text(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "workspace", workers=1), start_workers=False)
    script = '<img src=x onerror=alert(1)><script>alert("evidence")</script>'
    events = [{**EVENTS[0], "provider": "<script>alert(1)</script>", "data": [("Payload", script)]},
              {**EVENTS[1], "time": None}, {**EVENTS[2], "time": "bad"}]
    data, _ = make_evtx(events)
    with TestClient(app) as client:
        store = app.state.store
        case = client.post("/api/cases", json={"name": "EVTX"}, headers=HEADERS).json()["id"]
        saved = []
        for name in ("Application.evtx", "Other.evtx"):
            upload = client.post(f"/api/cases/{case}/evidence", params={"filename": name}, content=data, headers=HEADERS)
            assert upload.status_code == 202
            source = upload.json()["id"]
            job = store.claim()
            work = store.root / "work" / job["run_id"]
            work.mkdir()
            result = analysis.analyze(job, {"tool_timeout": 5, "max_artifacts": 100}, str(work))
            assert store.finish(job, result)
            route = f"/api/evidence/{source}/records"
            original = client.get(route).json()
            assert original["total"] == 3
            assert [row["at"] for row in original["items"]] == [EVENTS[0]["at"], None, None]
            first = original["items"][0]
            assert first["details"]["event_data"][1]["text"] == script
            assert isinstance(first["details"]["raw_xml"], str) and first["details"]["raw_xml"].startswith("<Event ")
            detail_response = client.get(route + "/" + str(first["id"]))
            assert detail_response.headers["content-type"] == "application/json" and detail_response.json() == first
            saved.append((source, job, result, original))
        timeline = client.get(f"/api/cases/{case}/timeline").json()["events"]
        assert len(timeline) == 2 and len({event["id"] for event in timeline}) == 2
        for event in timeline:
            assert event["at"] == EVENTS[0]["at"] and event["timestamp_label"] == "Event creation time"
            assert event["timestamp_kind"] == "windows_event"
            record = client.get(f"/api/evidence/{event['source_id']}/records/{event['record_id']}").json()
            assert event["artifact_id"] == record["artifact_id"] and event["source_name"] == record["artifact_path"]
        export_route = f"/api/cases/{case}/records/export"
        exported = client.get(export_route)
        assert exported.headers["content-type"] == "application/json"
        items = exported.json()["items"]
        assert len(items) == 6 and len({row["id"] for row in items}) == 6
        assert sum(row["details"]["event_id"] == "1001" for row in items) == 4
        csv_response = client.get(export_route, params={"format": "csv"})
        assert csv_response.headers["content-type"].startswith("text/csv")
        csv_rows = list(csv.DictReader(io.StringIO(csv_response.text)))
        assert len(csv_rows) == 6
        assert json.loads(csv_rows[0]["details"])["raw_xml"] == items[0]["details"]["raw_xml"]
        source, job, result, original = saved[0]
        other = client.post("/api/cases", json={"name": "Other case"}, headers=HEADERS).json()["id"]
        assert client.get(f"/api/cases/{other}/records/export", params={"run_id": job["run_id"]}).status_code == 404
        assert client.get(f"/api/evidence/{saved[1][0]}/records/{original['items'][0]['id']}").status_code == 404
        store.retry(source)
        second = store.claim()
        result["records"] = result["records"][:1]
        assert store.finish(second, result)
        route = f"/api/evidence/{source}/records"
        assert client.get(route).json()["total"] == 1
        store.initialize()
        assert client.get(route, params={"run_id": job["run_id"]}).json() == original
        assert store.detail(source)["sha256"] == hashlib.sha256(data).hexdigest()
