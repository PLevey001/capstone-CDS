import hashlib
import json
import sqlite3

import pytest

import cds.analysis as analysis
from cds.chrome_history import read_history as read_chrome
from cds.content_parser import read_history as detect_history
from cds.config import CONTENT_LIMITS
from cds.firefox_history import read_history
from scripts.make_browser_demo import make_chrome_history, make_firefox_history
from tests.test_chrome_history import analyze_file


@pytest.fixture
def history(tmp_path):
    return make_firefox_history(tmp_path / "places.sqlite")


def parse(path, **limits):
    return read_history(path, {**CONTENT_LIMITS, **limits})


def test_firefox_visits_exclude_unvisited_places_and_bookmarks(history):
    before = hashlib.sha256(history.read_bytes()).hexdigest()
    result = parse(history)
    assert result["status"] == "complete" and result["total"] == result["processed"] == 3
    assert result["parser"] == "firefox-history/1"
    records = result["records"]
    assert [row["source_key"] for row in records] == ["1", "2", "3"]
    assert records[0]["details"]["url"] == records[1]["details"]["url"]
    assert records[0]["summary"] == "Firefox résumé"
    assert records[0]["event_time_us"] == 1700000000123456
    details = records[0]["details"]
    assert details["browser"] == "Firefox" and details["place_id"] == 1
    assert details["timestamp_epoch"] == "1970-01-01T00:00:00Z" and details["timestamp_unit"] == "microseconds"
    assert details["original_timestamp"] == 1700000000123456
    assert details["visit_type"] == 1 and details["from_visit"] == 0 and details["source"] == 0
    assert details["place_guid"] == "firefox-url1"
    assert not any("unvisited" in row["details"]["url"] or "bookmark-only" in row["details"]["url"] for row in records)
    assert "WAL/journal" in result["detail"]
    assert hashlib.sha256(history.read_bytes()).hexdigest() == before


def test_chrome_and_firefox_epochs_resolve_to_the_same_instant(history, tmp_path):
    chrome = make_chrome_history(tmp_path / "History")
    first_chrome = read_chrome(chrome, CONTENT_LIMITS)["records"][0]
    first_firefox = parse(history)["records"][0]
    assert first_chrome["event_time_us"] == first_firefox["event_time_us"] == 1700000000123456
    assert first_chrome["details"]["original_timestamp"] != first_firefox["details"]["original_timestamp"]


@pytest.mark.parametrize("original,expected", [
    (1700000000123457, 1700000000123457), (0, 0), (-1, -1),
    (None, None), ("bad", None), (12.5, None),
    (9223372036854775807, None), (-9223372036854775808, None),
])
def test_firefox_timestamp_precision_range_and_unix_zero(history, original, expected):
    with sqlite3.connect(history) as db:
        db.execute("UPDATE moz_historyvisits SET visit_date=? WHERE id=1", (original,))
    result = parse(history)
    first = result["records"][0]
    assert first["event_time_us"] == expected
    assert first["details"]["original_timestamp"] == (str(original) if isinstance(original, int) and abs(original) > 2**53 - 1 else original)
    assert result["status"] == ("partial" if expected is None else "complete")


def test_optional_columns_null_titles_and_orphan_visits(tmp_path):
    path = make_firefox_history(tmp_path / "old.sqlite", optional=False)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE moz_places SET title=NULL WHERE id=1")
        db.execute("UPDATE moz_historyvisits SET place_id=999 WHERE id=2")
        db.execute("ALTER TABLE moz_places DROP COLUMN guid")
    result = parse(path)
    assert result["status"] == "partial" and result["processed"] == 3
    first = result["records"][0]
    assert first["summary"] == "https://firefox.example.test/repeated"
    assert first["details"]["title"] is None and "visit_type" not in first["details"]
    assert "place_guid" not in first["details"]
    assert result["records"][1]["details"]["url_missing"]
    assert result["records"][1]["details"]["place_id"] == 999
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE moz_places DROP COLUMN title")
    assert parse(path)["records"][0]["details"]["title"] is None


def test_empty_visits_do_not_turn_bookmarks_into_history(tmp_path):
    path = make_firefox_history(tmp_path / "empty.sqlite", count=0)
    result = parse(path)
    assert result["status"] == "complete" and result["reason"] == "empty_history"
    assert result["processed"] == result["total"] == 0 and not result["records"]


@pytest.mark.parametrize("table", ["moz_places", "moz_historyvisits"])
def test_unsupported_composite_keys_cannot_multiply_visits(history, table):
    with sqlite3.connect(history) as db:
        db.execute(f"DROP TABLE {table}")
        if table == "moz_places":
            db.execute("CREATE TABLE moz_places(id INTEGER,url TEXT,PRIMARY KEY(id,url))")
        else:
            db.execute("CREATE TABLE moz_historyvisits(id INTEGER,place_id INTEGER,visit_date INTEGER,PRIMARY KEY(id,place_id))")
    result = parse(history)
    assert result["status"] == "unsupported" and result["reason"] == "unsupported_schema"
    assert not result["records"]


