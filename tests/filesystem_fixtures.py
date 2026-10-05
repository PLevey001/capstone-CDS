"""Synthetic NTFS/ext4 images and source-side manifests; never read CDS output here."""

import argparse
import ctypes.util
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
from pathlib import Path

from scripts.make_browser_demo import make_chrome_history, make_firefox_history

TIMESTAMP = 1700000000  # 2023-11-14 22:13:20 UTC, also fixed in populate_ntfs.c.
VOLUME_BYTES = 64 * 1024**2
CHROME = "/Users/examiner/AppData/Local/Google/Chrome/User Data/Default/History"
FIREFOX = "/Users/examiner/AppData/Roaming/Mozilla/Firefox/Profiles/demo.default/places.sqlite"
TOOLS = ("mmls", "fls", "icat", "fsstat", "istat", "mkntfs", "mke2fs", "debugfs", "e2fsck", "cc")
EXT4_FEATURES = "has_journal,ext_attr,resize_inode,dir_index,filetype,extent,64bit,flex_bg,sparse_super,large_file,huge_file,dir_nlink,extra_isize,metadata_csum"


def missing_tools():
    missing = [tool for tool in TOOLS if not shutil.which(tool)]
    if not ctypes.util.find_library("ntfs-3g"):
        missing.append("libntfs-3g (shared library)")
    return missing


