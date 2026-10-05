"""Bounded EVTX records. Provider message resources and activity rules are not used."""

import re
import struct
import time
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from Evtx import Evtx
from Evtx import Nodes
from Evtx.BinaryParser import ParseException
from Evtx.Views import UnexpectedElementException

import cds.timestamps as timestamps
from cds.records import RecordBudget

PARSER = "windows-event-log/1"
SCOPE = ("EVTX records from declared chunks only; inactive chunks and deleted records were not examined. "
         "Provider resources were not supplied; event messages were not rendered. "
         "Event records do not identify a person or establish their actions.")
TIME_NOTE = ("Event TimeCreated/SystemTime, normalised to UTC microseconds; original text is retained. "
             "Sub-microsecond digits are truncated, not rounded. No record-header or file-time fallback.")
NAMESPACE = "http://schemas.microsoft.com/win/2004/08/events/event"


def event_record(xml, number, offset, text_limit, *, filetime=None):
    # DTD/entity declarations are not event fields. Reject them before any expansion or external resolution.
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ValueError("XML declarations for DTDs or entities are unsupported.")
    root = ET.fromstring(xml)
    namespace = "{" + NAMESPACE + "}" if root.tag == "{" + NAMESPACE + "}Event" else ""
    if root.tag != namespace + "Event":
        raise ValueError("XML root is not a Windows Event element.")
    system = root.find(namespace + "System")
    if system is None:
        raise ValueError("Event has no System element.")
    if len(root.findall(namespace + "System")) != 1 or len(system.findall(namespace + "TimeCreated")) > 1:
        raise ValueError("Event has ambiguous System or TimeCreated elements.")
    details = {"record_number": str(number), "record_offset": offset}
    for name, tag in (("event_record_id", "EventRecordID"), ("channel", "Channel"), ("event_id", "EventID"),
                      ("level", "Level"), ("computer", "Computer")):
        element = system.find(namespace + tag)
        details[name] = None if element is None else element.text or ""
    provider = system.find(namespace + "Provider")
    details["provider"] = None if provider is None else provider.get("Name")
    details["provider_guid"] = None if provider is None else provider.get("Guid")
    created = system.find(namespace + "TimeCreated")
    original = None if created is None else created.get("SystemTime")
    details.update(original_timestamp=original, timestamp_note=TIME_NOTE)
    when = None
    # XML dateTime needs an explicit zone here: assuming the examiner's local zone would invent a time.
    # The dependency emits a space separator for typed times; retain that text while accepting it here.
    # https://learn.microsoft.com/en-us/windows/win32/wes/eventschema-timecreated-systempropertiestype-element
    if filetime is not None:
        # The dependency renders FILETIME via a float (and maps zero to year 1). Use the actual
        # TimeCreated substitution, never the record header, to preserve all ticks and avoid rounding.
        details["original_filetime"] = str(filetime)
        details["timestamp_note"] = ("Event TimeCreated/SystemTime binary FILETIME, in 100 ns ticks since 1601-01-01 UTC. "
                                     "original_filetime retains exact ticks; original_timestamp is the dependency's XML text. "
                                     "UTC microseconds truncate sub-microsecond ticks. No record-header or file-time fallback.")
        try:
            when = filetime // 10 - 11_644_473_600_000_000
            timestamps.from_microseconds(when)
        except (ValueError, OverflowError):
            when = None
    elif original is not None and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
                                           r"(?:Z|[+-](?:0[0-9]|1[0-3]):[0-5][0-9]|[+-]14:00)", original):
        try:
            instant = datetime.fromisoformat(original).astimezone(timezone.utc)
            delta = instant - timestamps.UNIX_EPOCH
            when = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
        except (ValueError, OverflowError):
            pass
    if when is None:
        details["timestamp_note"] += " Missing or invalid creation time; omitted from the timeline."

    truncated = []
    for name, value in list(details.items()):
        # Fixed provenance and timestamp qualifications must remain intact when user fields are shortened.
        if name not in ("record_number", "original_filetime", "timestamp_note") and isinstance(value, str) and len(value) > text_limit:
            details[name] = value[:text_limit]
            truncated.append(name)
    for name, tag in (("event_data", "EventData"), ("user_data", "UserData")):
        container = root.find(namespace + tag)
        details[name] = None if container is None else []
        pending = [] if container is None else [(container, None)]
        remaining = text_limit
        while pending:
            if remaining <= 0:
                truncated.append(name)
                break
            element, parent = pending.pop()
            node = {"tag": element.tag, "attributes": list(element.attrib.items()),
                    "text": element.text, "tail": element.tail, "parent": parent}
            # Bound the entire structured field, including names and empty nodes. Lists preserve repeated
            # Data names; parent indexes preserve nested UserData without recursive saved JSON.
            remaining -= 1
            for key in ("tag", "text", "tail"):
                value = node[key]
                if value is not None:
                    node[key] = value[:remaining]
                    if len(value) > remaining:
                        truncated.append(name)
                    remaining -= len(node[key])
            attributes = []
            for key, value in node["attributes"]:
                if len(key) + len(value) + 1 > remaining:
                    truncated.append(name)
                    break
                attributes.append([key, value])
                remaining -= len(key) + len(value) + 1
            node["attributes"] = attributes
            index = len(details[name])
            details[name].append(node)
            pending.extend((child, index) for child in reversed(element))
    details["raw_xml"] = xml[:text_limit]
    if len(xml) > text_limit:
        truncated.append("raw_xml")
    summary = "Event " + (details["event_id"] if details["event_id"] is not None else "[ID missing]")
    if details["provider"] is not None:
        summary += " · " + details["provider"]
    if len(summary) > text_limit:
        truncated.append("summary")
    if truncated:
        details["truncated_fields"] = sorted(set(truncated))
    # Event IDs recur, and damaged logs can repeat record numbers. The physical offset distinguishes
    # both; the existing artifact/run foreign key supplies the source-file part of the identity.
    return {"artifact_key": "source", "kind": "windows_event", "source_key": f"record:{number}@{offset:x}",
            "event_time_us": when, "summary": summary[:text_limit], "parser": PARSER, "details": details}


