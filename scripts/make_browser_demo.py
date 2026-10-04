"""Generate invented browser-schema visits for demos and regression tests."""

import argparse
from pathlib import Path
import sqlite3


def make_chrome_history(path, count=3, optional=True):
    # Never overwrite a database supplied by an examiner.
    with path.open("xb"):
        pass
    db = sqlite3.connect(path)
    try:
        with db:
            db.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT,title TEXT)")
            extras = ",transition INTEGER,originator_cache_guid TEXT,originator_visit_id INTEGER" if optional else ""
            db.execute(f"CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,visit_time INTEGER{extras})")
            db.execute("CREATE TABLE visit_source(id INTEGER PRIMARY KEY,source INTEGER)")
            db.executemany("INSERT INTO urls VALUES(?,?,?)", [
                (1, "https://example.test/repeated", "Résumé & research"),
                (2, "https://example.test/100%_done", "=2+2"),
            ])
            for index in range(count):
                # Independently specified anchor: 2023-11-14 22:13:20.123456 UTC.
                values = [index + 1, 2 if index == count - 1 else 1, 13344473600123456 + index * 60_000_000]
                if optional:
                    values += [1, "synthetic-other-device" if index == count - 1 else "", 900 if index == count - 1 else 0]
                db.execute(f"INSERT INTO visits VALUES({','.join('?' for _ in values)})", values)
            if count:
                db.execute("INSERT INTO visit_source VALUES(?,0)", (count,))
    finally:
        db.close()
    return path


def make_firefox_history(path, count=3, optional=True):
    with path.open("xb"):
        pass
    db = sqlite3.connect(path)
    try:
        with db:
            db.execute("CREATE TABLE moz_places(id INTEGER PRIMARY KEY,url TEXT,title TEXT,guid TEXT)")
            extras = ",from_visit INTEGER,visit_type INTEGER,session INTEGER,source INTEGER,triggeringPlaceId INTEGER" if optional else ""
            db.execute(f"CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY,place_id INTEGER,visit_date INTEGER{extras})")
            db.execute("CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY,fk INTEGER,title TEXT)")
            db.executemany("INSERT INTO moz_places VALUES(?,?,?,?)", [
                (1, "https://firefox.example.test/repeated", "Firefox résumé", "firefox-url1"),
                (2, "https://firefox.example.test/100%_done", "=2+2", "firefox-url2"),
                (3, "https://firefox.example.test/unvisited", "No visit", "firefox-url3"),
                (4, "https://firefox.example.test/bookmark-only", "Bookmark only", "firefox-url4"),
            ])
            db.execute("INSERT INTO moz_bookmarks VALUES(1,4,'Unvisited bookmark')")
            for index in range(count):
                # Same independently specified UTC anchor as the Chrome fixture.
                values = [index + 1, 2 if index == count - 1 else 1, 1700000000123456 + index * 60_000_000]
                if optional:
                    values += [index, 1, 0, 0, 0]
                db.execute(f"INSERT INTO moz_historyvisits VALUES({','.join('?' for _ in values)})", values)
    finally:
        db.close()
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", choices=["chrome", "firefox"], default="chrome")
    args = parser.parse_args()
    directory = Path("demo-evidence")
    directory.mkdir(exist_ok=True)
    if args.browser == "firefox":
        print(make_firefox_history(directory / "Firefox-places.sqlite"))
    else:
        print(make_chrome_history(directory / "Chrome-History.sqlite"))
