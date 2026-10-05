"""Create small, synthetic fixtures with known contents; no external tools needed."""
import argparse
import hashlib
import json
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

# Also support the documented `python scripts/make_demo.py` entry point.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NOTE = b"CDS synthetic evidence only.\r\nA USB device contains a report and an activity export.\r\n"
EVENTS = b"timestamp,event,filename\n2026-09-16T10:30:00Z,created,REPORT.TXT\n2026-09-16T10:35:00Z,deleted,SECRET.TXT\n"
DELETED = b"Synthetic deleted file. These bytes have not been overwritten.\r\n"


def fat12_image():
    disk = bytearray(2880 * 512)
    disk[:3] = b"\xeb\x3c\x90"
    disk[3:11] = b"CDSDEMO "
    struct.pack_into("<HBHBHHBHHHII", disk, 11, 512, 1, 1, 2, 224, 2880, 0xF0, 9, 18, 2, 0, 0)
    disk[36:39] = b"\x00\x00\x29"
    struct.pack_into("<I", disk, 39, 0x43445301)
    disk[43:54] = b"CDS DEMO   "
    disk[54:62] = b"FAT12   "
    disk[510:512] = b"\x55\xaa"
    fat = bytearray(9 * 512)
    fat[:3] = b"\xf0\xff\xff"
    for cluster in (2, 3):
        offset = cluster + cluster // 2
        value = int.from_bytes(fat[offset:offset+2], "little")
        value = (value & 0x000F) | 0xFFF0 if cluster % 2 else (value & 0xF000) | 0x0FFF
        fat[offset:offset+2] = value.to_bytes(2, "little")
    disk[512:10*512] = fat
    disk[10*512:19*512] = fat
    fat_date = ((2026 - 1980) << 9) | (9 << 5) | 16
    fat_time = (10 << 11) | (30 << 5)
    files = [(b"REPORT  TXT", NOTE, 2), (b"EVENTS  CSV", EVENTS, 3), (b"\xe5ECRET  TXT", DELETED, 4)]
    for index, (name, content, cluster) in enumerate(files):
        entry = bytearray(32)
        entry[:11] = name
        entry[11] = 0x20
        struct.pack_into("<HHH", entry, 14, fat_time, fat_date, fat_date)
        struct.pack_into("<HHHI", entry, 22, fat_time, fat_date, cluster, len(content))
        root_offset = 19 * 512 + index * 32
        disk[root_offset:root_offset+32] = entry
        data_offset = (33 + cluster - 2) * 512
        disk[data_offset:data_offset+len(content)] = content
    return bytes(disk)


def partitioned_image():
    volume = fat12_image()
    start = 2048
    disk = bytearray(start * 512 + len(volume))
    disk[446:462] = struct.pack("<B3sB3sII", 0, b"\x00\x02\x00", 1, b"\xfe\xff\xff", start, 2880)
    disk[510:512] = b"\x55\xaa"
    disk[start*512:] = volume
    return bytes(disk)


