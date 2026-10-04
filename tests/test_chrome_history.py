import hashlib
import json
import sqlite3

import pytest

import cds.analysis as analysis
from cds.chrome_history import read_history
from cds.config import CONTENT_LIMITS
from scripts.make_browser_demo import make_chrome_history


@pytest.fixture
def history(tmp_path):
    return make_chrome_history(tmp_path / "History")


def parse(path, **limits):
    return read_history(path, {**CONTENT_LIMITS, **limits})


def analyze_file(path, work_dir, **settings):
    work_dir.mkdir(exist_ok=True)
    job = {"name": path.name, "source_path": str(path), "size": path.stat().st_size,
           "kind": "file", "sector_size": 512, "imported_at": "2026-01-01T00:00:00Z"}
    return analysis.analyze(job, {"tool_timeout": 5, "max_artifacts": 20, **settings}, str(work_dir))


def test_visits_preserve_precision_repeats_unicode_and_sync_provenance(history):
    before = hashlib.sha256(history.read_bytes()).hexdigest()
    result = parse(history)
    assert result["status"] == "complete" and result["processed"] == result["total"] == 3
    records = result["records"]
    assert [row["source_key"] for row in records] == ["1", "2", "3"]
    assert records[0]["details"]["url"] == records[1]["details"]["url"]
    assert records[0]["summary"] == "Résumé & research"
    assert records[0]["event_time_us"] == 1700000000123456
    assert records[0]["details"]["original_timestamp"] == "13344473600123456"
    assert records[0]["details"]["integer_text_fields"] == ["original_timestamp"]
    assert records[2]["details"]["visit_source"] == 0
    assert records[2]["details"]["originator_cache_guid"] == "synthetic-other-device"
    assert records[2]["details"]["originator_visit_id"] == 900
    assert "WAL/journal" in result["detail"]
    assert hashlib.sha256(history.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("original,expected", [
    (11644473600000000, 0), (11644473599999999, -1),
    (13344473600123456, 1700000000123456), (0, None), (None, None),
    (9223372036854775807, None), (-9223372036854775808, None), ("bad", None), (12.5, None),
])
def test_timestamp_sentinels_precision_and_out_of_range_values(history, original, expected):
    with sqlite3.connect(history) as db:
        db.execute("UPDATE visits SET visit_time=? WHERE id=1", (original,))
    result = parse(history)
    first = result["records"][0]
    assert first["event_time_us"] == expected
    saved_original = str(original) if isinstance(original, int) and abs(original) > 2**53 - 1 else original
    assert first["details"]["original_timestamp"] == saved_original
    assert result["status"] == ("partial" if expected is None else "complete")


def test_old_schema_and_orphan_url_keep_the_visit(tmp_path):
    path = make_chrome_history(tmp_path / "renamed.bin", optional=False)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE visit_source")
        db.execute("UPDATE visits SET url=999 WHERE id=2")
        db.execute("ALTER TABLE urls DROP COLUMN title")
    result = parse(path)
    assert result["processed"] == 3 and result["status"] == "partial"
    assert result["records"][1]["details"]["url_missing"]
    assert result["records"][1]["details"]["url_id"] == 999
    assert result["records"][0]["details"]["title"] is None
    assert result["records"][0]["details"]["visit_source"] is None
    assert "transition" not in result["records"][0]["details"]


def test_empty_and_unsupported_are_distinct(tmp_path):
    path = make_chrome_history(tmp_path / "empty.sqlite", count=0)
    result = parse(path)
    assert result["status"] == "complete" and result["reason"] == "empty_history"
    assert result["total"] == result["processed"] == 0
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE visits RENAME COLUMN visit_time TO unknown_time")
    result = parse(path)
    assert result["status"] == "unsupported" and result["reason"] == "unsupported_schema"


def test_corrupt_database_is_not_an_empty_history(tmp_path):
    path = tmp_path / "History"
    path.write_bytes(b"SQLite format 3\0" + b"broken" * 100)
    result = parse(path)
    assert result["status"] == "failed" and result["reason"] == "database_unreadable"
    assert not result["records"] and result["total"] is None


def test_record_and_payload_limits_return_deterministic_prefix(history):
    result = parse(history, parsed_records=2)
    assert result["reason"] == "record_limit" and result["total"] == 3
    assert [row["source_key"] for row in result["records"]] == ["1", "2"]
    assert "1 through 2" in result["detail"]
    first_size = len(json.dumps(result["records"][0], ensure_ascii=True).encode()) + 4
    limited = parse(history, record_payload_bytes=first_size)
    assert limited["reason"] == "record_payload_limit" and limited["status"] == "partial"
    assert limited["records"] == result["records"][:1]
    assert not parse(history, record_payload_bytes=2)["records"]


def test_long_fields_are_shortened_and_invalid_utf8_is_reported(history):
    with sqlite3.connect(history) as db:
        db.execute("UPDATE urls SET title=? WHERE id=1", ("a" * 100_000,))
        db.execute("UPDATE urls SET url=CAST(x'68747470733a2fff' AS TEXT) WHERE id=2")
    result = parse(history, record_text_chars=40)
    first = result["records"][0]
    assert first["summary"] == "a" * 40
    assert "title" in first["details"]["truncated_fields"]
    assert result["status"] == "partial" and "UTF-8" in result["detail"]


def test_standalone_reader_does_not_consume_or_checkpoint_wal(history):
    db = sqlite3.connect(history)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO visits(id,url,visit_time) VALUES(4,1,13344473600123456)")
        db.commit()
        wal = history.with_name(history.name + "-wal")
        main_before, wal_before = history.read_bytes(), wal.read_bytes()
        result = parse(history)
        assert result["total"] == 3
        assert "not supplied or examined" in result["detail"]
        assert history.read_bytes() == main_before and wal.read_bytes() == wal_before
    finally:
        db.close()


def test_actual_subprocess_analysis_recognizes_content_and_preserves_source(history, tmp_path):
    renamed = history.with_name("renamed.bin")
    history.rename(renamed)
    before = renamed.read_bytes()
    work = tmp_path / "work"
    result = analyze_file(renamed, work)
    assert "error" not in result and len(result["records"]) == 3
    assert result["sha256"] == hashlib.sha256(before).hexdigest()
    assert result["artifacts"][0]["key"] == result["records"][0]["artifact_key"]
    step = next(step for step in result["coverage"]["steps"] if step["id"] == "chrome-history")
    assert step["status"] == "complete" and step["processed"] == 3
    assert result["coverage"]["limits"]["parsed_records"] == 10000
    assert renamed.read_bytes() == before
    assert not list(work.glob("content-*"))


@pytest.mark.parametrize("failure,reason", [
    (analysis.ToolLimitError("tool_timeout", "Parser timed out"), "tool_timeout"),
    (analysis.ToolLimitError("tool_output_limit", "Parser output exceeded budget"), "tool_output_limit"),
    (ValueError("Parser failure"), "content_parser_error"),
])
def test_content_failure_keeps_source_inventory_and_hash(history, tmp_path, monkeypatch, failure, reason):
    def fail(*_args):
        raise failure
    monkeypatch.setattr(analysis, "_capture", fail)
    result = analyze_file(history, tmp_path / "work")
    assert "error" not in result and result["sha256"] and len(result["artifacts"]) == 1
    assert not result["records"]
    step = next(step for step in result["coverage"]["steps"] if step["id"] == "chrome-history")
    assert step["status"] == "failed" and step["reason"] == reason
    assert result["coverage"]["status"] == "partial"
    assert not list((tmp_path / "work").glob("content-*"))


def test_file_limit_skips_parser_before_copy(history, tmp_path, monkeypatch):
    monkeypatch.setitem(CONTENT_LIMITS, "content_file_bytes", 10)
    result = analyze_file(history, tmp_path / "work")
    assert result["sha256"] and not result["records"]
    assert result["coverage"]["steps"][-1]["reason"] == "content_file_limit"


def test_nonfinite_numeric_fields_remain_exportable(history):
    with sqlite3.connect(history) as db:
        db.execute("UPDATE visits SET visit_time=?,transition=? WHERE id=1", (float('inf'), float('-inf')))
    result = parse(history)
    first = result['records'][0]
    assert first['event_time_us'] is None
    assert first['details']['original_timestamp'] == 'inf'
    assert first['details']['transition'] == '-inf'
    assert first['details']['nonfinite_fields'] == ['original_timestamp', 'transition']
    assert result['status'] == 'partial'
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('table', ['visits', 'urls', 'visit_source'])
def test_composite_keys_cannot_multiply_visits(history, table):
    with sqlite3.connect(history) as db:
        db.execute(f'DROP TABLE {table}')
        if table == 'visits':
            db.execute('CREATE TABLE visits(id INTEGER,url INTEGER,visit_time INTEGER,PRIMARY KEY(id,url))')
        elif table == 'urls':
            db.execute('CREATE TABLE urls(id INTEGER,url TEXT,PRIMARY KEY(id,url))')
        else:
            db.execute('CREATE TABLE visit_source(id INTEGER,source INTEGER,PRIMARY KEY(id,source))')
            db.executemany('INSERT INTO visit_source VALUES(1,?)', [(0,), (1,)])
    result = parse(history)
    if table == 'visit_source':
        assert result['status'] == 'partial' and len(result['records']) == 3
        assert 'source attribution was not read' in result['detail']
    else:
        assert result['status'] == 'unsupported' and not result['records']


def test_changed_source_is_not_paired_with_the_pre_copy_hash(history, tmp_path, monkeypatch):
    inspect = analysis.inspect_history
    def change_source(path, result, settings, work_dir):
        with sqlite3.connect(path) as db:
            db.execute("UPDATE urls SET title='Changed' WHERE id=1")
        inspect(path, result, settings, work_dir)
    monkeypatch.setattr(analysis, 'inspect_history', change_source)
    result = analyze_file(history, tmp_path / 'work')
    assert not result['records']
    assert result['coverage']['status'] == 'partial'
    assert 'Source changed between hashing' in result['coverage']['steps'][-1]['detail']
    assert not list((tmp_path / 'work').glob('content-*'))


def test_large_odd_integers_keep_every_digit(history):
    with sqlite3.connect(history) as db:
        db.execute('UPDATE visits SET visit_time=13344473600123457,originator_visit_id=9007199254740993 WHERE id=1')
    record = parse(history)['records'][0]
    assert record['event_time_us'] == 1700000000123457
    assert record['details']['original_timestamp'] == '13344473600123457'
    assert record['details']['originator_visit_id'] == '9007199254740993'
    assert record['details']['integer_text_fields'] == ['original_timestamp', 'originator_visit_id']
