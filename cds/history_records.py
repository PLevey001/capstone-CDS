"""Shared bounds and record handling for standalone browser-history snapshots."""

import contextlib
import json
import math
import sqlite3

import cds.timestamps as timestamps

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


def bounded(column, text_limit):
    # Nominal integer fields can contain arbitrary text in an evidence database.
    return (f"CASE WHEN typeof({column}) IN ('text','blob') THEN substr({column},1,{text_limit + 1}) "
            f"ELSE {column} END")


def read_history(path, limits, readers):
    """Readers recognize their own schema and supply lazy (time, details) rows."""
    result = {"id": "browser-history", "label": "Browser history", "parser": "browser-history/1",
              "status": "complete", "reason": "history_read", "detail": SNAPSHOT_SCOPE,
              "processed": 0, "total": None, "records": []}
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
            matches = []
            for reader in readers:
                match = reader(db, limits)
                if match is not None:
                    matches.append(match)
            if len(matches) != 1:
                detail = ("SQLite file has multiple supported browser schemas; no parser was chosen. " if matches
                          else "SQLite file does not have a supported browser history schema. ")
                result.update(status="unsupported", reason="ambiguous_schema" if matches else "unsupported_schema",
                              detail=detail + SNAPSHOT_SCOPE)
                return result
            match = matches[0]
            result.update({key: match[key] for key in ("id", "label", "parser", "total")})
            issues = match["issues"]
            total = result["total"]
            text_limit = limits["record_text_chars"]
            payload_bytes = 2  # Array brackets; each record also accounts for its separator.
            missing_urls = invalid_times = truncated_fields = nonfinite_fields = 0
            for event_time, details in match["rows"]:
                if not isinstance(details["visit_id"], int):
                    raise sqlite3.DataError("Browser visit ID is not an integer.")
                source_key = str(details["visit_id"])
                shortened = []
                for name, value in list(details.items()):
                    # Decimal text preserves integers that browser JSON numbers would round.
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
                if details.get("url_missing"):
                    missing_urls += 1
                if event_time is not None:
                    try:
                        timestamps.from_microseconds(event_time)
                    except (OverflowError, ValueError):
                        event_time = None
                if event_time is None:
                    invalid_times += 1
                    details["timestamp_note"] = "Unavailable, invalid, or outside the supported UTC date range; omitted from the timeline."
                record = {"artifact_key": "source", "kind": "browser_visit", "source_key": source_key,
                          "event_time_us": event_time, "summary": str(details["title"] or details["url"] or f"Visit {source_key}"),
                          "parser": result["parser"], "details": details}
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
        result.update(status="failed", reason="database_unreadable", records=[], processed=0,
                      detail=f"SQLite snapshot could not be read: {str(error)[:300]}. {SNAPSHOT_SCOPE}")
    return result
