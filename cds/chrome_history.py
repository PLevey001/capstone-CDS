"""Chrome/Chromium visit schema and integer microseconds since 1601.

Reference: Chromium components/history/core/browser/{visit,url}_database.cc
and base/time/time.h.
"""

import cds.history_records as history_records

PARSER = "chrome-history/1"
CHROME_EPOCH_OFFSET = 11_644_473_600_000_000


def read_history(path, limits):
    return history_records.read_history(path, limits, [read_visits])


def read_visits(db, limits):
    visits = history_records.table_columns(db, "visits")
    urls = history_records.table_columns(db, "urls")
    if not ({"id", "url", "visit_time"} <= visits.keys() and {"id", "url"} <= urls.keys()
            and history_records.has_integer_id(visits) and history_records.has_integer_id(urls)):
        return None
    text_limit = limits["record_text_chars"]
    columns = ["v.id", f"{history_records.bounded('v.url', text_limit)} AS url_id",
               f"{history_records.bounded('v.visit_time', text_limit)} AS visit_time", "u.id AS matched_url_id",
               f"substr(u.url,1,{text_limit + 1}) AS url",
               f"substr(u.title,1,{text_limit + 1}) AS title" if "title" in urls else "NULL AS title"]
    optional = ("from_visit", "transition", "visit_duration", "opener_visit", "originator_cache_guid",
                "originator_visit_id", "originator_from_visit", "originator_opener_visit", "is_known_to_sync")
    for name in optional:
        if name in visits:
            columns.append(f"{history_records.bounded('v.' + name, text_limit)} AS {name}")
    source_table = history_records.table_columns(db, "visit_source")
    join_source = {"id", "source"} <= source_table.keys() and history_records.has_integer_id(source_table)
    issues = []
    if source_table and not join_source and "source" not in visits:
        issues.append("Unsupported visit_source table; source attribution was not read.")
    if "source" in visits:
        columns.append(f"{history_records.bounded('v.source', text_limit)} AS visit_source")
    elif join_source:
        columns.append(f"{history_records.bounded('s.source', text_limit)} AS visit_source")
    else:
        columns.append("NULL AS visit_source")
    query = f"SELECT {','.join(columns)} FROM visits v LEFT JOIN urls u ON u.id=v.url "
    if join_source and "source" not in visits:
        query += "LEFT JOIN visit_source s ON s.id=v.id "
    query += "ORDER BY v.id LIMIT ?"

    def rows():
        for row in db.execute(query, (limits["parsed_records"],)):
            details = {"browser": "Chrome/Chromium", "visit_id": row["id"], "url_id": row["url_id"],
                       "url": row["url"], "title": row["title"], "visit_source": row["visit_source"],
                       "original_timestamp": row["visit_time"], "timestamp_unit": "microseconds",
                       "timestamp_epoch": "1601-01-01T00:00:00Z"}
            details.update({name: row[name] for name in optional if name in visits})
            if row["matched_url_id"] is None:
                details["url_missing"] = True
            original = row["visit_time"]
            # Zero is Chrome's unavailable-time sentinel, not a visit in 1601.
            when = original - CHROME_EPOCH_OFFSET if isinstance(original, int) and original != 0 else None
            yield when, details

    return {"id": "chrome-history", "label": "Chrome/Chromium history", "parser": PARSER,
            "total": db.execute("SELECT COUNT(*) FROM visits").fetchone()[0], "rows": rows(), "issues": issues}