def run(*args, cwd=None):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, capture_output=True, timeout=60,
                            env={**os.environ, "TZ": "UTC", "LC_ALL": "C.UTF-8",
                                 "E2FSPROGS_FAKE_TIME": str(TIMESTAMP)})
    assert result.returncode == 0, (args, result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace"))
    return (result.stdout + result.stderr).decode("utf-8")


def make_sources(directory):
    """Define bytes and visit answers before a filesystem or parser sees them."""
    directory.mkdir()
    files = {
        "/small.txt": b"A resident NTFS file and an ordinary ext4 file.\n",
        "/large.bin": bytes(range(256)) * 1024,
        "/nested/报告-résumé.txt": "Synthetic Unicode evidence: café, 東京.\n".encode(),
        "/deleted.txt": b"Known deleted bytes; recovery depends on surviving metadata.\n",
    }
    make_chrome_history(directory / "History", count=1, optional=False)
    # Commit a second visit only to WAL, then copy while the connection remains
    # open. Closing normally would checkpoint away the scenario under test.
    with sqlite3.connect(directory / "History") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO visits(id,url,visit_time) VALUES(2,1,13344473660123456)")
        db.commit()
        for suffix in ("", "-wal", "-shm"):
            files[CHROME + suffix] = (directory / ("History" + suffix)).read_bytes()
    db.close()
    files[FIREFOX] = make_firefox_history(directory / "places.sqlite").read_bytes()
    return files


def make_filesystem_image(directory, filesystem, sector_size, partitioned, source_files):
    directory.mkdir()
    tree = directory / "source-tree"
    tree.mkdir()
    files = dict(source_files)
    if filesystem == "ext4":
        files["/sparse.bin"] = b"HEAD" + bytes(1024**2 - 8) + b"TAIL"
        files["/hardlink.txt"] = files["/small.txt"]
        files["/symlink.txt"] = b"small.txt"
        files["/deleted-retained.txt"] = b"debugfs unlink keeps these inode extents.\n"
    for path, content in files.items():
        source = tree / path.lstrip("/")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
    directories = sorted((path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_dir()),
                         key=lambda path: (path.count("/"), path))
    directories = ["/" + path for path in directories]
    offset = 1024**2 // sector_size if partitioned else 0
    timestamps = {name: TIMESTAMP for name in ("created", "modified", "accessed", "metadata_changed")}
    manifest = {
        "filesystem": filesystem, "sector_size": sector_size, "volume_bytes": VOLUME_BYTES,
        "partition_offset": offset, "partition_table": "DOS/MBR" if partitioned else None,
        "partitions": ([{"start_sector": offset, "length_sectors": VOLUME_BYTES // sector_size,
                         "type": 0x07 if filesystem == "ntfs" else 0x83}] if partitioned else []),
        "block_size": 4096, "directories": directories, "directory_timestamps": timestamps,
        "files": {path: {"content_file": "source-tree" + path, "size": len(content),
                         "sha256": hashlib.sha256(content).hexdigest(), "timestamps": dict(timestamps),
                         "deleted": path in ("/deleted.txt", "/deleted-retained.txt")}
                  for path, content in files.items()},
        "browser_records": [
            {"path": CHROME, "parser": "chrome-history/1", "source_key": "1",
             "at": "2023-11-14T22:13:20.123456+00:00", "url": "https://example.test/100%_done"},
            {"path": CHROME, "parser": "chrome-history/1", "source_key": "2",
             "at": "2023-11-14T22:14:20.123456+00:00", "url": "https://example.test/repeated"},
            *[{"path": FIREFOX, "parser": "firefox-history/1", "source_key": str(index + 1),
               "at": f"2023-11-14T22:{13 + index}:20.123456+00:00",
               "url": "https://firefox.example.test/" + ("100%_done" if index == 2 else "repeated")}
              for index in range(3)],
        ],
        "not_covered": ["ext2", "ext3", "NTFS alternate data streams", "compression", "encryption"],
        "reproducibility": "Source-side content manifest; volume IDs and SQLite WAL salts may vary.",
        "tool_versions": {tool: run(tool, flag).strip() for tool, flag in
                          (("fls", "-V"), ("mke2fs", "-V"), ("debugfs", "-V"), ("mkntfs", "-V"), ("cc", "--version"))},
        "sqlite_version": sqlite3.sqlite_version,
    }
    if filesystem == "ext4":
        manifest["features"] = EXT4_FEATURES.split(",")
        manifest["files"]["/sparse.bin"]["allocated_data_bytes"] = 8192
        manifest["files"]["/hardlink.txt"]["link_to"] = "/small.txt"
        manifest["files"]["/symlink.txt"]["symlink_target"] = "small.txt"
        # Explicitly model two deletion states. debugfs rm alone retains size
        # and extents; truncate-before-unlink models the cleared extents/size
        # left by ordinary ext4 deletion. Neither is a mounted-kernel test.
        manifest["files"]["/deleted.txt"]["inventory_size"] = 0
        manifest["files"]["/deleted.txt"]["recovered_hex"] = ""
        manifest["files"]["/deleted.txt"]["deletion"] = "truncate, zero size, unlink; original bytes unavailable through icat"
        manifest["files"]["/deleted-retained.txt"]["deletion"] = "debugfs rm only; retained size and extents"
    else:
        manifest["version"] = "3.1"
        manifest["files"]["/small.txt"]["data_storage"] = "Resident"
        manifest["files"]["/large.bin"]["data_storage"] = "Non-Resident"
        manifest["files"]["/deleted.txt"]["data_storage"] = "Resident"
        manifest["files"]["/deleted.txt"]["deletion"] = "ntfs_delete after final allocation; resident content retained"
    # Save expected answers before constructing the image. No fls, icat, CDS,
    # or other filesystem parser is used to manufacture these expectations.
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    volume = directory / "volume.img"
    with volume.open("xb") as image:
        image.truncate(VOLUME_BYTES)
    if filesystem == "ntfs":
        run("mkntfs", "-Q", "-F", "-T", "-L", "CDS_NTFS", "-s", sector_size, "-c", 4096,
            "-p", offset, "-H", 255, "-S", 63, volume)
        writer = directory / "populate_ntfs"
        run("cc", "-Wall", "-Wextra", "-Werror", "-Wl,--export-dynamic",
            Path(__file__).parent / "fixtures" / "populate_ntfs.c",
            "-l:" + ctypes.util.find_library("ntfs-3g"), "-o", writer)
        run(writer, volume, tree, *directories, *files)
    else:
        # Pin the feature set, block/inode geometry and device alignment rather
        # than inheriting the host's mke2fs.conf defaults. ext4 has no BPB sector
        # field; 4 KiB blocks and sector-sized partition LBAs support both devices.
        run("mke2fs", "-q", "-t", "ext4", "-F", "-L", "CDS_EXT4", "-b", 4096, "-I", 256,
            "-O", "none," + EXT4_FEATURES, "-E", "lazy_itable_init=0,lazy_journal_init=0",
            "-U", "12345678-1234-5678-9abc-123456789abc", volume)
        commands = [f"set_current_time {TIMESTAMP}"]
        commands += [f'mkdir "{path}"' for path in directories]
        for path in files:
            if path not in ("/hardlink.txt", "/symlink.txt"):
                commands.append(f'write "source-tree{path}" "{path}"')
        commands += ["ln /small.txt /hardlink.txt", "sif /small.txt links_count 2",
                     "symlink /symlink.txt small.txt"]
        for path in [*directories, *files]:
            for field in ("atime", "mtime", "ctime", "crtime"):
                commands.append(f'sif "{path}" {field} {TIMESTAMP}')
        commands += ["punch /deleted.txt 0 4294967295", "sif /deleted.txt size 0",
                     "rm /deleted.txt", "rm /deleted-retained.txt"]
        (directory / "debugfs.commands").write_text("\n".join(commands) + "\n", encoding="utf-8")
        run("debugfs", "-w", "-f", "debugfs.commands", "volume.img", cwd=directory)
        # debugfs can exit zero after individual command failures; fsck and the
        # manifest assertions must both pass before this fixture is evidence.
        run("e2fsck", "-f", "-n", volume)
    image = directory / "disk.img"
    if partitioned:
        # LBA values are in the actual logical sector size. Reserve a full
        # logical sector for MBR, keeping its signature at byte 510 as specified.
        header = bytearray(offset * sector_size)
        header[446:462] = struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff",
                                     manifest["partitions"][0]["type"], b"\xfe\xff\xff",
                                     offset, VOLUME_BYTES // sector_size)
        header[510:512] = b"\x55\xaa"
        with image.open("xb") as output, volume.open("rb") as source:
            output.write(header)
            shutil.copyfileobj(source, output)
        volume.unlink()
    else:
        volume.rename(image)
    return image, manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    missing = missing_tools()
    if missing:
        parser.error("Missing image fixture tools: " + ", ".join(missing))
    root = args.output.resolve()
    root.mkdir()  # Refuse to overwrite an existing fixture directory.
    sources = make_sources(root / "browser-sources")
    for filesystem in ("ntfs", "ext4"):
        for sector_size in (512, 4096):
            for partitioned in (False, True):
                name = f"{filesystem}-{sector_size}-{'mbr' if partitioned else 'bare'}"
                image, _ = make_filesystem_image(root / name, filesystem, sector_size, partitioned, sources)
                print(image)
