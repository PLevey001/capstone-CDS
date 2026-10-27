"""Authored examples satisfy actual validators; no model semantic success is measured."""
import json
from pathlib import Path
import re

import pytest

from cds import case_brief, learning, local_model
from cds.config import Settings

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "docs" / "ai-guidance"
EXAMPLES = json.loads((PACK / "examples.json").read_text(encoding="utf-8"))["examples"]


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example["id"])
def test_preferred_authored_outputs_pass_actual_validators(example, tmp_path, monkeypatch):
    output = example["preferred_output"]
    if example["mode"] == "brief":
        checked = case_brief.check_assessment(json.dumps(output), example["facts"])
        assert sum(map(len, checked.values())) == sum(map(len, output.values()))
        return
    # Supply the authored reply at the transport boundary, leaving runtime chat validation intact.
    monkeypatch.setattr(local_model, "chat", lambda *a, **kw: (json.dumps(output), {"context": {}}))
    facts = [{**fact, "kind": "source", "source_id": fact["id"]}
             for fact in example["facts"] if fact["id"].startswith("F")]
    prior = None
    a_refs = [ref for ref in example["facts"] if ref["id"].startswith("A-")]
    if a_refs:
        prior = example.get("prior_assessment") or {
            **{category: [] for category in case_brief.SECTION_LIMITS},
            "inferences": [{**ref, "facts": [facts[0]["id"]]} for ref in a_refs]}
        prior = {**prior, "id": "authored-example", "version": 1, "context": {}}
    if example["mode"] == "concepts":
        monkeypatch.setattr(learning, "GLOSSARY", example["facts"])
    result = learning.generate(Settings(tmp_path, ai_context=32768),
        learning.ChatInput(mode=example["mode"], message=example["question"]),
        {"facts": facts, "facts_digest": case_brief.digest(facts), "provenance": {}}, prior)
    assert result["withheld"] == 0
    assert [item["text"] for item in result["answer"]] == [item["statement"] for item in output["answer"]]
    assert bool(result["uncertainty"]) == output["cannot_answer"]


def test_example_coverage_and_rejections_are_explicitly_authored():
    assert {ex["id"] for ex in EXAMPLES} >= {
        "brief-sparse-registry", "chat-mixed-answerable-unknown", "chat-prior-wrong-registry",
        "chat-hostile-label", "brief-hostile-evidence"}
    assert all(ex["rejected_wording"] and all(item["reason"] for item in ex["rejected_wording"]) for ex in EXAMPLES)
    # A semantically bad answer can pass software checks. Never label these rejections automated detection.
    wrong = next(ex for ex in EXAMPLES if ex["id"] == "brief-sparse-registry")
    output = {**wrong["preferred_output"], "overall_summary": [
        {"statement": wrong["rejected_wording"][0]["text"], "facts": ["F1"]}]}
    assert case_brief.check_assessment(json.dumps(output), wrong["facts"])["overall_summary"]


def test_guidance_document_links_and_runtime_index_are_present():
    for path in [ROOT / "README.md", ROOT / "docs/local-ai.md", *PACK.glob("*.md")]:
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if "://" not in target and not target.startswith("#"):
                assert (path.parent / target.split("#")[0]).exists(), (path, target)
    index = (PACK / "README.md").read_text()
    for name in (ROOT / "cds/prompts").glob("*.txt"):
        assert name.name in index
    assert "ai-guidance/README.md" in (ROOT / "README.md").read_text()
    assert "ai-guidance/README.md" in (ROOT / "docs/local-ai.md").read_text()
