"""Bounded ordinary hive records. No transaction-log replay or artifact interpretation."""

import json
import struct

from Registry import RegistryParse

import cds.timestamps as timestamps

PARSER = "windows-registry/1"
SCOPE = ("Ordinary hive keys and values only. Transaction logs were not examined or replayed; "
         "deleted cells and interpreted Registry artifacts were not examined.")
# https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regqueryinfokeya
KEY_TIME_NOTE = ("Key last-write time describes the key or its value entries collectively; "
                 "it does not identify which entry changed or who changed it.")
VALUE_TIME_NOTE = ("Registry values have no individual timestamp. The containing key's last-write time "
                   "describes the key or its value entries collectively, not a change time for this value. "
                   "See key_source_key; this value is omitted from the timeline.")
VALUE_TYPES = {0: "REG_NONE", 1: "REG_SZ", 2: "REG_EXPAND_SZ", 3: "REG_BINARY", 4: "REG_DWORD",
               5: "REG_DWORD_BIG_ENDIAN", 6: "REG_LINK", 7: "REG_MULTI_SZ", 8: "REG_RESOURCE_LIST",
               9: "REG_FULL_RESOURCE_DESCRIPTOR", 10: "REG_RESOURCE_REQUIREMENTS_LIST", 11: "REG_QWORD"}


