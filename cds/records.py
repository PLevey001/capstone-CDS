"""Saved-record byte accounting shared by readers, source linking, and storage."""

import json


def record_size(record):
    # Callers start at two bytes for array brackets; allow a separator per record.
    # ASCII JSON matches subprocess output and keeps the budget independent of Unicode encoding.
    return len(json.dumps(record, ensure_ascii=True, allow_nan=False).encode("utf-8")) + 2
