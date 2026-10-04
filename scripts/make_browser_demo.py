"""Generate invented Chrome-schema visits for demos and regression tests."""

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


if __name__ == "__main__":
    directory = Path("demo-evidence")
    directory.mkdir(exist_ok=True)
    print(make_chrome_history(directory / "Chrome-History.sqlite"))
