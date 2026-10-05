"""Synthetic EVTX headers, chunks, and binary XML; no downloaded or personal logs.

Answers are source inputs, independent of python-evtx and the CDS parser.
Each record has a resident template with UTF-16 values and optional FILETIME substitution.
"""

import struct
import zlib
from xml.etree import ElementTree as ET

NAMESPACE = "http://schemas.microsoft.com/win/2004/08/events/event"
EVENTS = [
    {"record_id": "41", "provider": "Synthetic-Provider", "channel": "Application", "event_id": "1001",
     "level": "4", "computer": "LAB-ONE", "time": "2023-11-14T22:13:20.1234567Z",
     "event_time_us": 1700000000123456, "at": "2023-11-14T22:13:20.123456+00:00",
     "data": [("Label", "café 東京"), ("Repeat", "first"), ("Repeat", "second"), (None, "unnamed")]},
    {"record_id": "42", "provider": "Synthetic-Provider", "channel": "Application", "event_id": "1001",
     "level": "0", "computer": "LAB-ONE", "time": "2023-11-14T17:14:20.000001-05:00",
     "event_time_us": 1700000060000001, "at": "2023-11-14T22:14:20.000001+00:00", "data": []},
    {"record_id": "43", "provider": "Synthetic-Other", "channel": "System", "event_id": "7",
     "level": "2", "computer": "LAB-TWO", "time": "1970-01-01T00:00:00Z",
     "event_time_us": 0, "at": "1970-01-01T00:00:00+00:00", "data": [("Empty", "")]},
]


def event_element(event, namespace=True, prefix=""):
    def tag(name):
        return prefix + name
    root = ET.Element(tag("Event"), {"xmlns" + (":" + prefix[:-1] if prefix else ""): NAMESPACE} if namespace else {})
    system = ET.SubElement(root, tag("System"))
    if event.get("provider") is not None:
        ET.SubElement(system, tag("Provider"), {"Name": event["provider"], "Guid": "{11111111-2222-3333-4444-555555555555}"})
    for key, name in (("event_id", "EventID"), ("level", "Level"), ("record_id", "EventRecordID"),
                      ("channel", "Channel"), ("computer", "Computer")):
        if event.get(key) is not None:
            ET.SubElement(system, tag(name)).text = event[key]
    if event.get("time") is not None:
        attributes = {"SystemTime": event["time"]}
        time_tag = tag("TimeCreated")
        if "time_namespace" in event:
            time_tag = "time:TimeCreated"
            attributes["xmlns:time"] = event["time_namespace"]
        ET.SubElement(system, time_tag, attributes)
    if event.get("data") is not None:
        data = ET.SubElement(root, tag("EventData"))
        for name, value in event["data"]:
            ET.SubElement(data, tag("Data"), {"Name": name} if name is not None else {}).text = value
    if "user_data" in event:
        data = ET.SubElement(root, tag("UserData"))
        ET.SubElement(data, "Payload", {"xmlns": "urn:synthetic", "label": "custom"}).text = event["user_data"]
    return root


def binary_xml(root, offset, filetime=None):
    data = bytearray(b"\x0f\x01\x01\x00")

    def name(value):
        encoded = value.encode("utf-16le")
        hashed = 0
        for char in value:
            hashed = (hashed * 65599 + ord(char)) & 0xFFFF
        return struct.pack("<IHH", 0, hashed, len(encoded) // 2) + encoded + b"\0\0"

    def value(text):
        encoded = text.encode("utf-16le")
        return b"\x05\x01" + struct.pack("<H", len(encoded) // 2) + encoded

    def element(node):
        start = len(data)
        header_size = 15 if node.attrib else 11
        data.extend(struct.pack("<BHII", 0x41 if node.attrib else 1, 0xFFFF, 0, offset + start + header_size))
        if node.attrib:
            data.extend(bytes(4))
        data.extend(name(node.tag))
        attributes_start = len(data)
        for index, (key, text) in enumerate(node.attrib.items()):
            data.extend(struct.pack("<BI", 0x46 if index + 1 < len(node.attrib) else 6, offset + len(data) + 5))
            data.extend(name(key))
            data.extend(b"\x0d\x00\x00\x11" if key == "SystemTime" and filetime is not None else value(text))
        if node.attrib:
            struct.pack_into("<I", data, start + 11, len(data) - attributes_start)
        data.append(2)
        if node.text is not None:
            data.extend(value(node.text))
        for child in node:
            element(child)
        data.append(4)
        struct.pack_into("<I", data, start + 3, len(data) - start - 7)

    element(root)
    data.append(0)
    return bytes(data)


def chunk_checksums(data, offset=4096):
    end = struct.unpack_from("<I", data, offset + 48)[0]
    struct.pack_into("<I", data, offset + 52, zlib.crc32(data[offset + 512:offset + end]))
    struct.pack_into("<I", data, offset + 124, zlib.crc32(data[offset:offset + 120] + data[offset + 128:offset + 512]))


def file_checksum(data):
    struct.pack_into("<I", data, 124, zlib.crc32(data[:120]))


def make_evtx(events=None, *, namespace=True, prefix="", records_per_chunk=3):
    events = EVENTS if events is None else events
    manifest = {"events": events, "offsets": []}
    chunks = bytearray()
    for start in range(0, len(events), records_per_chunk):
        group = events[start:start + records_per_chunk]
        chunk = bytearray(512)
        last = 0
        for index, event in enumerate(group):
            last = len(chunk)
            manifest["offsets"].append(4096 + len(chunks) + last)
            template_offset = last + 24 + 4 + 10
            template = binary_xml(event_element(event, namespace, prefix), template_offset + 24, event.get("filetime"))
            root = b"\x0f\x01\x01\x00" + struct.pack("<BBII", 12, 1, 1, template_offset)
            root += struct.pack("<I16sI", 0, bytes(range(16)), len(template)) + template
            root += (struct.pack("<IHBBQ", 1, 8, 0x11, 0, event["filetime"]) if "filetime" in event else bytes(4))
            size = (24 + len(root) + 4 + 7) // 8 * 8
            number = int(event.get("record_id") or start + index + 1)
            # A deliberately different header time detects accidental fallback from TimeCreated.
            chunk.extend(struct.pack("<IIQQ", 0x2A2A, size, number, 132_000_000_000_000_000))
            chunk.extend(root + bytes(size - 28 - len(root)) + struct.pack("<I", size))
        end = len(chunk)
        assert end <= 65536
        chunk.extend(bytes(65536 - end))
        struct.pack_into("<8sQQQQIIII", chunk, 0, b"ElfChnk\0", start + 1, start + len(group),
                         int(group[0].get("record_id") or start + 1), int(group[-1].get("record_id") or start + len(group)),
                         128, last, end, 0)
        chunk_checksums(chunk, 0)
        chunks.extend(chunk)
    header = bytearray(4096)
    count = len(chunks) // 65536
    struct.pack_into("<8sQQQIHHHH", header, 0, b"ElfFile\0", 0, max(0, count - 1), len(events) + 1,
                     128, 1, 3, 4096, count)
    file_checksum(header)
    return bytes(header + chunks), manifest
