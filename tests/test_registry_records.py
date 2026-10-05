import hashlib
import json
import struct

import pytest
from fastapi.testclient import TestClient
from Registry import Registry

import cds.analysis as analysis
from cds.config import CONTENT_LIMITS, Settings
from cds.main import create_app
from cds.registry_records import KEY_TIME_NOTE, SCOPE, VALUE_TIME_NOTE, read_hive
from tests.registry_fixtures import FILETIME, KEY_PATH, VALUES, checksum, make_hive
from tests.test_chrome_history import analyze_file

HEADERS = {"X-CDS-Request": "local-ui"}


def parse(path, **limits):
    return read_hive(path, {**CONTENT_LIMITS, **limits})


@pytest.fixture
def hive(tmp_path):
    data, manifest = make_hive()
    path = tmp_path / "NTUSER.DAT"
    path.write_bytes(data)
    return path, manifest


@pytest.mark.parametrize("name", ["NTUSER.DAT", "SYSTEM", "SOFTWARE"])
def test_typed_records_match_source_manifest_and_library(tmp_path, name):
    data, manifest = make_hive(name)
    path = tmp_path / name
    path.write_bytes(data)
    result = parse(path)
    assert result["status"] == "complete" and result["total"] == result["processed"] == 12
    keys = [row for row in result["records"] if row["kind"] == "registry_key"]
    values = [row for row in result["records"] if row["kind"] == "registry_value"]
    assert [row["details"]["key_path"] for row in keys] == manifest["keys"]
    assert all(row["event_time_us"] == manifest["event_time_us"] for row in keys)
    assert all(row["details"]["original_timestamp"] == manifest["original_timestamp"] for row in keys)
    assert all(row["details"]["timestamp_note"] == KEY_TIME_NOTE for row in keys)
    for row, expected in zip(values, manifest["values"]):
        detail = row["details"]
        expected_data = expected["data"]
        if isinstance(expected_data, int) and abs(expected_data) > 2**53 - 1:
            expected_data = str(expected_data)
            assert detail["integer_text_fields"] == ["data"]
        assert detail["data"] == expected_data
        assert detail["value_name"] == expected["name"] and detail["value_type"] == expected["type"]
        assert detail["value_type_name"].startswith("REG_")
        assert detail["byte_length"] == expected["byte_length"] and not detail["data_truncated"]
        assert detail["is_default"] == (expected["name"] == "")
        assert detail["key_source_key"] == keys[-1]["source_key"] and detail["key_path"] == KEY_PATH
        assert row["event_time_us"] is None and "original_timestamp" not in detail
        assert detail["timestamp_note"] == VALUE_TIME_NOTE
    # The independent high-level API also opens this builder's ordinary hive structure.
    with path.open("rb") as source:
        key = Registry.Registry(source).open("Software\\研究")
        assert key.name() == "研究" and key.values_number() == len(VALUES)
    assert path.read_bytes() == data
    assert SCOPE in result["detail"]


@pytest.mark.parametrize("name", ["renamed.bin", "misleading.sqlite", "NTUSER.DAT", "SYSTEM", "SOFTWARE"])
def test_direct_subprocess_is_structural_and_preserves_original(hive, tmp_path, name):
    path, _ = hive
    renamed = path.with_name(name)
    path.rename(renamed)
    before = renamed.read_bytes()
    result = analyze_file(renamed, tmp_path / "work")
    assert "error" not in result and len(result["records"]) == 12
    assert result["sha256"] == hashlib.sha256(before).hexdigest()
    assert result["coverage"]["steps"][-1]["status"] == "complete"
    assert result["coverage"]["steps"][-1]["id"] == "windows-registry"
    assert result["metadata"]["format"] == "Windows Registry"
    assert result["records"][0]["artifact_key"] == result["artifacts"][0]["key"]
    assert SCOPE in result["coverage"]["scope"] and SCOPE in result["warnings"]
    assert renamed.read_bytes() == before and not list((tmp_path / "work").glob("content-*"))


@pytest.mark.parametrize("name", ["NTUSER.DAT", "SYSTEM", "SOFTWARE"])
def test_non_hive_name_does_not_establish_recognition(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"not a hive")
    result = analyze_file(path, tmp_path / "work")
    step = result["coverage"]["steps"][-1]
    assert step["status"] == "unsupported" and step["reason"] == "not_registry_hive"
    assert result["coverage"]["status"] == "partial" and not result["records"]
    assert step["total"] is None


