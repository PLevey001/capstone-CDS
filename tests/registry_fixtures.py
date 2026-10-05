"""Synthetic hive bytes and source-side answers; no parser output or personal hives.

The builder writes REGF/HBIN, NK/VK, index, and data cells directly. The manifest
is defined from the inputs before serialization, independently of python-registry.
"""

import struct


KEY_PATH = "ROOT\\Software\\研究"
FILETIME = 133_444_736_001_234_567
VALUES = [
    ("", 1, "Unnamed café", "Unnamed café\0".encode("utf-16le")),
    ("(Default)", 1, "Literal name", "Literal name\0".encode("utf-16le")),
    ("展開", 2, "%USERPROFILE%\\資料", "%USERPROFILE%\\資料\0".encode("utf-16le")),
    ("Multi", 7, ["alpha", "東京", "omega"], "alpha\0東京\0omega\0\0".encode("utf-16le")),
    ("DWord", 4, 4_000_000_001, struct.pack("<I", 4_000_000_001)),
    ("QWord", 11, 9_223_372_036_854_775_825, struct.pack("<Q", 9_223_372_036_854_775_825)),
    ("Binary", 3, "00017fff", b"\x00\x01\x7f\xff"),
    ("Empty", 3, "", b""),
    ("Zero", 4, 0, bytes(4)),
]


def checksum(data):
    value = 0
    for offset in range(0, 508, 4):
        value ^= struct.unpack_from("<I", data, offset)[0]
    struct.pack_into("<I", data, 508, 1 if value == 0 else 0xFFFFFFFE if value == 0xFFFFFFFF else value)


def make_hive(name="NTUSER.DAT", values=None):
    values = VALUES if values is None else values
    manifest = {
        "hive": name, "keys": ["ROOT", "ROOT\\Software", KEY_PATH],
        "at": "2023-11-14T22:13:20.123456+00:00", "event_time_us": 1700000000123456,
        "original_timestamp": str(FILETIME),
        "values": [{"name": name, "type": kind, "data": value, "byte_length": len(raw)}
                   for name, kind, value, raw in values],
        "cells": {},
    }
    cells = bytearray(32)

    def cell(data):
        offset = len(cells)
        size = (len(data) + 11) // 8 * 8
        cells.extend(struct.pack("<i", -size) + data + bytes(size - 4 - len(data)))
        return offset

    def key(name, parent=None):
        compressed = name.isascii()
        encoded = name.encode("ascii" if compressed else "utf-16le")
        data = bytearray(76) + encoded
        data[:2] = b"nk"
        struct.pack_into("<HQ", data, 2, (0x20 if compressed else 0) | (4 if parent is None else 0), FILETIME)
        struct.pack_into("<I", data, 16, 0xFFFFFFFF if parent is None else parent)
        struct.pack_into("<IIII", data, 28, 0xFFFFFFFF, 0xFFFFFFFF, 0, 0xFFFFFFFF)
        struct.pack_into("<I", data, 48, 0xFFFFFFFF)
        struct.pack_into("<H", data, 72, len(encoded))
        offset = cell(data)
        manifest["cells"][name] = 4096 + offset + 4
        return offset

    root = key("ROOT")
    software = key("Software", root)
    leaf = key("研究", software)
    for parent, child in ((root, software), (software, leaf)):
        index = cell(struct.pack("<2sHI", b"li", 1, child))
        struct.pack_into("<I", cells, parent + 4 + 20, 1)
        struct.pack_into("<I", cells, parent + 4 + 28, index)
    # A self-relative, empty security descriptor makes these ordinary synthetic key cells.
    security = cell(struct.pack("<2sHIIIII", b"sk", 0, 0, 0, 3, 20, 0x80000001) + bytes(16))
    struct.pack_into("<II", cells, security + 8, security, security)
    for offset in (root, software, leaf):
        struct.pack_into("<I", cells, offset + 4 + 44, security)
    offsets = []
    for value_name, kind, _, raw in values:
        encoded = value_name.encode("utf-16le")
        data = bytearray(20) + encoded
        data[:2] = b"vk"
        struct.pack_into("<HI", data, 2, len(encoded), len(raw) | (0x80000000 if len(raw) <= 4 else 0))
        if len(raw) <= 4:
            data[8:8 + len(raw)] = raw
        else:
            if len(raw) > 0x3FD8:
                segments = [cell(raw[start:start + 0x3FD8]) for start in range(0, len(raw), 0x3FD8)]
                indirect = cell(struct.pack("<" + "I" * len(segments), *segments))
                location = cell(struct.pack("<2sHI", b"db", len(segments), indirect))
            else:
                location = cell(raw)
            struct.pack_into("<I", data, 8, location)
        struct.pack_into("<I", data, 12, kind)
        offset = cell(data)
        offsets.append(offset)
        manifest["cells"]["value:" + value_name] = 4096 + offset + 4
    if offsets:
        index = cell(struct.pack("<" + "I" * len(offsets), *offsets))
        struct.pack_into("<II", cells, leaf + 4 + 36, len(offsets), index)
    size = (len(cells) + 8 + 4095) // 4096 * 4096
    cells.extend(struct.pack("<i", size - len(cells)) + bytes(size - len(cells) - 4))
    struct.pack_into("<4sII", cells, 0, b"hbin", 0, size)
    header = bytearray(4096)
    struct.pack_into("<4sIIQIIIIIII", header, 0, b"regf", 1, 1, FILETIME, 1, 5, 0, 1, root, size, 1)
    encoded = name.encode("utf-16le")
    header[48:48 + len(encoded)] = encoded
    checksum(header)
    return bytes(header + cells), manifest
