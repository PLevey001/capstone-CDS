"""Synthetic regression cases; citation checks do not verify meaning."""
import json

import pytest

from cds import case_brief
from cds.local_model import ModelError


FACT = {"id": "F1", "kind": "inventory", "text": "The inventory lists 8 file entries and 1 directory.",
        "source_id": "s1", "run_id": "r1", "artifact_id": None}


@pytest.mark.parametrize("bad", [
    {"statement": "Mixed references.", "facts": ["F1", "F99"]},
    {"statement": "Unknown reference.", "facts": ["F99"]},
    {"statement": "Wrong type.", "facts": ["F1", 1]},
    {"statement": "There are 9 file entries.", "facts": ["F1"]},
    {"statement": "x" * 401, "facts": ["F1"]},
    {"statement": "", "facts": ["F1"]},
    "malformed",
])
def test_invalid_statements_are_withheld_whole(bad):
    good = {"statement": "The inventory lists file entries.", "facts": ["F1"]}
    overview, _, withheld = case_brief.check_statements(json.dumps({"overview": [bad, good], "review": []}), [FACT])
    assert [item["text"] for item in overview] == [good["statement"]]
    assert withheld == 1


def test_all_overflow_is_counted_and_meaning_is_explicitly_unverified():
    item = {"statement": "The suspect deliberately destroyed evidence.", "facts": ["F1"]}
    overview, _, withheld = case_brief.check_statements(json.dumps({"overview": [item] * 12, "review": []}), [FACT])
    assert len(overview) == 5 and withheld == 7
    assert all(statement["verification"] == "unverified" for statement in overview)


@pytest.mark.parametrize("key,value", [("source_id", "s2"), ("run_id", "r2"), ("artifact_id", 12)])
def test_digest_includes_provenance(key, value):
    assert case_brief.digest([FACT]) != case_brief.digest([FACT | {key: value}])
    assert case_brief.digest([FACT]) == case_brief.digest([dict(reversed(list(FACT.items())))])


def test_unexpected_top_level_output_is_rejected():
    with pytest.raises(ModelError) as error:
        case_brief.check_statements(json.dumps({"overview": [], "review": [], "verdict": "Invented verdict"}), [FACT])
    assert error.value.status == 502