def read_evtx(path, limits):
    result = {"id": "windows-event-log", "label": "Windows event log", "parser": PARSER,
              "status": "complete", "reason": "evtx_read", "detail": SCOPE,
              "processed": 0, "total": None, "records": []}
    counts = result["counts"] = {"examined_records": 0, "malformed_records": 0, "corrupt_chunks": 0,
                                 "shortened_records": 0, "missing_times": 0}
    issues = []
    budget = RecordBudget(limits["record_payload_bytes"])
    deadline = time.monotonic() + limits["content_timeout"]
    try:
        with path.open("rb") as source:
            data = source.read(limits["content_file_bytes"] + 1)
        if len(data) > limits["content_file_bytes"]:
            result.update(status="skipped", reason="content_file_limit", detail="EVTX exceeds the content byte limit. " + SCOPE)
            return result
        if not data.startswith(b"ElfFile\0"):
            result.update(status="unsupported", reason="not_evtx", detail="No EVTX file signature. " + SCOPE)
            return result
        if len(data) < 4096:
            raise ValueError("EVTX file header is truncated.")
        header = Evtx.FileHeader(data, 0)
        if (header.major_version(), header.minor_version(), header.header_size(), header.header_chunk_size()) != (3, 1, 128, 4096):
            result.update(status="unsupported", reason="unsupported_evtx_format", detail="Unsupported EVTX version or layout. " + SCOPE)
            return result
        if not header.verify():
            raise ValueError("EVTX file-header checksum is invalid.")
        if header.is_dirty():
            issues.append("File is marked dirty; declared chunks may not include all changes. No recovery attempted.")
        # The format's count is 32 bits; the dependency accessor reads only 16, which could hide missing chunks.
        for chunk_index in range(header.unpack_dword(42)):
            if time.monotonic() >= deadline:
                result.update(status="partial", reason="content_timeout")
                break
            base = 4096 + chunk_index * 65536
            chunk_data = data[base:base + 65536]
            if len(chunk_data) != 65536:
                counts["corrupt_chunks"] += 1
                raise ValueError(f"Declared chunk at {base:#x} is truncated or missing.")
            # Isolate chunks so corrupt binary-XML offsets cannot read another chunk's bytes.
            chunk = Evtx.ChunkHeader(chunk_data, 0)
            end = chunk.next_record_offset()
            if not chunk.check_magic() or chunk.header_size() != 128 or not 512 <= end <= 65536 or not chunk.verify():
                counts["corrupt_chunks"] += 1
                # Fixed-size chunks have independent headers/tables; later chunks remain readable.
                # Five diagnostic samples keep coverage small; the failure counts remain exhaustive.
                if len(issues) < 5:
                    issues.append(f"Chunk at {base:#x} skipped: invalid header, extent, or checksum; its record count is unknown.")
                continue
            position, last, examined = 512, 0, 0
            while position < end:
                if time.monotonic() >= deadline:
                    result.update(status="partial", reason="content_timeout")
                    break
                if len(result["records"]) >= limits["parsed_records"]:
                    result.update(status="partial", reason="record_limit")
                    break
                counts["examined_records"] += 1
                if position + 28 > end:
                    counts["malformed_records"] += 1
                    raise ValueError(f"Truncated record header at {base + position:#x}; cannot locate the next record safely.")
                magic, size, number = struct.unpack_from("<IIQ", chunk_data, position)
                if (size < 28 or size % 8 or position + size > end
                        or struct.unpack_from("<I", chunk_data, position + size - 4)[0] != size):
                    counts["malformed_records"] += 1
                    raise ValueError(f"Invalid record extent at {base + position:#x}; cannot locate the next record safely.")
                try:
                    if magic != 0x2A2A:
                        raise ValueError("Invalid record signature.")
                    record = Evtx.Record(chunk_data, position, chunk)
                    root = record.root()
                    if root.length() > size - 28:
                        raise ValueError("Binary XML extends beyond its record.")
                    # Only the exact Event/System/TimeCreated attribute can supply binary time.
                    # Other FILETIME fields (including the record header) describe different things.
                    filetime = None
                    pending = [(node, (), {}) for node in root.template().children()]
                    while pending:
                        node, parents, inherited = pending.pop()
                        if not isinstance(node, Nodes.OpenStartElementNode):
                            continue
                        namespaces = dict(inherited)
                        attributes = {}
                        for child in node.children():
                            if not isinstance(child, Nodes.AttributeNode):
                                continue
                            name = child.attribute_name().string()
                            if name != "SystemTime" and name != "xmlns" and not name.startswith("xmlns:"):
                                continue
                            value = child.attribute_value()
                            if isinstance(value, Nodes.ValueNode):
                                value = value.value()
                            elif isinstance(value, (Nodes.NormalSubstitutionNode, Nodes.ConditionalSubstitutionNode)):
                                value = root.substitutions()[value.index()]
                            else:
                                raise ValueError("Unsupported namespace or creation-time attribute encoding.")
                            attributes[name] = value
                            if name == "xmlns" or name.startswith("xmlns:"):
                                namespaces[name[6:] if name.startswith("xmlns:") else ""] = value.string()
                        prefix, _, name = node.tag_name().rpartition(":")
                        namespace = namespaces.get(prefix, "")
                        names = (*parents, "{" + namespace + "}" + name if namespace else name)
                        # Resolve inherited and locally rebound prefixes just as the XML reader does.
                        if names in (("Event", "System", "TimeCreated"),
                                     tuple("{" + NAMESPACE + "}" + tag for tag in ("Event", "System", "TimeCreated"))):
                            value = attributes.get("SystemTime")
                            if isinstance(value, Nodes.FiletimeTypeNode):
                                filetime = value.unpack_qword(0)
                        # TimeCreated is a direct System child; deeper payload elements cannot supply it.
                        if len(names) < 3:
                            pending.extend((child, names, namespaces) for child in node.children())
                    row = event_record(record.xml(), number, base + position, limits["record_text_chars"], filetime=filetime)
                except (ValueError, ParseException, ET.ParseError, UnexpectedElementException, KeyError,
                        IndexError, struct.error, UnicodeError) as error:
                    counts["malformed_records"] += 1
                    # Bound error samples and their text so corrupt input cannot fill the output allowance.
                    if len(issues) < 5:
                        issues.append(f"Record at {base + position:#x} skipped: {str(error)[:200]}.")
                else:
                    if not budget.append(result["records"], row):
                        result.update(status="partial", reason="record_payload_limit")
                        break
                    counts["shortened_records"] += bool(row["details"].get("truncated_fields"))
                    counts["missing_times"] += row["event_time_us"] is None
                last = position
                position += size
                examined += 1
                # Archived logs may zero-fill between the last record and the free-space offset.
                # Only accept padding after the declared last record; the count check still applies.
                if last == chunk.last_record_offset() and not any(chunk_data[position:end]):
                    position = end
            if result["status"] == "partial":
                break
            if last != chunk.last_record_offset() or examined != chunk.file_last_record_number() - chunk.file_first_record_number() + 1:
                counts["corrupt_chunks"] += 1
                raise ValueError(f"Chunk at {base:#x} has inconsistent record counts or last-record offset.")
        else:
            if not counts["corrupt_chunks"] and not header.is_dirty():
                result["total"] = counts["examined_records"]
    except Exception as error:
        # An unexpected decoder failure is terminal for this log. Completed records and other source
        # artifacts remain usable; never replace a partial prefix with a clean empty result.
        result.update(status="partial" if result["records"] else "failed", reason="evtx_unreadable")
        # Retain a bounded stop reason even when the five recoverable-error samples are already full.
        issues.append(f"Parser stopped: {type(error).__name__}: {str(error)[:300]}. Remaining records were not examined.")
    result["processed"] = len(result["records"])
    if result["reason"] in ("record_limit", "record_payload_limit", "content_timeout"):
        issues.append("Enumeration stopped at the record, payload, or time limit; remaining records were not examined.")
    for count, description in ((counts["malformed_records"], "malformed records"),
                               (counts["corrupt_chunks"], "corrupt chunks with unknown record counts"),
                               (counts["shortened_records"], "records contain shortened fields"),
                               (counts["missing_times"], "records have no usable creation time")):
        if count:
            issues.append(f"{count} {description}.")
    if issues and result["status"] == "complete":
        result.update(status="partial", reason="evtx_corruption" if counts["corrupt_chunks"] or counts["malformed_records"] else "record_quality")
    result["detail"] = (f"Saved {result['processed']} events in physical record order; examined {counts['examined_records']} records. "
                        + " ".join(issues) + " " + SCOPE)
    return result
