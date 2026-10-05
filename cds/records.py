"""Saved-record numeric bounds and byte accounting; no format-specific parsing."""

import json

EMPTY_RECORD_PAYLOAD_BYTES = 2  # JSON array brackets.


def preserve_large_integers(details):
    """Preserve top-level integers that browser JSON numbers would round."""
    for name, value in list(details.items()):
        if isinstance(value, int) and abs(value) > 2**53 - 1:
            details[name] = str(value)
            details.setdefault("integer_text_fields", []).append(name)


def record_size(record):
    # Callers start at two bytes for array brackets; allow a separator per record.
    # ASCII JSON matches subprocess output and keeps the budget independent of Unicode encoding.
    return len(json.dumps(record, ensure_ascii=True, allow_nan=False).encode("utf-8")) + 2


class RecordBudget:
    """Append to an initially empty record list only while its JSON payload fits."""

    def __init__(self, limit):
        self.limit = limit
        self.used = EMPTY_RECORD_PAYLOAD_BYTES

    def append(self, records, record):
        size = record_size(record)
        if self.used + size > self.limit:
            return False
        records.append(record)
        self.used += size
        return True
