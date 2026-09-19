"""Create small, synthetic fixtures with known contents; no external tools needed."""
import argparse
import hashlib
import json
import struct
from pathlib import Path

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("demo-evidence"))
    args = parser.parse_args()
    manifest = make_demo(args.output)
    print(f"Created {len(manifest)} synthetic sources and a SHA-256 manifest in {args.output.resolve()}")
