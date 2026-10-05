"""Shared bounds and record handling for browser-history snapshots and working sets."""

import contextlib
import json
import math
import sqlite3
import struct

import cds.timestamps as timestamps

SNAPSHOT_SCOPE = "Standalone database snapshot only. Separate WAL/journal files were not supplied or examined."
JOURNAL_SCOPE = ("Matching WAL/SHM sidecars were included in a disposable working set. "
                 "Visits may originate from committed WAL content; individual visits are not attributed to a file.")
SIDECAR_GAP_SCOPE = "Sidecars are present, but the working set could not be fully examined; journal coverage has a gap."


def _working_set_issue(path):
    """Reject visibly damaged journals that SQLite could otherwise silently ignore."""
    # SQLite's documented checksums distinguish usable frames from damaged or stale tails.
    # This checks supplied bytes only; it does not recover transactions or replace SQLite's reader.
    # https://www.sqlite.org/fileformat2.html#walformat and https://www.sqlite.org/walformat.html
    def checksum(data, order, first=0, second=0):
        for left, right in struct.iter_unpack(order + "II", data):
            first = (first + left + second) & 0xFFFFFFFF
            second = (second + right + first) & 0xFFFFFFFF
        return first, second

    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        with path.open("rb") as source:
            database_header = source.read(100)
        with wal.open("rb") as source:
            header = source.read(32)
            if len(header) != 32:
                return "WAL header is truncated."
            magic, version, page_size = struct.unpack(">III", header[:12])
            if magic not in (0x377F0682, 0x377F0683) or version != 3007000:
                return "WAL header is invalid or unsupported."
            database_page_size = int.from_bytes(database_header[16:18], "big")
            if database_page_size == 1:
                database_page_size = 65536
            if (page_size < 512 or page_size > 65536 or page_size & (page_size - 1)
                    or page_size != database_page_size or database_header[18:20] != b"\x02\x02"):
                return "WAL page size or database journal mode does not match the main database."
            order = ">" if magic & 1 else "<"
            sums = checksum(header[:24], order)
            if sums != struct.unpack(">II", header[24:]):
                return "WAL header checksum is invalid."
            while frame := source.read(24 + page_size):
                if len(frame) != 24 + page_size:
                    return "WAL frame is truncated; recovery was not attempted."
                if frame[8:16] != header[16:24] or int.from_bytes(frame[:4], "big") == 0:
                    return "WAL contains an invalid or stale frame; recovery was not attempted."
                sums = checksum(frame[:8] + frame[24:], order, *sums)
                if sums != struct.unpack(">II", frame[16:24]):
                    return "WAL frame checksum is invalid; recovery was not attempted."

    shm = path.with_name(path.name + "-shm")
    if shm.exists():
        with shm.open("rb") as source:
            header = source.read(96)
        if len(header) != 96 or shm.stat().st_size % 32768:
            return "SHM size is invalid or truncated."
        # SHM uses the originating host's byte order; the evidence need not come from this host.
        order = "<" if int.from_bytes(header[:4], "little") == 3007000 else ">"
        if (struct.unpack(order + "I", header[:4])[0] != 3007000 or header[12] != 1
                or header[:48] != header[48:96]
                or checksum(header[:40], order) != struct.unpack(order + "II", header[40:48])):
            return "SHM header is invalid or unsupported; recovery was not attempted."
    return None


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


def read_history(path, limits, readers, *, journal_aware=False):
    """Readers recognize their own schema and supply lazy (time, details) rows."""
    scope = SNAPSHOT_SCOPE
    if journal_aware:
        scope = JOURNAL_SCOPE if path.with_name(path.name + "-wal").exists() else (
            "Matching SHM sidecar was included in a disposable working set. No WAL was present; base database only.")
    result = {"id": "browser-history", "label": "Browser history", "parser": "browser-history/1",
              "status": "complete", "reason": "history_read", "detail": scope,
              "processed": 0, "total": None, "records": []}
    if journal_aware:
        result["scope"] = scope
    replaced_text = False

    def decode_text(value):
        nonlocal replaced_text
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            replaced_text = True
            return value.decode("utf-8", errors="replace")

    try:
        if journal_aware:
            issue = _working_set_issue(path)
            if issue:
                result.update(status="failed", reason="sidecar_unusable", detail=issue + " " + SIDECAR_GAP_SCOPE)
                return result
        # mode=ro may create or change SHM, so only the image working-set caller opts in.
        options = "?mode=ro" if journal_aware else "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + options, uri=True, timeout=0)) as db:
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
                              detail=detail + scope)
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
                                + " ".join(issues) + " " + scope)
    except sqlite3.DatabaseError as error:
        result.update(status="failed", reason="database_unreadable", records=[], processed=0,
                      detail=f"SQLite snapshot could not be read: {str(error)[:300]}. "
                             + (SIDECAR_GAP_SCOPE if journal_aware else scope))
    return result