def test_corrupt_input_is_failed_not_an_empty_history(tmp_path):
    path = tmp_path / "places.sqlite"
    path.write_bytes(b"SQLite format 3\0" + b"broken" * 100)
    result = detect_history(path, CONTENT_LIMITS)
    assert result["status"] == "failed" and result["reason"] == "database_unreadable"
    assert not result["records"]


def test_record_payload_and_text_limits_are_shared(history):
    limited = parse(history, parsed_records=2)
    assert limited["status"] == "partial" and limited["reason"] == "record_limit"
    assert [record["source_key"] for record in limited["records"]] == ["1", "2"]
    size = len(json.dumps(limited["records"][0], ensure_ascii=True).encode()) + 4
    payload = parse(history, record_payload_bytes=size)
    assert payload["reason"] == "record_payload_limit" and payload["records"] == limited["records"][:1]
    with sqlite3.connect(history) as db:
        db.execute("UPDATE moz_places SET title=? WHERE id=1", ("x" * 100_000,))
        db.execute("UPDATE moz_places SET url=CAST(x'68747470733a2fff' AS TEXT) WHERE id=2")
        db.execute("UPDATE moz_historyvisits SET session=?,source=? WHERE id=1", ("y" * 100_000, float('inf')))
    result = parse(history, record_text_chars=40)
    assert result["status"] == "partial" and "UTF-8" in result["detail"]
    assert result["records"][0]["summary"] == "x" * 40
    assert result["records"][0]["details"]["truncated_fields"] == ["title", "session"]
    assert result["records"][0]["details"]["source"] == "inf"
    json.dumps(result, allow_nan=False)


def test_automatic_detection_uses_schema_and_rejects_ambiguous_databases(history, tmp_path):
    renamed = history.with_name("History")  # Misleading Chrome filename must not choose the parser.
    history.rename(renamed)
    result = analyze_file(renamed, tmp_path / "work")
    assert result["metadata"]["format"] == "Firefox history"
    assert result["coverage"]["steps"][-1]["id"] == "firefox-history"
    assert result["coverage"]["steps"][-1]["parser"] == "firefox-history/1"
    assert result["coverage"]["status"] == "complete" and len(result["records"]) == 3
    assert result["sha256"] == hashlib.sha256(renamed.read_bytes()).hexdigest()
    assert not list((tmp_path / "work").glob("content-*"))
    with sqlite3.connect(renamed) as db:
        db.execute("CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,visit_time INTEGER)")
        db.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT)")
    mixed = detect_history(renamed, CONTENT_LIMITS)
    assert mixed["status"] == "unsupported" and mixed["reason"] == "ambiguous_schema"
    assert not mixed["records"]


def test_firefox_snapshot_does_not_read_or_checkpoint_separate_wal(history):
    db = sqlite3.connect(history)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO moz_historyvisits(id,place_id,visit_date) VALUES(4,1,1700000000123456)")
        db.commit()
        wal = history.with_name(history.name + "-wal")
        before, wal_before = history.read_bytes(), wal.read_bytes()
        assert parse(history)["processed"] == 3
        assert history.read_bytes() == before and wal.read_bytes() == wal_before
    finally:
        db.close()


@pytest.mark.parametrize("failure,reason", [
    (analysis.ToolLimitError("tool_timeout", "Parser timed out"), "tool_timeout"),
    (analysis.ToolLimitError("tool_output_limit", "Output budget exceeded"), "tool_output_limit"),
])
def test_firefox_parser_failure_keeps_source_hash_and_inventory(history, tmp_path, monkeypatch, failure, reason):
    def fail(*_args):
        raise failure
    monkeypatch.setattr(analysis, "_capture", fail)
    result = analyze_file(history, tmp_path / "work")
    assert result["sha256"] and len(result["artifacts"]) == 1 and not result["records"]
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["steps"][-1]["reason"] == reason
    assert not list((tmp_path / "work").glob("content-*"))


def test_malformed_primary_key_values_fail_before_saving_duplicate_record_keys(history):
    with sqlite3.connect(history) as db:
        db.execute("DROP TABLE moz_historyvisits")
        # SQLite's DESC primary-key exception allows NULL/noninteger values.
        db.execute("CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY DESC,place_id INTEGER,visit_date INTEGER)")
        db.executemany("INSERT INTO moz_historyvisits VALUES(NULL,1,1700000000123456)", [(), ()])
    result = parse(history)
    assert result["status"] == "failed" and result["reason"] == "database_unreadable"
    assert "visit ID is not an integer" in result["detail"]
    assert not result["records"]