def hive_records(header, cell_data, text_limit):
    # Low-level records preserve unnamed values and do not swallow missing value lists.
    # Lazy iterators avoid materializing all children before applying the saved-record bound.
    root = header.first_key()
    pending = [(iter([root]), "", None, 1)]
    seen = set()
    while pending:
        children, parent_path, parent_offset, remaining = pending.pop()
        try:
            key = next(children)
        except StopIteration:
            if remaining:
                raise ValueError("Subkey count differs from the enumerated list.")
            continue
        if remaining <= 0 or key.offset() in seen:
            raise ValueError("Repeated key cell, cycle, or inconsistent subkey count.")
        seen.add(key.offset())
        pending.append((children, parent_path, parent_offset, remaining - 1))
        cell_data(key.offset(), 0x4C + key.unpack_word(0x48))
        if parent_offset is None:
            if not key.is_root():
                raise ValueError("Hive root does not have the root-key flag.")
        elif key.is_root() or key.unpack_dword(0x10) + 0x1004 != parent_offset:
            raise ValueError("Key parent does not match its subkey-list location.")
        path = parent_path + "\\" + key.name() if parent_path else key.name()
        source_key = f"key:{key.offset():x}"
        original = key.unpack_qword(4)
        # Preserve all FILETIME ticks; the timeline supports integer microseconds, not 100 ns.
        # Unlike Chrome visit times, this field has no documented zero-as-missing sentinel.
        when = original // 10 - 11_644_473_600_000_000
        try:
            timestamps.from_microseconds(when)
        except (OverflowError, ValueError):
            when = None
        details = {"key_path": path, "original_timestamp": str(original), "timestamp_unit": "100 nanoseconds",
                   "timestamp_epoch": "1601-01-01T00:00:00Z", "timestamp_note": KEY_TIME_NOTE,
                   "subkey_count": key.subkey_number(), "value_count": key.values_number()}
        if when is None:
            details["timestamp_note"] += " Unavailable or outside the supported UTC range; omitted from the timeline."
        yield {"artifact_key": "source", "kind": "registry_key", "source_key": source_key,
               "event_time_us": when, "summary": path, "parser": PARSER, "details": details}

        if key.values_number():
            cell_data(key.unpack_dword(0x28) + 0x1004, key.values_number() * 4)
            for value in key.values_list().values():
                if value.offset() in seen:
                    raise ValueError("Repeated value cell; ownership is ambiguous.")
                seen.add(value.offset())
                cell_data(value.offset(), 0x14 + value.unpack_word(2))
                length = value.data_length()
                value_type = value.unpack_dword(0xC)
                if value.raw_data_length() & 0x80000000:
                    if length > 4:
                        raise ValueError("Inline value exceeds its four-byte data field.")
                    raw = value.unpack_binary(8, length)
                elif not length:
                    raw = b""
                else:
                    offset = value.unpack_dword(8) + 0x1004
                    data = cell_data(offset, 0)
                    if length > 0x3FD8 and bytes(data[:2]) == b"db":
                        # Validate each segment before asking the dependency to join it: slices can hide short reads.
                        cell_data(offset, 8)
                        count, indirect = struct.unpack_from("<HI", data, 2)
                        if count != (length + 0x3FD7) // 0x3FD8:
                            raise ValueError("Segmented value count does not match its byte length.")
                        offsets = cell_data(indirect + 0x1004, count * 4)
                        left = length
                        for index in range(count):
                            size = min(left, 0x3FD8)
                            cell_data(struct.unpack_from("<I", offsets, index * 4)[0] + 0x1004, size)
                            left -= size
                        raw = value.raw_data()
                    else:
                        raw = bytes(cell_data(offset, length)[:length])
                if len(raw) != length:
                    raise ValueError("Value data is incomplete.")
                details = {"key_path": path, "key_source_key": source_key, "value_name": value.name(),
                           "is_default": not value.has_name(), "value_type": value_type,
                           "value_type_name": VALUE_TYPES.get(value_type, "Unknown"), "byte_length": length,
                           "data_truncated": False, "timestamp_note": VALUE_TIME_NOTE}
                if value_type in (1, 2, 6, 7):
                    decoded = raw.decode("utf-16le")
                    if value_type == 7:
                        # Bound the whole multi-string, including separators, before splitting it into entries.
                        decoded = decoded[:-2] if decoded.endswith("\0\0") else decoded
                        details["data_truncated"] = len(decoded) > text_limit
                        details["data"] = decoded[:text_limit].split("\0") if decoded else []
                    else:
                        decoded = decoded[:-1] if decoded.endswith("\0") else decoded
                        details["data_truncated"] = len(decoded) > text_limit
                        details["data"] = decoded[:text_limit]
                    details["data_encoding"] = "UTF-16LE"
                elif value_type in (4, 5, 11):
                    if length != (8 if value_type == 11 else 4):
                        raise ValueError("Integer value has an invalid byte length.")
                    details["data"] = int.from_bytes(raw, "big" if value_type == 5 else "little")
                else:
                    # Two hex characters represent one byte; use the existing text allowance for the preview.
                    details["data"] = raw[:text_limit // 2].hex()
                    details["data_encoding"] = "hex"
                    details["data_truncated"] = length > text_limit // 2
                yield {"artifact_key": "source", "kind": "registry_value", "source_key": f"value:{value.offset():x}",
                       "event_time_us": None, "summary": path + " : " + (value.name() if value.has_name() else "[unnamed value]"),
                       "parser": PARSER, "details": details}

        if key.subkey_number():
            # Check every index cell, including indirect lists, before traversing it. Corrupt list counts
            # must not read through adjacent cells or turn into a silently shorter enumeration.
            lists = [key.unpack_dword(0x1C) + 0x1004]
            visited_lists = set()
            while lists:
                offset = lists.pop()
                if offset in visited_lists:
                    raise ValueError("Repeated subkey index cell or index cycle.")
                visited_lists.add(offset)
                data = cell_data(offset, 4)
                kind, count = struct.unpack_from("<2sH", data)
                if kind not in (b"li", b"lf", b"lh", b"ri"):
                    raise ValueError("Unsupported subkey index structure.")
                width = 8 if kind in (b"lf", b"lh") else 4
                cell_data(offset, 4 + count * width)
                if kind == b"ri":
                    lists.extend(struct.unpack_from("<I", data, 4 + index * 4)[0] + 0x1004 for index in range(count))
            pending.append((key.subkey_list().keys(), path, key.offset(), key.subkey_number()))


def read_hive(path, limits):
    result = {"id": "windows-registry", "label": "Windows Registry", "parser": PARSER,
              "status": "complete", "reason": "hive_read", "detail": SCOPE,
              "processed": 0, "total": None, "records": []}
    try:
        with path.open("rb") as source:
            data = source.read(limits["content_file_bytes"] + 1)
        if len(data) > limits["content_file_bytes"]:
            result.update(status="skipped", reason="content_file_limit", detail="Hive exceeds the content byte limit. " + SCOPE)
            return result
        if not data.startswith(b"regf"):
            result.update(status="unsupported", reason="not_registry_hive", detail="No regf hive signature. " + SCOPE)
            return result
        if len(data) < 4096:
            raise ValueError("Hive base block is truncated.")
        header = RegistryParse.REGFBlock(data, 0, None)
        recovery = header.recovery_required()
        if recovery.recover_header or recovery.recover_data:
            result.update(status="failed", reason="hive_recovery_required",
                          detail=("Hive header checksum is invalid; header/data recovery is required. " if recovery.recover_header
                                  else "Hive sequence numbers differ; this dirty hive requires transaction-log replay. ")
                                 + "No records were saved; Registry coverage has a gap. " + SCOPE)
            return result
        # Other layouts need separate validation; recognizing regf alone does not establish support.
        if (not header.is_primary_file() or header.major_version() != 1 or header.minor_version() not in (3, 4, 5, 6)
                or header.file_format() != 1 or header.clustering_factor() != 1):
            result.update(status="unsupported", reason="unsupported_hive_format",
                          detail="Unsupported hive version, layout, or transaction-log input; no recovery attempted. " + SCOPE)
            return result
        end = 4096 + header.hbins_size()
        if not header.hbins_size() or header.hbins_size() % 4096 or end > len(data):
            raise ValueError("Hive bin extent is invalid or incomplete.")
        cells = {}
        position = 4096
        while position < end:
            signature, relative, size = struct.unpack_from("<4sII", data, position)
            if signature != b"hbin" or relative != position - 4096 or not size or size % 4096 or position + size > end:
                raise ValueError("Hive bin header or extent is invalid.")
            cell = position + 32
            while cell < position + size:
                allocated_size = struct.unpack_from("<i", data, cell)[0]
                length = abs(allocated_size)
                if length < 8 or length % 8 or cell + length > position + size:
                    raise ValueError("Hive cell extent is invalid or incomplete.")
                if allocated_size < 0:
                    cells[cell + 4] = length - 4
                cell += length
            position += size

        def cell_data(offset, needed):
            length = cells.get(offset)
            if length is None or needed > length:
                raise ValueError("Referenced hive cell is missing, free, or shorter than its declared data.")
            return memoryview(data)[offset:offset + length]

        payload_bytes = 2  # Same array and separator accounting as browser records and Store.finish.
        shortened = missing_times = 0
        for record in hive_records(header, cell_data, limits["record_text_chars"]):
            if len(result["records"]) >= limits["parsed_records"]:
                result.update(status="partial", reason="record_limit")
                break
            details = record["details"]
            truncated = []
            for name, value in list(details.items()):
                if isinstance(value, int) and abs(value) > 2**53 - 1:
                    details[name] = str(value)
                    details.setdefault("integer_text_fields", []).append(name)
                # Data is already bounded above. Keep fixed provenance, links, and timestamp qualifications intact.
                if name in ("key_path", "value_name") and len(value) > limits["record_text_chars"]:
                    details[name] = value[:limits["record_text_chars"]]
                    truncated.append(name)
            if len(record["summary"]) > limits["record_text_chars"]:
                truncated.append("summary")
            if truncated:
                details["truncated_fields"] = truncated
            record["summary"] = record["summary"][:limits["record_text_chars"]]
            size = len(json.dumps(record, ensure_ascii=True, allow_nan=False).encode("utf-8")) + 2
            if payload_bytes + size > limits["record_payload_bytes"]:
                result.update(status="partial", reason="record_payload_limit")
                break
            result["records"].append(record)
            payload_bytes += size
            shortened += bool(truncated or details.get("data_truncated"))
            missing_times += record["kind"] == "registry_key" and record["event_time_us"] is None
        else:
            result["total"] = len(result["records"])
        result["processed"] = len(result["records"])
        issues = []
        if result["reason"] in ("record_limit", "record_payload_limit"):
            issues.append("Enumeration stopped at the saved-record count or payload byte limit; remaining records were not examined.")
        if shortened:
            issues.append(f"{shortened} records contain shortened fields or data previews.")
        if missing_times:
            issues.append(f"{missing_times} keys have no usable last-write time.")
        if issues and result["status"] == "complete":
            result.update(status="partial", reason="record_quality")
        result["detail"] = (f"Saved {result['processed']} key/value records in hive traversal order. "
                            + " ".join(issues) + " " + SCOPE)
    except (OSError, ValueError, struct.error, RegistryParse.RegistryException, RecursionError) as error:
        # Do not present a prefix from a damaged hive as a complete or empty hive.
        result.update(status="failed", reason="hive_unreadable", records=[], processed=0, total=None,
                      detail=f"Hive is damaged or uses an unsupported structure: {str(error)[:300]}. "
                             "No records were saved; Registry coverage has a gap. " + SCOPE)
    return result