def make_demo(directory):
    directory.mkdir(parents=True, exist_ok=True)
    files = {"usb-demo.img": partitioned_image(), "investigation-notes.txt": NOTE, "activity-export.csv": EVENTS,
             "device-summary.json": json.dumps({"synthetic": True, "device": "CDS-DEMO-USB", "owner": "Demo user",
                                                "purpose": "Capstone ingestion demonstration"}, indent=2).encode()}
    manifest = {}
    for name, content in files.items():
        (directory / name).write_bytes(content)
        manifest[name] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def make_acceptance(directory):
    """One fixed case, composed from the shipped demo and Stage 6–8 builders.

    All answers below are construction inputs, never CDS/TSK parser output.
    Volume IDs and WAL salts can vary; logical answers are reproducible.
    """
    import sqlite3

    from scripts.make_browser_demo import make_chrome_history, make_firefox_history
    from tests.evtx_fixtures import EVENTS as LOG_EVENTS, make_evtx
    from tests.filesystem_fixtures import CHROME, FIREFOX, make_filesystem_image, make_sources, missing_tools, run
    from tests.registry_fixtures import checksum, make_hive

    missing = missing_tools()
    if missing:
        raise RuntimeError("Missing acceptance fixture tools: " + ", ".join(missing))
    directory.mkdir(parents=True)  # Refuse to overwrite previous evidence or answers.
    manifest = {"name": "Stage 10 · Combined acceptance", "sources": {}, "filesystems": {},
                "not_covered": [
                    "E01 deliberately excluded: local TSK 4.12.1/libewf 20140816 segfaults on EWF input; Stage 9 skipped.",
                    "Segmented uploads; standalone WAL replay; new parsers or record families.",
                    "Unallocated carving, encrypted/compressed content, NTFS alternate streams, ext2/ext3.",
                    "Registry transaction-log replay and deleted cells; EVTX message rendering and carved records.",
                    "Forensic certification, exhaustive format/version compatibility, unlimited scale."]}

    def source(name, data, records=(), kind="file", sector=512):
        (directory / name).write_bytes(data)
        manifest["sources"][name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                      "kind": kind, "sector_size": sector, "records": list(records)}

    # Reuse Stage 6's source tree, replacing only the browser inputs for this case.
    files = make_sources(directory / "browser-sources")
    browser_answers = []
    for name, profile, builder, table, column, epoch, parser, url_root in (
        ("Chrome-History.sqlite", CHROME, make_chrome_history, "visits", "visit_time", 11644473600000000,
         "chrome-history/1", "https://example.test/"),
        ("Firefox-places.sqlite", FIREFOX, make_firefox_history, "moz_historyvisits", "visit_date", 0,
         "firefox-history/1", "https://firefox.example.test/"),
    ):
        path = builder(directory / name, count=60)
        db = sqlite3.connect(path)
        # The first browser visit coincides with filesystem seconds, a Registry
        # FILETIME and an EVTX SystemTime. Later visits retain microseconds.
        db.execute(f"UPDATE {table} SET {column}=? WHERE id=1", (1700000000000000 + epoch,))
        db.commit()
        answers = [{"kind": "browser_visit", "parser": parser, "identity": str(index + 1),
                    "at": ("2023-11-14T22:13:20+00:00" if index == 0 else
                           datetime.fromtimestamp(1700000000 + index * 60, timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%S.123456+00:00")),
                    "url": url_root + ("100%_done" if index == 59 else "repeated")}
                   for index in range(60)]
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute(f"INSERT INTO {table}(id,{'url' if table == 'visits' else 'place_id'},{column}) VALUES(61,1,?)",
                   (1700003600123456 + epoch,))
        db.commit()
        working_set = {suffix: Path(str(path) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")}
        db.close()
        # Restore the uncheckpointed base snapshot after closing the writer.
        source(name, working_set[""], answers)
        for suffix in ("-wal", "-shm"):
            source(name + suffix, working_set[suffix])
        for suffix, data in working_set.items():
            files[profile + suffix] = data
        browser_answers += [{**answer, "path": profile} for answer in answers]
        browser_answers.append({"kind": "browser_visit", "parser": parser, "identity": "61",
                                "path": profile, "at": "2023-11-14T23:13:20.123456+00:00",
                                "url": url_root + "repeated"})

    # Set independently chosen key times directly in the fixture cells. Values
    # have no event time; they must never inherit their parent's last-write time.
    hive, hive_manifest = make_hive()
    hive = bytearray(hive)
    key_times = [("ROOT", "ROOT", 1700000000000000, "2023-11-14T22:13:20+00:00"),
                 ("Software", "ROOT\\Software", 1700000060000000, "2023-11-14T22:14:20+00:00"),
                 ("研究", "ROOT\\Software\\研究", 1700000120123456, "2023-11-14T22:15:20.123456+00:00")]
    hive_answers = []
    for cell, key, micros, at in key_times:
        struct.pack_into("<Q", hive, hive_manifest["cells"][cell] + 4, (micros + 11644473600000000) * 10 + 7)
        hive_answers.append({"kind": "registry_key", "parser": "windows-registry/1", "identity": key, "at": at})
    checksum(hive)
    hive_answers += [{"kind": "registry_value", "parser": "windows-registry/1",
                      "identity": value["name"], "at": None, "data": value["data"]}
                     for value in hive_manifest["values"]]
    source("NTUSER.DAT", hive, hive_answers)
    hive_path = "/Users/examiner/NTUSER.DAT"
    files[hive_path] = bytes(hive)

    # Offset-bearing and seven-digit timestamps share the browser UTC scale.
    events = [dict(event) for event in LOG_EVENTS]
    events[0].update(time="2023-11-14T17:13:20.0000007-05:00", event_time_us=1700000000000000,
                     at="2023-11-14T22:13:20+00:00")
    events[2].update(time="2023-11-14T22:15:20.1234567Z", event_time_us=1700000120123456,
                     at="2023-11-14T22:15:20.123456+00:00")
    evtx, _ = make_evtx(events)
    event_answers = [{"kind": "windows_event", "parser": "windows-event-log/1", "identity": event["record_id"],
                      "at": event["at"], "event_id": event["event_id"]} for event in events]
    source("Application.evtx", evtx, event_answers)
    log_path = "/Windows/System32/winevt/Logs/Application.evtx"
    files[log_path] = evtx
    inner_answers = browser_answers + [{**answer, "path": hive_path} for answer in hive_answers]
    inner_answers += [{**answer, "path": log_path} for answer in event_answers]

    # Existing FAT fixture gives a small known-file extraction and a retained
    # deleted cluster. NTFS/ext4 exercise real native writers without mounting.
    source("fat.img", partitioned_image(), kind="raw_image")
    manifest["fat_files"] = {
        "/REPORT.TXT": {"size": len(NOTE), "sha256": hashlib.sha256(NOTE).hexdigest(), "deleted": False},
        "/EVENTS.CSV": {"size": len(EVENTS), "sha256": hashlib.sha256(EVENTS).hexdigest(), "deleted": False},
        "/_ECRET.TXT (deleted)": {"size": len(DELETED), "sha256": hashlib.sha256(DELETED).hexdigest(), "deleted": True},
    }
    for filesystem in ("ntfs", "ext4"):
        image_files = dict(files)
        answers = list(inner_answers)
        if filesystem == "ntfs":
            # Deliberately omit the journal of another Chrome profile. Its base
            # 60 visits survive; visit 61 must not be borrowed from Default.
            missing_path = CHROME.replace("Default", "MissingJournal")
            image_files[missing_path] = files[CHROME]
            answers += [{**answer, "path": missing_path} for answer in browser_answers
                        if answer["path"] == CHROME and answer["identity"] != "61"]
        image, expected = make_filesystem_image(directory / filesystem, filesystem, 512,
                                                filesystem == "ntfs", image_files)
        if filesystem == "ext4":
            # A bare ext4 volume is intentional (partition layout unknown).
            # Spread this known file's MAC times between the record families.
            stamps = {"created": 1700000000, "accessed": 1700000060,
                      "modified": 1700000120, "metadata_changed": 1700000180}
            for field, key in (("crtime", "created"), ("atime", "accessed"),
                               ("mtime", "modified"), ("ctime", "metadata_changed")):
                run("debugfs", "-w", "-R", f"sif /small.txt {field} {stamps[key]}", image)
            # hardlink.txt names the same inode and therefore the same times.
            for path in ("/small.txt", "/hardlink.txt"):
                expected["files"][path]["timestamps"] = stamps
        expected["browser_records"] = [answer for answer in answers if answer["kind"] == "browser_visit"]
        (image.parent / "manifest.json").write_text(json.dumps(expected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        manifest["filesystems"][filesystem + ".img"] = expected
        source(filesystem + ".img", image.read_bytes(), answers, kind="raw_image")

    # Negative sources are part of the same imported case, with explicit gaps.
    unknown = directory / "unknown.sqlite"
    # Close explicitly: the context manager commits but leaves the handle open.
    db = sqlite3.connect(unknown)
    try:
        with db:
            db.execute("CREATE TABLE unfamiliar_schema(id INTEGER)")
    finally:
        db.close()
    source(unknown.name, unknown.read_bytes())
    source("damaged.evtx", evtx[:4100])  # Declared chunk is truncated.
    source("incomplete.img", partitioned_image()[:2048 * 512 + 512], kind="raw_image")
    manifest["diagnostics"] = {"unknown.sqlite": "unsupported_schema", "damaged.evtx": "evtx_unreadable",
                               "incomplete.img": "filesystem_unreadable", "ext4.img": "partition_layout_unknown"}
    manifest["record_count"] = sum(len(item["records"]) for item in manifest["sources"].values())
    manifest["timeline_anchors"] = ["2023-11-14T22:13:20+00:00", "2023-11-14T22:14:20+00:00",
                                     "2023-11-14T22:15:20.123456+00:00"]
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("demo-evidence"))
    parser.add_argument("--acceptance", action="store_true", help="Build the combined Stage 10 case (requires image tools)")
    args = parser.parse_args()
    manifest = make_acceptance(args.output) if args.acceptance else make_demo(args.output)
    print(f"Created synthetic sources and a known-answer manifest in {args.output.resolve()}")
