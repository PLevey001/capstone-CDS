import json

import pytest

from cds.records import RecordBudget, preserve_large_integers


@pytest.mark.parametrize("value,expected", [
    (2**53 - 1, 2**53 - 1),
    (-(2**53 - 1), -(2**53 - 1)),
    (2**53, "9007199254740992"),
    (-2**53, "-9007199254740992"),
    (2**53 + 1, "9007199254740993"),
    (-(2**53 + 1), "-9007199254740993"),
    (True, True),
    ("9007199254740993", "9007199254740993"),
])
def test_integer_precision_boundary_preserves_values_and_marks_only_conversions(value, expected):
    details = {"value": value}
    preserve_large_integers(details)
    saved = json.loads(json.dumps(details))
    assert saved["value"] == expected and type(saved["value"]) is type(expected)
    assert saved.get("integer_text_fields", []) == (["value"] if type(value) is int and isinstance(expected, str) else [])


def test_payload_budget_counts_escaped_text_and_rejection_does_not_consume_space():
    first = {"text": "東京"}
    second = {"text": "é"}
    # The budget conservatively reserves a comma and space even after the last record.
    limit = len(json.dumps([first, second], ensure_ascii=True).encode("utf-8")) + 2
    records = []
    budget = RecordBudget(limit)
    assert budget.append(records, first)
    assert not budget.append(records, {"text": "éx"})
    assert records == [first]
    assert budget.append(records, second)
    assert records == [first, second] and budget.used == limit
    assert not budget.append(records, {})
    assert records == [first, second] and budget.used == limit


def test_payload_budget_reserves_array_brackets():
    records = []
    assert not RecordBudget(2).append(records, {})
    assert records == []
