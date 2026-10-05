import hashlib
import json
import shutil
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest

import cds.analysis as analysis
from cds.config import CONTENT_LIMITS
from cds.history_records import SNAPSHOT_SCOPE
from cds.store import Store
from scripts.make_browser_demo import make_chrome_history, make_firefox_history
from scripts.make_demo import fat12_image
from tests.test_coverage import fake_image_tools

CHROME = "/Users/examiner/AppData/Local/Google/Chrome/User Data/Default/History"
FIREFOX = "/Users/examiner/AppData/Roaming/Mozilla/Firefox/Profiles/demo.default/places.sqlite"
LIMITS = {"tool_timeout": 5, "max_artifacts": 200}
TSK = pytest.mark.skipif(any(not shutil.which(tool) for tool in ("mmls", "fls", "icat")),
                         reason="Install The Sleuth Kit for image browser tests")


def browser_image(files, partitions=1):
    """Build FAT12 directories, long names, and cluster chains without mounting anything."""
    disk = bytearray(fat12_image())
    disk[19 * 512:] = bytes(len(disk) - 19 * 512)
    fat = bytearray(9 * 512)
    fat[:3] = b"\xf0\xff\xff"
    next_cluster = 2

    def allocate(size):
        nonlocal next_cluster
        start = next_cluster
        count = max(1, (size + 511) // 512)
        next_cluster += count
        assert next_cluster < 2849
        for cluster in range(start, next_cluster):
            link = cluster + 1 if cluster + 1 < next_cluster else 0xFFF
            offset = cluster + cluster // 2
            value = int.from_bytes(fat[offset:offset + 2], "little")
            value = (value & 0x000F) | (link << 4) if cluster % 2 else (value & 0xF000) | link
            fat[offset:offset + 2] = value.to_bytes(2, "little")
        return start

    def entry(name, cluster, size, directory=False):
        row = bytearray(32)
        row[:11] = name
        row[11] = 0x10 if directory else 0x20
        struct.pack_into("<HI", row, 26, cluster, size)
        return row

    def long_name(name, alias):
        checksum = 0
        for byte in alias:
            checksum = (((checksum & 1) << 7) + (checksum >> 1) + byte) & 255
        encoded = name.encode("utf-16le") + b"\0\0"
        encoded += b"\xff\xff" * ((-len(encoded) // 2) % 13)
        rows = []
        for index in reversed(range(len(encoded) // 26)):
            row = bytearray(32)
            row[0] = index + 1 | (0x40 if index == len(encoded) // 26 - 1 else 0)
            row[11], row[13] = 0x0F, checksum
            chunk = encoded[index * 26:(index + 1) * 26]
            row[1:11], row[14:26], row[28:32] = chunk[:10], chunk[10:22], chunk[22:]
            rows.append(row)
        return b"".join(rows)

    tree = {}
    for path, data in files.items():
        node = tree
        parts = path.strip("/").split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = data

    def directory(children, parent=0, root=False):
        names = [(name, f"N{index:07d}   ".encode()) for index, name in enumerate(children)]
        size = sum(len(long_name(name, alias)) + 32 for name, alias in names) + 96
        cluster = 0 if root else allocate(size)
        rows = bytearray() if root else entry(b".          ", cluster, 0, True) + entry(b"..         ", parent, 0, True)
        for name, alias in names:
            child = children[name]
            is_directory = isinstance(child, dict)
            start = directory(child, cluster) if is_directory else allocate(len(child))
            if not is_directory:
                offset = (33 + start - 2) * 512
                disk[offset:offset + len(child)] = child
            rows += long_name(name, alias) + entry(alias, start, 0 if is_directory else len(child), is_directory)
        offset = 19 * 512 if root else (33 + cluster - 2) * 512
        disk[offset:offset + len(rows)] = rows
        return cluster

    directory(tree, root=True)
    disk[512:10 * 512] = disk[10 * 512:19 * 512] = fat
    image = bytearray((2048 + partitions * 2880) * 512)
    image[510:512] = b"\x55\xaa"
    for index in range(partitions):
        start = 2048 + index * 2880
        image[446 + index * 16:462 + index * 16] = struct.pack("<B3sB3sII", 0, b"\x00\x02\x00", 1, b"\xfe\xff\xff", start, 2880)
        image[start * 512:(start + 2880) * 512] = disk
    return bytes(image)


@pytest.fixture
def histories(tmp_path):
    return {CHROME: make_chrome_history(tmp_path / "History").read_bytes(),
            FIREFOX: make_firefox_history(tmp_path / "places.sqlite").read_bytes()}


def analyze_image(tmp_path, image, **limits):
    path = tmp_path / "browser.img"
    path.write_bytes(image)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    job = {"id": "image-evidence", "run_id": "image-run", "name": path.name, "source_path": str(path),
           "size": len(image), "kind": "raw_image", "sector_size": 512, "imported_at": "2026-01-01T00:00:00Z"}
    result = analysis.analyze(job, {**LIMITS, **limits}, str(work))
    assert "error" not in result
    assert path.read_bytes() == image
    assert not list(work.glob("content-*"))
    return result


def summary(result):
    return next(step for step in result["coverage"]["steps"] if step["id"] == "image-browser-history")


@TSK
def test_fat_browser_records_save_with_inner_artifacts_and_utc_times(tmp_path, histories):
    store = Store(tmp_path / "workspace")
    store.initialize()
    case = store.create_case("Image history", "")
    source = store.root / "evidence" / "image"
    image = browser_image(histories)
    source.write_bytes(image)
    store.register("image", case["id"], "browser.img", len(image), source, "raw_image", 512)
    job = store.claim(LIMITS, analysis.PARSER_VERSION)
    work = store.root / "work" / job["run_id"]
    work.mkdir()
    result = analysis.analyze(job, LIMITS, str(work))
    assert "error" not in result and store.finish(job, result)
    assert source.read_bytes() == image
    digest = hashlib.sha256(image).hexdigest()
    assert store.detail("image")["sha256"] == result["sha256"] == digest
    assert result["artifacts"][0]["details"]["sha256"] == digest
    records = store.records("image", "", 0, 100)["items"]
    assert len(records) == 6 and len({record["artifact_id"] for record in records}) == 2
    for record in records:
        artifact = store.artifact("image", record["artifact_id"])
        assert artifact["kind"] == "file" and artifact["path"] in histories
        assert artifact["partition_offset"] == 2048 and artifact["metadata_address"].isdigit()
        assert record["evidence_id"] == artifact["evidence_id"] == "image"
        assert record["run_id"] == artifact["run_id"] == job["run_id"]
        content = artifact["details"]["content"]
        assert content["sha256"] == hashlib.sha256(histories[artifact["path"]]).hexdigest() != digest
        assert content["evidence_id"] == "image" and content["run_id"] == job["run_id"]
        assert content["parser"] == record["parser"] and content["parser_version"] == analysis.PARSER_VERSION
        assert record["at"] == f"2023-11-14T22:{12 + int(record['source_key'])}:20.123456+00:00"
        assert analysis.extract_artifact(store.extraction_target("image", artifact["id"])) == histories[artifact["path"]]
    assert summary(result)["candidates_found"] == summary(result)["extracted"] == summary(result)["parsed"] == 2
    assert result["coverage"]["status"] == "complete"
    assert SNAPSHOT_SCOPE in result["coverage"]["scope"] and SNAPSHOT_SCOPE in result["warnings"]
    assert not list(work.glob("content-*"))
    repeated = analysis.analyze(job, LIMITS, str(work))
    assert [record["artifact_key"] for record in repeated["records"]] == [record["artifact_key"] for record in result["records"]]


@TSK
def test_equal_addresses_in_two_partitions_have_distinct_keys(tmp_path, histories):
    result = analyze_image(tmp_path, browser_image(histories, partitions=2))
    assert len(result["records"]) == 12
    assert len({record["artifact_key"] for record in result["records"]}) == 4
    assert {item["partition_offset"] for item in result["artifacts"] if item.get("key", "").startswith("image:")} == {2048, 4928}


@TSK
def test_profile_path_only_nominates_schema_candidate(tmp_path, histories):
    unrelated = tmp_path / "unrelated.sqlite"
    with sqlite3.connect(unrelated) as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
    files = {**histories, CHROME.replace("Default", "Profile 1"): b"plain text, not a database",
             CHROME.replace("Default", "Profile 2"): unrelated.read_bytes(),
             CHROME.replace("Default", "Profile 3"): histories[FIREFOX],
             "/Documents/History": histories[CHROME]}
    result = analyze_image(tmp_path, browser_image(files))
    assert len(result["records"]) == 9
    assert summary(result)["candidates_found"] == summary(result)["extracted"] == 5
    assert summary(result)["failed"] == summary(result)["unsupported"] == 1
    steps = {step["label"]: step for step in result["coverage"]["steps"]}
    assert steps[CHROME.replace("Default", "Profile 1")]["reason"] == "database_unreadable"
    assert steps[CHROME.replace("Default", "Profile 2")]["reason"] == "unsupported_schema"
    assert steps[CHROME.replace("Default", "Profile 3")]["parser"] == "firefox-history/1"


@TSK
def test_truncated_inventory_keeps_content_coverage_qualified(tmp_path, histories):
    image = browser_image({**histories, "/Unexamined.txt": b"outside the retained inventory"})
    complete = analyze_image(tmp_path, image)
    # Even two successful database parses cannot establish coverage beyond the inventory's cutoff.
    limit = max(index + 1 for index, item in enumerate(complete["artifacts"]) if item["path"] in histories)
    result = analyze_image(tmp_path, image, max_artifacts=limit)
    assert len(result["records"]) == 6 and summary(result)["parsed"] == 2
    assert summary(result)["status"] == "partial" and summary(result)["reason"] == "inventory_incomplete"
    assert "additional candidates may be missing" in summary(result)["detail"]
    assert any(step["reason"] == "artifact_limit" for step in result["coverage"]["steps"])


@TSK
@pytest.mark.parametrize("limit,value,reason", [
    ("image_content_candidates", 1, "image_candidate_limit"),
    ("image_content_timeout", 0, "image_content_timeout"),
    ("content_file_bytes", 10, "content_file_limit"),
    ("parsed_records", 4, "record_limit"),
    ("record_payload_bytes", 1000, "record_payload_limit"),
])
def test_image_budgets_are_visible_and_aggregate(tmp_path, histories, monkeypatch, limit, value, reason):
    limits = analysis.IMAGE_CONTENT_LIMITS if limit.startswith("image_") else CONTENT_LIMITS
    monkeypatch.setitem(limits, limit, value)
    result = analyze_image(tmp_path, browser_image(histories))
    assert summary(result)["budget_stops"] > 0 and summary(result)["status"] == "partial"
    assert any(step["reason"] == reason for step in result["coverage"]["steps"])
    assert len(result["records"]) <= CONTENT_LIMITS["parsed_records"]
    payload = 2 + sum(len(json.dumps(record, ensure_ascii=True).encode()) + 2 for record in result["records"])
    assert payload <= CONTENT_LIMITS["record_payload_bytes"]


@TSK
@pytest.mark.parametrize("phase", ["extract", "parse"])
def test_failure_cleans_copies_and_continues_other_candidates(tmp_path, histories, monkeypatch, phase):
    capture = analysis._capture
    failed = False

    def fail_once(args, timeout, max_bytes):
        nonlocal failed
        if not failed and ((phase == "extract" and args[0] == "icat") or (phase == "parse" and "cds.content_parser" in args)):
            failed = True
            if phase == "parse":
                copy = Path(args[4])
                assert copy.parent.parent == tmp_path / "work" and copy.is_file()
                raise ValueError("Synthetic parser failure")
            raise analysis.ToolLimitError("tool_output_limit", "Synthetic extraction output limit")
        return capture(args, timeout, max_bytes)

    monkeypatch.setattr(analysis, "_capture", fail_once)
    result = analyze_image(tmp_path, browser_image(histories))
    assert summary(result)["failed"] == 1 and summary(result)["parsed"] == 1
    assert len(result["records"]) == 3 and result["sha256"]


@pytest.mark.parametrize("failure", ["missing_parser", "memory_error"])
@pytest.mark.parametrize("failed_path", [CHROME, FIREFOX])
def test_unexpected_candidate_error_preserves_other_saved_records(tmp_path, histories, monkeypatch, failure, failed_path):
    files = {str(index): (path, data) for index, (path, data) in enumerate(histories.items(), 10)}
    listing = "".join(f"0|{path}|{address}|r/rrw-rw-rw-|0|0|{len(data)}|0|0|0|0\n"
                      for address, (path, data) in files.items())
    fake_image_tools(monkeypatch, {2048: (0, listing, "")})
    capture = analysis._capture

    def fail_candidate(args, timeout, max_bytes):
        if args[0] == "icat":
            return 0, files[args[-1]][1], ""
        copy = Path(args[4])
        assert copy.is_file()
        if copy.read_bytes() != histories[failed_path]:
            return capture(args, timeout, max_bytes)
        if failure == "memory_error":
            raise MemoryError("Synthetic candidate memory failure")
        code, output, error = capture(args, timeout, max_bytes)
        parsed = json.loads(output)
        del parsed["parser"]  # Malformed output raises KeyError in the candidate consumer.
        return code, json.dumps(parsed).encode(), error

    monkeypatch.setattr(analysis, "_capture", fail_candidate)
    store = Store(tmp_path / "workspace")
    store.initialize()
    case = store.create_case("Candidate failure", "")
    source = store.root / "evidence" / "image"
    source.write_bytes(b"synthetic inventory")
    store.register("image", case["id"], "browser.img", source.stat().st_size, source, "raw_image", 512)
    job = store.claim(LIMITS, analysis.PARSER_VERSION)
    work = store.root / "work" / job["run_id"]
    work.mkdir()
    result = analysis.analyze(job, LIMITS, str(work))
    assert "error" not in result and store.finish(job, result)
    saved = store.detail("image")
    assert saved["status"] == "completed" and saved["coverage"]["status"] == "partial"
    assert saved["sha256"] == hashlib.sha256(b"synthetic inventory").hexdigest()
    steps = {step["label"]: step for step in saved["coverage"]["steps"]}
    assert steps[failed_path]["status"] == "failed"
    assert steps[failed_path]["reason"] == "content_parser_error"
    assert ("parser" if failure == "missing_parser" else "Synthetic candidate memory failure") in steps[failed_path]["detail"]
    assert summary(result)["failed"] == summary(result)["parsed"] == 1
    assert "1 failed" in summary(result)["detail"]
    assert summary(result)["detail"] in saved["warnings"]
    records = store.records("image", "", 0, 100)["items"]
    assert len(records) == 3
    assert all(store.artifact("image", record["artifact_id"])["path"] != failed_path for record in records)
    assert source.read_bytes() == b"synthetic inventory" and not list(work.glob("content-*"))


@pytest.mark.parametrize("status", ["unknown", "running", "unexpected", None, ["complete"]])
def test_unexpected_candidate_status_is_counted_without_claiming_success(tmp_path, monkeypatch, status):
    listing = f"0|{CHROME}|10|r/rrw-rw-rw-|0|0|4|0|0|0|0\n"
    fake_image_tools(monkeypatch, {2048: (0, listing, "")})
    parsed = {"parser": "browser-history/1", "records": [], "status": status,
              "reason": "unexpected_parser_status", "detail": "Candidate did not finish.", "total": 0}
    monkeypatch.setattr(analysis, "_capture", lambda args, *_: (
        0, b"data" if args[0] == "icat" else json.dumps(parsed).encode(), ""))
    result = analyze_image(tmp_path, b"synthetic inventory")
    assert result["coverage"]["status"] == summary(result)["status"] == "partial"
    assert summary(result)["unknown"] == 1 and summary(result)["parsed"] == 0
    assert summary(result)["reason"] == "candidate_errors"
    assert "1 unknown" in summary(result)["detail"]
    assert summary(result)["detail"] in result["warnings"]
    candidate = next(step for step in result["coverage"]["steps"] if step["label"] == CHROME)
    assert candidate["status"] == "unknown"
    assert "Candidate did not finish." in candidate["detail"]
    if status != "unknown":
        assert f"Unexpected parser status {status!r}; recorded as unknown." in candidate["detail"]


@pytest.mark.parametrize("issue,status,reason,note", [
    ("none", "complete", "candidates_examined", ""),
    ("truncated", "partial", "candidates_partial", "shortened fields"),
    ("utf8", "partial", "candidates_partial", "Invalid UTF-8"),
    ("unreadable", "failed", "candidate_errors", "could not be read"),
    ("unsupported", "unsupported", "candidate_errors", "supported browser history schema"),
])
def test_candidate_summary_distinguishes_quality_notes_from_errors(tmp_path, monkeypatch, issue, status, reason, note):
    path = make_chrome_history(tmp_path / "History")
    with sqlite3.connect(path) as db:
        if issue == "truncated":
            db.execute("UPDATE urls SET title=? WHERE id=1", ("a" * (CONTENT_LIMITS["record_text_chars"] + 1),))
        elif issue == "utf8":
            db.execute("UPDATE urls SET url=CAST(x'68747470733a2fff' AS TEXT) WHERE id=2")
        elif issue == "unsupported":
            db.execute("DROP TABLE visits")
    data = b"not a database" if issue == "unreadable" else path.read_bytes()
    listing = f"0|{CHROME}|10|r/rrw-rw-rw-|0|0|{len(data)}|0|0|0|0\n"
    fake_image_tools(monkeypatch, {2048: (0, listing, "")})
    monkeypatch.setattr(analysis, "_icat", lambda *_: data)
    result = analyze_image(tmp_path, b"synthetic inventory")
    candidate = next(step for step in result["coverage"]["steps"] if step["label"] == CHROME)
    assert candidate["status"] == status and note in candidate["detail"]
    assert summary(result)["reason"] == reason
    assert summary(result)["status"] == ("complete" if status == "complete" else "partial")
    assert summary(result)["parsed"] == (1 if status in {"complete", "partial"} else 0)
    assert len(result["records"]) == (3 if status in {"complete", "partial"} else 0)


@pytest.mark.parametrize("suffix", ["", "-wal"])
def test_size_mismatch_saves_extracted_content_with_null_parser(tmp_path, monkeypatch, suffix):
    listing = f"0|{CHROME}|10|r/rrw-rw-rw-|0|0|{4 if suffix else 5}|0|0|0|0\n"
    if suffix:
        listing += f"0|{CHROME}{suffix}|11|r/rrw-rw-rw-|0|0|5|0|0|0|0\n"
    fake_image_tools(monkeypatch, {2048: (0, listing, "")})
    monkeypatch.setattr(analysis, "_icat", lambda *_: b"data")

    def unexpected_parser(*_):
        pytest.fail("A size-mismatched working set must not reach the parser.")

    monkeypatch.setattr(analysis, "_capture", unexpected_parser)
    store = Store(tmp_path / "workspace")
    store.initialize()
    case = store.create_case("Size mismatch", "")
    source = store.root / "evidence" / "image"
    source.write_bytes(b"synthetic inventory")
    store.register("image", case["id"], "browser.img", source.stat().st_size, source, "raw_image", 512)
    job = store.claim(LIMITS, analysis.PARSER_VERSION)
    work = store.root / "work" / job["run_id"]
    work.mkdir()
    result = analysis.analyze(job, LIMITS, str(work))
    assert "error" not in result and store.finish(job, result)
    candidate = next(step for step in store.detail("image")["coverage"]["steps"] if step["label"] == CHROME)
    assert candidate["status"] == "failed" and candidate["extracted"]
    assert "extracted byte count differs" in candidate["detail"]
    artifacts = store.artifacts("image", "", 0, 100)["items"]
    for artifact in artifacts:
        if artifact["path"].startswith(CHROME):
            content = artifact["details"]["content"]
            assert content["parser"] is None and content["size_bytes"] == 4
            assert content["sha256"] == hashlib.sha256(b"data").hexdigest()
            assert content["evidence_id"] == "image" and content["run_id"] == job["run_id"]
    if suffix:
        assert candidate["sidecars"][0]["extracted"] and not candidate["sidecars"][0]["included"]
    assert source.read_bytes() == b"synthetic inventory" and not list(work.glob("content-*"))


def test_candidate_extraction_failure_uses_content_wording(tmp_path, monkeypatch):
    listing = f"0|{CHROME}|10|r/rrw-rw-rw-|0|0|4|0|0|0|0\n"
    fake_image_tools(monkeypatch, {2048: (0, listing, "")})
    monkeypatch.setattr(analysis, "_capture", lambda *args: (1, b"partial bytes", "Unreadable file"))
    result = analyze_image(tmp_path, b"synthetic inventory")
    candidate = next(step for step in result["coverage"]["steps"] if step["label"] == CHROME)
    assert candidate["status"] == "failed" and candidate["detail"] == "Extraction failed. Unreadable file"


@TSK
@pytest.mark.parametrize("scope", ["candidate", "image"])
def test_extraction_and_parser_share_time_budget(tmp_path, histories, monkeypatch, scope):
    capture = analysis._capture
    monotonic = analysis.time.monotonic
    elapsed = 0
    monkeypatch.setattr(analysis.time, "monotonic", lambda: monotonic() + elapsed)

    def exhaust(args, timeout, max_bytes):
        nonlocal elapsed
        result = capture(args, timeout, max_bytes)
        if args[0] == "icat":
            elapsed += CONTENT_LIMITS["content_timeout"] + 1 if scope == "candidate" else analysis.IMAGE_CONTENT_LIMITS["image_content_timeout"] + 1
        assert "cds.content_parser" not in args
        return result

    monkeypatch.setattr(analysis, "_capture", exhaust)
    result = analyze_image(tmp_path, browser_image(histories))
    assert not result["records"] and summary(result)["budget_stops"] == 2
    reason = "content_timeout" if scope == "candidate" else "image_content_timeout"
    assert any(step["reason"] == reason for step in result["coverage"]["steps"])


@pytest.mark.parametrize("path", [
    CHROME, FIREFOX, "/home/user/.config/chromium/Profile 1/History",
    "/home/user/.config/google-chrome/Default/History", "/home/user/.mozilla/firefox/a.default/places.sqlite",
    "/Users/user/Library/Application Support/Google/Chrome/Default/History",
    "/Users/user/Library/Application Support/Chromium/Default/History",
    "/Users/user/Library/Application Support/Firefox/Profiles/a.default/places.sqlite",
    CHROME.replace("Google/Chrome", "Chromium"),
])
def test_known_profile_layouts_and_allocated_only(tmp_path, monkeypatch, path):
    listing = (f"0|{path}|10|r/rrw-rw-rw-|0|0|100|0|0|0|0\n"
               f"0|{path} (deleted)|11|r/rrw-rw-rw-|0|0|100|0|0|0|0\n"
               f"0|{path}|12|d/drwxrwxrwx|0|0|100|0|0|0|0\n"
               "0|/Documents/History|13|r/rrw-rw-rw-|0|0|100|0|0|0|0\n")
    fake_image_tools(monkeypatch, {2048: (0, listing, "")})
    monkeypatch.setitem(analysis.IMAGE_CONTENT_LIMITS, "image_content_candidates", 0)
    result = analyze_image(tmp_path, b"synthetic inventory")
    assert summary(result)["candidates_found"] == 1
    candidate = next(item for item in result["artifacts"] if item.get("key", "").startswith("image:"))
    assert candidate["path"] == path and candidate["metadata_address"] == "10" and not candidate["deleted"]


def test_restart_removes_interrupted_content_copies_only(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    case = store.create_case("Interrupted image", "")
    source = store.root / "evidence" / "source"
    source.write_bytes(b"original")
    store.register("source", case["id"], "disk.img", 8, source, "raw_image", 512)
    job = store.claim()
    work = store.root / "work" / job["run_id"]
    temporary = work / "content-interrupted"
    temporary.mkdir(parents=True)
    (temporary / "history.sqlite").write_bytes(b"disposable")
    (work / "progress.json").write_text("{}")
    (work / "content-link").symlink_to(source.parent, target_is_directory=True)
    store.recover_interrupted()
    assert not list(work.glob("content-*"))
    assert (work / "progress.json").is_file() and source.read_bytes() == b"original"
    assert store.detail("source")["status"] == "queued"


@pytest.fixture
def journal_files(tmp_path):
    files = {}
    for profile, name, make_history, statement in (
        (CHROME, "wal-History", make_chrome_history,
         "INSERT INTO visits(id,url,visit_time) VALUES(2,1,13344473660123456)"),
        (FIREFOX, "wal-places.sqlite", make_firefox_history,
         "INSERT INTO moz_historyvisits(id,place_id,visit_date) VALUES(2,1,1700000060123456)"),
    ):
        path = make_history(tmp_path / name, count=1, optional=False)
        # A normal close checkpoints away the condition this fixture must reproduce.
        subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA wal_autocheckpoint=0')
db.execute(sys.argv[2])
db.commit()
os._exit(0)
""", str(path), statement], check=True, timeout=5)
        for suffix in ("", "-wal", "-shm"):
            files[profile + suffix] = path.with_name(path.name + suffix).read_bytes()
        assert len(files[profile + "-wal"]) > 32
    return files


@TSK
@pytest.mark.parametrize("suffixes", [("-wal", "-shm"), ("-wal",), ()])
def test_unclean_wal_visits_and_sidecar_provenance_are_saved(tmp_path, journal_files, monkeypatch, suffixes):
    files = {profile + suffix: journal_files[profile + suffix]
             for profile in (CHROME, FIREFOX) for suffix in ("", *suffixes)}
    image = browser_image(files)
    store = Store(tmp_path / "workspace")
    store.initialize()
    case = store.create_case("WAL image", "")
    source = store.root / "evidence" / "image"
    source.write_bytes(image)
    store.register("image", case["id"], "browser.img", len(image), source, "raw_image", 512)
    job = store.claim(LIMITS, analysis.PARSER_VERSION)
    work = store.root / "work" / job["run_id"]
    work.mkdir()
    capture = analysis._capture
    working_sets = []

    def inspect_working_set(args, timeout, max_bytes):
        if "cds.content_parser" not in args:
            return capture(args, timeout, max_bytes)
        copy = Path(args[4])
        assert copy.parent.parent == work
        assert sorted(part.name for part in copy.parent.iterdir()) == sorted("history.sqlite" + suffix for suffix in ("", *suffixes))
        assert ("--journal-aware" in args) == bool(suffixes)
        working_sets.append(copy.parent)
        result = capture(args, timeout, max_bytes)
        if suffixes == ("-wal",):
            # SQLite really writes here; this must be confined to the disposable run directory.
            assert copy.with_name("history.sqlite-shm").stat().st_size > 0
        return result

    monkeypatch.setattr(analysis, "_capture", inspect_working_set)
    result = analysis.analyze(job, LIMITS, str(work))
    assert "error" not in result and store.finish(job, result)
    records = store.records("image", "", 0, 100)["items"]
    assert len(records) == (4 if suffixes else 2)
    assert {record["source_key"] for record in records} == ({"1", "2"} if suffixes else {"1"})
    assert {record["at"] for record in records} == (
        {"2023-11-14T22:13:20.123456+00:00", "2023-11-14T22:14:20.123456+00:00"} if suffixes
        else {"2023-11-14T22:13:20.123456+00:00"})
    steps = {step["label"]: step for step in store.detail("image")["coverage"]["steps"]}
    artifacts = store.artifacts("image", "", 0, 200)["items"]
    by_path = {artifact["path"]: artifact for artifact in artifacts}
    for profile in (CHROME, FIREFOX):
        step = steps[profile]
        assert step["sidecar_status"] == ("included" if suffixes else "absent")
        assert len(step["sidecars"]) == len(suffixes)
        for sidecar in step["sidecars"]:
            artifact = by_path[sidecar["path"]]
            content = artifact["details"]["content"]
            assert artifact["id"] != by_path[profile]["id"]
            assert sidecar["artifact_key"] == content["artifact_key"] != step["artifact_key"]
            assert content["sha256"] == sidecar["sha256"] == hashlib.sha256(files[sidecar["path"]]).hexdigest()
            assert content["size_bytes"] == sidecar["size_bytes"] == len(files[sidecar["path"]])
            assert content["run_id"] == job["run_id"] and content["evidence_id"] == "image"
            assert content["parser_version"] == analysis.PARSER_VERSION
            assert sidecar["included"] and sidecar["extracted"]
            assert sidecar["partition_offset"] == artifact["partition_offset"] == 2048
            assert sidecar["metadata_address"] == artifact["metadata_address"]
        assert by_path[profile]["details"]["content"]["sidecars"] == step["sidecars"]
        if suffixes:
            assert "committed WAL" in step["detail"] and SNAPSHOT_SCOPE not in step["detail"]
        else:
            assert "No allocated WAL/SHM sidecars" in step["detail"] and "base database only" in step["detail"]
    assert summary(result)["sidecars_included" if suffixes else "sidecars_absent"] == 2
    assert summary(result)["sidecars_unusable"] == 0 and result["coverage"]["status"] == "complete"
    assert (SNAPSHOT_SCOPE in result["coverage"]["scope"]) == (not suffixes)
    assert source.read_bytes() == image
    assert result["sha256"] == store.detail("image")["sha256"] == hashlib.sha256(image).hexdigest()
    assert len(working_sets) == 2 and all(not directory.exists() for directory in working_sets)
    assert not list(work.glob("content-*"))


@TSK
@pytest.mark.parametrize("failure", ["extraction", "short_read", "parser", "wal_header", "wal_checksum", "wal_frame", "truncated_wal", "shm"])
def test_present_unusable_sidecars_are_gaps_and_leave_no_copies(tmp_path, journal_files, monkeypatch, failure):
    files = dict(journal_files)
    if failure == "truncated_wal":
        files[CHROME + "-wal"] = files[CHROME + "-wal"][:-1]
    if failure in {"wal_header", "wal_checksum", "wal_frame", "shm"}:
        suffix = "-shm" if failure == "shm" else "-wal"
        damaged = bytearray(files[CHROME + suffix])
        offset = {"wal_header": 0, "wal_checksum": 24, "wal_frame": -1, "shm": 40}[failure]
        damaged[offset] ^= 255
        files[CHROME + suffix] = bytes(damaged)
    icat = analysis._icat
    capture = analysis._capture

    def fail_sidecar(path, sector, artifact, timeout, max_bytes):
        if artifact["path"] == CHROME + "-shm":
            if failure == "extraction":
                raise subprocess.CalledProcessError(1, ["icat"], stderr="Synthetic sidecar extraction failure")
            if failure == "short_read":
                return files[CHROME + "-shm"][:-1]
        return icat(path, sector, artifact, timeout, max_bytes)

    def fail_parser(args, timeout, max_bytes):
        if failure == "parser" and "cds.content_parser" in args:
            copy = Path(args[4])
            assert copy.with_name("history.sqlite-wal").is_file() and copy.with_name("history.sqlite-shm").is_file()
            if copy.read_bytes() == files[CHROME]:
                raise ValueError("Synthetic working-set parser failure")
        return capture(args, timeout, max_bytes)

    monkeypatch.setattr(analysis, "_icat", fail_sidecar)
    monkeypatch.setattr(analysis, "_capture", fail_parser)
    result = analyze_image(tmp_path, browser_image(files))
    candidate = next(step for step in result["coverage"]["steps"] if step["label"] == CHROME)
    assert candidate["status"] == "failed" and candidate["sidecar_status"] == "unusable"
    assert "journal coverage has a gap" in candidate["detail"]
    assert "No allocated WAL/SHM sidecars" not in candidate["detail"] and SNAPSHOT_SCOPE not in candidate["detail"]
    assert len(candidate["sidecars"]) == 2 and all(not entry["included"] for entry in candidate["sidecars"])
    assert summary(result)["sidecars_unusable"] == summary(result)["sidecars_included"] == 1
    assert summary(result)["sidecars_absent"] == 0 and result["coverage"]["status"] == "partial"
    assert len(result["records"]) == 2 and all(record["parser"] == "firefox-history/1" for record in result["records"])
    assert summary(result)["detail"] in result["warnings"]


@TSK
@pytest.mark.parametrize("scope", ["candidate", "image"])
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_sidecars_share_candidate_and_image_byte_budgets(tmp_path, journal_files, monkeypatch, scope, suffix):
    files = {path: data for path, data in journal_files.items() if path.startswith(CHROME)}
    required = len(files[CHROME]) + len(files[CHROME + "-wal"])
    if suffix == "-shm":
        required += len(files[CHROME + "-shm"])
    limits = CONTENT_LIMITS if scope == "candidate" else analysis.IMAGE_CONTENT_LIMITS
    monkeypatch.setitem(limits, "content_file_bytes" if scope == "candidate" else "image_content_bytes", required - 1)
    result = analyze_image(tmp_path, browser_image(files))
    step = next(step for step in result["coverage"]["steps"] if step["label"] == CHROME)
    assert step["sidecar_status"] == "unusable" and step["reason"] == ("content_file_limit" if scope == "candidate" else "image_content_limit")
    assert CHROME + suffix in step["detail"] and "gap" in step["detail"]
    assert not result["records"] and summary(result)["budget_stops"] == 1
    assert summary(result)["extraction_bytes_reserved"] < required


@TSK
def test_image_byte_budget_includes_previous_working_sets(tmp_path, journal_files, monkeypatch):
    chrome_bytes = sum(len(data) for path, data in journal_files.items() if path.startswith(CHROME))
    monkeypatch.setitem(analysis.IMAGE_CONTENT_LIMITS, "image_content_bytes", chrome_bytes + len(journal_files[FIREFOX]))
    result = analyze_image(tmp_path, browser_image(journal_files))
    assert summary(result)["sidecars_included"] == summary(result)["sidecars_unusable"] == 1
    assert summary(result)["budget_stops"] == 1 and len(result["records"]) == 2
    assert all(record["parser"] == "chrome-history/1" for record in result["records"])


@TSK
@pytest.mark.parametrize("location", ["directory", "partition"])
def test_wal_is_never_adopted_from_another_directory_or_partition(tmp_path, journal_files, location):
    files = {CHROME: journal_files[CHROME]}
    if location == "directory":
        files[CHROME.replace("Default", "Other Profile") + "-wal"] = journal_files[CHROME + "-wal"]
        image = browser_image(files)
    else:
        image = bytearray(browser_image(files, partitions=2))
        other = browser_image({CHROME + "-wal": journal_files[CHROME + "-wal"]})
        image[4928 * 512:] = other[2048 * 512:]
        image = bytes(image)
    result = analyze_image(tmp_path, image)
    assert len(result["records"]) == 1 and result["records"][0]["source_key"] == "1"
    assert summary(result)["sidecars_absent"] == 1 and summary(result)["sidecars_included"] == 0
    assert all("content" not in item["details"] for item in result["artifacts"] if item["path"].endswith("-wal"))


@pytest.mark.parametrize("entry", ["deleted", "directory", "case", "ambiguous"])
def test_only_unambiguous_allocated_exact_sidecar_paths_are_used(tmp_path, journal_files, monkeypatch, entry):
    main = journal_files[CHROME]
    wal = journal_files[CHROME + "-wal"]
    path = CHROME + "-wal"
    if entry == "deleted":
        path += " (deleted)"
    elif entry == "case":
        path = path.replace("Default", "default")
    mode = "d/drwxrwxrwx" if entry == "directory" else "r/rrw-rw-rw-"
    listing = (f"0|{CHROME}|10|r/rrw-rw-rw-|0|0|{len(main)}|0|0|0|0\n"
               f"0|{path}|11|{mode}|0|0|{len(wal)}|0|0|0|0\n")
    if entry == "ambiguous":
        listing += f"0|{path}|12|{mode}|0|0|{len(wal)}|0|0|0|0\n"
    fake_image_tools(monkeypatch, {2048: (0, listing, "")})

    def main_only(path, sector, artifact, timeout, max_bytes):
        assert artifact["path"] == CHROME
        return main

    monkeypatch.setattr(analysis, "_icat", main_only)
    result = analyze_image(tmp_path, b"synthetic inventory")
    assert summary(result)["sidecars_unusable" if entry == "ambiguous" else "sidecars_absent"] == 1
    assert len(result["records"]) == (0 if entry == "ambiguous" else 1)


def test_restart_removes_entire_interrupted_journal_working_set(tmp_path):
    store = Store(tmp_path / "workspace")
    store.initialize()
    case = store.create_case("Interrupted journals", "")
    source = store.root / "evidence" / "source"
    source.write_bytes(b"original image")
    store.register("source", case["id"], "disk.img", source.stat().st_size, source, "raw_image", 512)
    job = store.claim()
    work = store.root / "work" / job["run_id"]
    temporary = work / "content-interrupted"
    temporary.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm"):
        (temporary / ("history.sqlite" + suffix)).write_bytes(b"disposable")
    (work / "progress.json").write_text("{}")
    store.recover_interrupted()
    assert not list(work.glob("content-*")) and (work / "progress.json").is_file()
    assert source.read_bytes() == b"original image" and store.detail("source")["status"] == "queued"


@TSK
@pytest.mark.parametrize("scope", ["candidate", "image"])
def test_sidecar_extraction_shares_the_parser_deadline(tmp_path, journal_files, monkeypatch, scope):
    icat = analysis._icat
    monotonic = analysis.time.monotonic
    elapsed = 0
    if scope == "image":
        monkeypatch.setitem(analysis.IMAGE_CONTENT_LIMITS, "image_content_timeout", CONTENT_LIMITS["content_timeout"] // 2)
    monkeypatch.setattr(analysis.time, "monotonic", lambda: monotonic() + elapsed)

    def exhaust(path, sector, artifact, timeout, max_bytes):
        nonlocal elapsed
        data = icat(path, sector, artifact, timeout, max_bytes)
        if artifact["path"].endswith("-wal"):
            elapsed += (CONTENT_LIMITS["content_timeout"] if scope == "candidate"
                        else analysis.IMAGE_CONTENT_LIMITS["image_content_timeout"]) + 1
        return data

    monkeypatch.setattr(analysis, "_icat", exhaust)
    result = analyze_image(tmp_path, browser_image(journal_files))
    assert not result["records"] and summary(result)["budget_stops"] == 2
    assert summary(result)["sidecars_unusable"] == 2 and summary(result)["sidecars_absent"] == 0
    step = next(step for step in result["coverage"]["steps"] if step["label"] == CHROME)
    assert step["reason"] == ("content_timeout" if scope == "candidate" else "image_content_timeout")
    assert "gap" in step["detail"] and CHROME + "-shm" in step["detail"]


@TSK
def test_truncated_inventory_does_not_claim_journals_are_absent_from_image(tmp_path, journal_files):
    image = browser_image(journal_files)
    full = analyze_image(tmp_path, image)
    limit = next(index + 1 for index, item in enumerate(full["artifacts"]) if item["path"] == CHROME)
    result = analyze_image(tmp_path, image, max_artifacts=limit)
    assert summary(result)["status"] == "partial" and summary(result)["reason"] == "inventory_incomplete"
    assert summary(result)["sidecars_absent"] == 1
    assert "Sidecars may also be missing from the available inventory" in summary(result)["detail"]
    assert len(result["records"]) == 1 and result["records"][0]["source_key"] == "1"


def test_unclean_upload_still_ignores_sidecars_without_mutating_them(tmp_path, journal_files):
    path = tmp_path / "upload.sqlite"
    before = {}
    for suffix in ("", "-wal", "-shm"):
        part = path.with_name(path.name + suffix)
        before[part] = journal_files[CHROME + suffix]
        part.write_bytes(before[part])
    work = tmp_path / "work"
    work.mkdir()
    job = {"id": "upload", "name": path.name, "source_path": str(path), "size": path.stat().st_size,
           "kind": "file", "imported_at": "2026-01-01T00:00:00Z"}
    result = analysis.analyze(job, LIMITS, str(work))
    assert "error" not in result and len(result["records"]) == 1
    assert result["records"][0]["source_key"] == "1"
    assert SNAPSHOT_SCOPE in result["coverage"]["scope"] and SNAPSHOT_SCOPE in result["warnings"]
    assert all(part.read_bytes() == data for part, data in before.items())
    assert not list(work.glob("content-*"))


@TSK
def test_shm_without_wal_is_included_but_reports_base_database_only(tmp_path, journal_files):
    files = {CHROME: journal_files[CHROME], CHROME + "-shm": journal_files[CHROME + "-shm"]}
    result = analyze_image(tmp_path, browser_image(files))
    step = next(step for step in result["coverage"]["steps"] if step["label"] == CHROME)
    assert step["sidecar_status"] == "included" and step["sidecars"][0]["included"]
    assert "No WAL was present; base database only" in step["detail"]
    assert len(result["records"]) == 1 and result["records"][0]["source_key"] == "1"
    assert summary(result)["sidecars_included"] == 1 and summary(result)["sidecars_absent"] == 0