@pytest.mark.parametrize("damage", ["dirty", "checksum", "header", "truncated", "bin", "cell", "root", "value_data",
                                   "value_list", "subkey_count", "subkey_index", "cycle", "parent", "free_value", "utf16"])
def test_damaged_hive_never_silently_returns_a_prefix(hive, tmp_path, damage):
    path, manifest = hive
    data = bytearray(path.read_bytes())
    leaf = manifest["cells"]["研究"]
    value = manifest["cells"]["value:"]
    if damage == "dirty":
        struct.pack_into("<I", data, 4, 2)
        checksum(data)
    elif damage == "checksum":
        data[508] ^= 1
    elif damage == "header":
        data = data[:100]
    elif damage == "truncated":
        data = data[:-1]
    elif damage == "bin":
        data[4096] = 0
    elif damage == "cell":
        struct.pack_into("<i", data, 4128, 0)
    elif damage == "root":
        data[manifest["cells"]["ROOT"]] = 0
    elif damage == "value_data":
        struct.pack_into("<I", data, value + 4, 16000)
    elif damage == "value_list":
        struct.pack_into("<I", data, leaf + 40, 0xFFFFFFFF)
    elif damage == "subkey_count":
        struct.pack_into("<I", data, manifest["cells"]["ROOT"] + 20, 2)
    elif damage in ("subkey_index", "cycle"):
        root = manifest["cells"]["ROOT"]
        index = struct.unpack_from("<I", data, root + 28)[0] + 4100
        if damage == "subkey_index":
            struct.pack_into("<H", data, index + 2, 1000)
        else:
            struct.pack_into("<I", data, index + 4, root - 4100)
    elif damage == "parent":
        struct.pack_into("<I", data, leaf + 16, 0xFFFFFFFF)
    elif damage == "free_value":
        size = struct.unpack_from("<i", data, value - 4)[0]
        struct.pack_into("<i", data, value - 4, -size)
    elif damage == "utf16":
        offset = struct.unpack_from("<I", data, value + 8)[0] + 4100
        data[offset:offset + 2] = b"\x00\xdc"
    path.write_bytes(data)
    log = path.with_name(path.name + ".LOG1")
    log.write_bytes(b"out of scope transaction log")
    result = analyze_file(path, tmp_path / "work")
    step = result["coverage"]["steps"][-1]
    assert "error" not in result and step["status"] == "failed"
    assert step["reason"] == ("hive_recovery_required" if damage in ("dirty", "checksum") else "hive_unreadable")
    assert "gap" in step["detail"] and "not examined or replayed" in step["detail"]
    assert not result["records"] and step["processed"] == 0 and step["total"] is None
    assert result["coverage"]["status"] == "partial" and result["sha256"]
    assert log.read_bytes() == b"out of scope transaction log" and path.read_bytes() == data


@pytest.mark.parametrize("offset,value", [(20, 2), (24, 99), (28, 1), (32, 2), (44, 2)])
def test_unsupported_versions_and_log_inputs_are_explicit(hive, offset, value):
    path, _ = hive
    data = bytearray(path.read_bytes())
    struct.pack_into("<I", data, offset, value)
    checksum(data)
    path.write_bytes(data)
    result = parse(path)
    assert result["status"] == "unsupported" and result["reason"] == "unsupported_hive_format"
    assert not result["records"]


@pytest.mark.parametrize("original,expected", [(FILETIME, 1700000000123456), (116444736000000000, 0),
                                               (116444735999999990, -1), (0, -11644473600000000), (2**64 - 1, None)])
def test_key_time_precision_zero_and_missing_are_distinct(hive, original, expected):
    path, manifest = hive
    data = bytearray(path.read_bytes())
    struct.pack_into("<Q", data, manifest["cells"]["研究"] + 4, original)
    path.write_bytes(data)
    result = parse(path)
    key = next(row for row in result["records"] if row["details"]["key_path"] == KEY_PATH)
    assert key["event_time_us"] == expected and key["details"]["original_timestamp"] == str(original)
    assert result["status"] == ("complete" if expected is not None else "partial")
    assert all(row["event_time_us"] is None for row in result["records"] if row["kind"] == "registry_value")


