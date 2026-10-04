"""Read Chrome/Chromium visit records from a disposable standalone SQLite copy.

Schema references: Chromium's components/history/core/browser/{visit,url}_database.cc.
Time values follow base/time/time.h: integer microseconds since 1601-01-01 UTC.
"""

import contextlib
import json
import math
import sqlite3

import cds.timestamps as timestamps

PARSER = "chrome-history/1"
CHROME_EPOCH_OFFSET = 11_644_473_600_000_000
SNAPSHOT_SCOPE = "Standalone database snapshot only. Separate WAL/journal files were not supplied or examined."


def table_columns(db, name):
    row = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (name,)).fetchone()
    if row is None or not row[0] or "VIRTUAL" in row[0].upper():
        return {}
    # Callers supply fixed, known table names, never names taken from the file.
    return {row["name"]: row for row in db.execute(f"PRAGMA table_info({name})")}


def has_integer_id(columns):
    return ("id" in columns and columns["id"]["pk"] == 1
            and columns["id"]["type"].upper() == "INTEGER"
            and sum(bool(column["pk"]) for column in columns.values()) == 1)


def read_history(path, limits):
    result = {"status": "complete", "reason": "history_read", "detail": SNAPSHOT_SCOPE,
              "processed": 0, "total": None, "records": []}
    issues = []
    replaced_text = False

    def decode_text(value):
        nonlocal replaced_text
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            replaced_text = True
            return value.decode("utf-8", errors="replace")

    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)) as db:
            db.row_factory = sqlite3.Row
            db.text_factory = decode_text
            db.enable_load_extension(False)
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            visits, urls = table_columns(db, "visits"), table_columns(db, "urls")
            if not ({"id", "url", "visit_time"} <= visits.keys() and {"id", "url"} <= urls.keys()
                    and has_integer_id(visits) and has_integer_id(urls)):
                result.update(status="unsupported", reason="unsupported_schema",
                              detail="SQLite file does not have a supported Chrome/Chromium history schema. " + SNAPSHOT_SCOPE)
                return result
            total = result["total"] = db.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
            text_limit = limits["record_text_chars"]
            def bounded(column):
                return (f"CASE WHEN typeof({column}) IN ('text','blob') THEN substr({column},1,{text_limit + 1}) "
                        f"ELSE {column} END")

            columns = ["v.id", f"{bounded('v.url')} AS url_id", f"{bounded('v.visit_time')} AS visit_time", "u.id AS matched_url_id",
                       f"substr(u.url,1,{text_limit + 1}) AS url",
                       f"substr(u.title,1,{text_limit + 1}) AS title" if "title" in urls else "NULL AS title"]
            optional = ("from_visit", "transition", "visit_duration", "opener_visit", "originator_cache_guid",
                        "originator_visit_id", "originator_from_visit", "originator_opener_visit", "is_known_to_sync")
            for name in optional:
                if name in visits:
                    # Even nominal integer fields can contain arbitrary text in an evidence database.
                    columns.append(f"{bounded('v.' + name)} AS {name}")
            source_table = table_columns(db, "visit_source")
            join_source = {"id", "source"} <= source_table.keys() and has_integer_id(source_table)
            if source_table and not join_source and "source" not in visits:
                issues.append("Unsupported visit_source table; source attribution was not read.")
            if "source" in visits:
                columns.append(f"{bounded('v.source')} AS visit_source")
            elif join_source:
                columns.append(f"{bounded('s.source')} AS visit_source")
            else:
                columns.append("NULL AS visit_source")
            query = f"SELECT {','.join(columns)} FROM visits v LEFT JOIN urls u ON u.id=v.url "
            if join_source and "source" not in visits:
                query += "LEFT JOIN visit_source s ON s.id=v.id "
            query += "ORDER BY v.id LIMIT ?"
            payload_bytes = 2  # JSON array brackets; each record also accounts for its separator.
            missing_urls = invalid_times = truncated_fields = nonfinite_fields = 0
            for row in db.execute(query, (limits["parsed_records"],)):
                details = {"browser": "Chrome/Chromium", "visit_id": row["id"], "url_id": row["url_id"],
                           "url": row["url"], "title": row["title"], "visit_source": row["visit_source"],
                           "original_timestamp": row["visit_time"], "timestamp_unit": "microseconds",
                           "timestamp_epoch": "1601-01-01T00:00:00Z"}
                details.update({name: row[name] for name in optional if name in visits})
                shortened = []
                for name, value in list(details.items()):
                    # Browser JSON numbers lose precision above 2**53 - 1.
                    # Decimal text retains the exact integer, including Chrome's epoch value.
                    if isinstance(value, int) and abs(value) > 2**53 - 1:
                        details[name] = str(value)
                        details.setdefault("integer_text_fields", []).append(name)
                    if isinstance(value, float) and not math.isfinite(value):
                        details[name] = str(value)
                        details.setdefault("nonfinite_fields", []).append(name)
                        nonfinite_fields += 1
                    if isinstance(value, bytes):
                        value = decode_text(value)
                        details[name] = value
                    if isinstance(value, str) and len(value) > text_limit:
                        details[name] = value[:text_limit]
                        shortened.append(name)
                if shortened:
                    details["truncated_fields"] = shortened
                    truncated_fields += 1
                if row["matched_url_id"] is None:
                    missing_urls += 1
                    details["url_missing"] = True
                original = row["visit_time"]
                event_time = None
                if isinstance(original, int) and original != 0:
                    try:
                        event_time = original - CHROME_EPOCH_OFFSET
                        timestamps.from_microseconds(event_time)
                    except (OverflowError, ValueError):
                        event_time = None
                if event_time is None:
                    invalid_times += 1
                    details["timestamp_note"] = "Zero, missing, invalid, or outside the supported UTC date range; omitted from the timeline."
                record = {"artifact_key": "source", "kind": "browser_visit", "source_key": str(row["id"]),
                          "event_time_us": event_time, "summary": str(details["title"] or details["url"] or f"Visit {row['id']}"),
                          "parser": PARSER, "details": details}
                size = len(json.dumps(record, ensure_ascii=True, allow_nan=False).encode("utf-8")) + 2
                if payload_bytes + size > limits["record_payload_bytes"]:
                    issues.append("Saved record payload reached its byte limit.")
                    result["reason"] = "record_payload_limit"
                    break
                result["records"].append(record)
                payload_bytes += size
            result["processed"] = len(result["records"])
            if result["processed"] < total and result["reason"] != "record_payload_limit":
                issues.append("Saved records reached the record-count limit.")
                result["reason"] = "record_limit"
            for count, description in ((missing_urls, "visits reference a missing URL"),
                                       (invalid_times, "visits have no usable timestamp"),
                                       (truncated_fields, "records contain shortened fields"),
                                       (nonfinite_fields, "non-finite numeric fields were preserved as text")):
                if count:
                    issues.append(f"{count} {description}.")
            if replaced_text:
                issues.append("Invalid UTF-8 was replaced in saved text.")
            if issues:
                result["status"] = "partial"
                if result["reason"] == "history_read":
                    result["reason"] = "record_quality"
            if not total:
                result["reason"] = "empty_history"
            keys = [record["source_key"] for record in result["records"]]
            result["detail"] = (f"Saved {len(keys)} of {total} visits, ordered by original visit ID. "
                                + (f"Saved ID range: {keys[0]} through {keys[-1]}. " if keys else "")
                                + " ".join(issues) + " " + SNAPSHOT_SCOPE)
    except sqlite3.DatabaseError as error:
        # A failed query must not be presented as a complete or empty history.
        result.update(status="failed", reason="database_unreadable", records=[], processed=0,
                      detail=f"SQLite snapshot could not be read: {str(error)[:300]}. {SNAPSHOT_SCOPE}")
    return result
