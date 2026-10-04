"""Firefox Places visit schema and integer microseconds since the Unix epoch.

References: Mozilla toolkit/components/places/nsPlacesTables.h, History.sys.mjs,
and nsprpub/pr/include/prtime.h.
Only moz_historyvisits rows are visits; stored places and bookmarks alone are not.
"""

import cds.history_records as history_records

PARSER = "firefox-history/1"


def read_history(path, limits):
    return history_records.read_history(path, limits, [read_visits])


def read_visits(db, limits):
    visits = history_records.table_columns(db, "moz_historyvisits")
    places = history_records.table_columns(db, "moz_places")
    if not ({"id", "place_id", "visit_date"} <= visits.keys() and {"id", "url"} <= places.keys()
            and history_records.has_integer_id(visits) and history_records.has_integer_id(places)):
        return None
    text_limit = limits["record_text_chars"]
    columns = ["v.id", f"{history_records.bounded('v.place_id', text_limit)} AS place_id",
               f"{history_records.bounded('v.visit_date', text_limit)} AS visit_date", "p.id AS matched_place_id",
               f"substr(p.url,1,{text_limit + 1}) AS url",
               f"substr(p.title,1,{text_limit + 1}) AS title" if "title" in places else "NULL AS title"]
    optional = ("from_visit", "visit_type", "session", "source", "triggeringPlaceId")
    for name in optional:
        if name in visits:
            columns.append(f"{history_records.bounded('v.' + name, text_limit)} AS {name}")
    if "guid" in places:
        columns.append(f"substr(p.guid,1,{text_limit + 1}) AS place_guid")
    query = f"SELECT {','.join(columns)} FROM moz_historyvisits v LEFT JOIN moz_places p ON p.id=v.place_id ORDER BY v.id LIMIT ?"

    def rows():
        for row in db.execute(query, (limits["parsed_records"],)):
            details = {"browser": "Firefox", "visit_id": row["id"], "place_id": row["place_id"],
                       "url": row["url"], "title": row["title"], "original_timestamp": row["visit_date"],
                       "timestamp_unit": "microseconds", "timestamp_epoch": "1970-01-01T00:00:00Z"}
            details.update({name: row[name] for name in optional if name in visits})
            if "guid" in places:
                details["place_guid"] = row["place_guid"]
            if row["matched_place_id"] is None:
                details["url_missing"] = True
            original = row["visit_date"]
            # PRTime is already Unix microseconds. Do not apply Chrome's offset or zero sentinel.
            when = original if isinstance(original, int) else None
            yield when, details

    return {"id": "firefox-history", "label": "Firefox history", "parser": PARSER,
            "total": db.execute("SELECT COUNT(*) FROM moz_historyvisits").fetchone()[0], "rows": rows(), "issues": []}