def test_enumeration_and_payload_bounds_have_unknown_remaining_total(hive):
    path, _ = hive
    complete = parse(path)
    limited = parse(path, parsed_records=4)
    assert limited["records"] == complete["records"][:4]
    assert limited["reason"] == "record_limit" and limited["total"] is None
    size = 2 + sum(len(json.dumps(row, ensure_ascii=True).encode()) + 2 for row in complete["records"][:4])
    limited = parse(path, record_payload_bytes=size)
    assert limited["records"] == complete["records"][:4]
    assert limited["reason"] == "record_payload_limit" and limited["total"] is None
    assert not parse(path, record_payload_bytes=2)["records"]
    assert parse(path, parsed_records=12)["status"] == "complete"
    assert parse(path, content_file_bytes=10)["reason"] == "content_file_limit"


@pytest.mark.parametrize("kind,raw,expected", [
    (1, ("東京" * 10000 + "\0").encode("utf-16le"), "東京" * 20),
    (2, ("%PATH%" * 10000 + "\0").encode("utf-16le"), ("%PATH%" * 7)[:40]),
    (7, ("東京\0" * 10000 + "\0").encode("utf-16le"), ("東京\0" * 14)[:40].split("\0")),
    (3, bytes(range(256)) * 100, bytes(range(20)).hex()),
])
def test_segmented_values_and_whole_multistring_share_text_bound(tmp_path, kind, raw, expected):
    data, _ = make_hive(values=[("Long" * 20, kind, None, raw)])
    path = tmp_path / "hive"
    path.write_bytes(data)
    result = parse(path, record_text_chars=40)
    assert result["status"] == "partial" and result["reason"] == "record_quality"
    detail = result["records"][-1]["details"]
    assert detail["data"] == expected and detail["data_truncated"]
    assert detail["byte_length"] == len(raw) and "value_name" in detail["truncated_fields"]
    assert len(result["records"][-1]["summary"]) <= 40 and "shortened" in result["detail"]


@pytest.mark.parametrize("failure", [analysis.ToolLimitError("tool_timeout", "Timed out"), ValueError("Parser failed")])
def test_direct_parser_failure_preserves_hash_and_removes_copy(hive, tmp_path, monkeypatch, failure):
    path, _ = hive
    def fail(*args):
        raise failure
    monkeypatch.setattr(analysis, "_capture", fail)
    result = analyze_file(path, tmp_path / "work")
    assert "error" not in result and result["sha256"] and len(result["artifacts"]) == 1
    assert not result["records"] and result["coverage"]["steps"][-1]["status"] == "failed"
    assert not list((tmp_path / "work").glob("content-*"))


def test_upload_timeline_navigation_exports_ownership_and_historical_runs(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "workspace", workers=1), start_workers=False)
    data, manifest = make_hive()
    with TestClient(app) as client:
        store = app.state.store
        case = client.post("/api/cases", json={"name": "Registry records"}, headers=HEADERS).json()["id"]
        upload = client.post(f"/api/cases/{case}/evidence", params={"filename": "NTUSER.DAT"}, content=data, headers=HEADERS)
        assert upload.status_code == 202
        source = upload.json()["id"]
        job = store.claim()
        work = store.root / "work" / job["run_id"]
        work.mkdir()
        result = analysis.analyze(job, {"tool_timeout": 5, "max_artifacts": 100}, str(work))
        store.finish(job, result)
        route = f"/api/evidence/{source}/records"
        original = client.get(route).json()
        assert original["total"] == 12
        events = client.get(f"/api/cases/{case}/timeline").json()["events"]
        assert len(events) == 3
        for event in events:
            assert event["at"] == manifest["at"] and event["timestamp_label"] == "Registry key last-write"
            assert event["timestamp_kind"] == "registry_key" and event["source_name"] == "NTUSER.DAT"
            record = client.get(route + "/" + str(event["record_id"])).json()
            assert event["artifact_id"] == record["artifact_id"] and event["summary"] == record["details"]["key_path"]
        exported = client.get(f"/api/cases/{case}/records/export").json()
        assert len(exported["items"]) == 12 and all(row["at"] is None for row in exported["items"] if row["kind"] == "registry_value")
        other = client.post("/api/cases", json={"name": "Other"}, headers=HEADERS).json()["id"]
        assert client.get(f"/api/cases/{other}/records/export", params={"run_id": job["run_id"]}).status_code == 404
        assert client.get(f"/api/evidence/missing/records/{original['items'][0]['id']}").status_code == 404
        store.retry(source)
        second = store.claim()
        result["records"] = result["records"][:1]
        store.finish(second, result)
        assert client.get(route).json()["total"] == 1
        assert client.get(route, params={"run_id": job["run_id"]}).json() == original
        store.initialize()
        assert client.get(route, params={"run_id": job["run_id"]}).json() == original
        assert hashlib.sha256(data).hexdigest() == store.detail(source)["sha256"]
