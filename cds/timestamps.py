"""Exact UTC conversion for parsed records, including pre-1970 dates."""

from datetime import datetime, timedelta, timezone

UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def from_microseconds(value):
    return (UNIX_EPOCH + timedelta(microseconds=value)).isoformat()
