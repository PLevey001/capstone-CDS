"""Mechanical regressions from synthetic live findings; these do not grade semantics."""
import copy
import json

import pytest

from cds import case_brief, learning, local_model
from cds.config import Settings
from scripts import run
from scripts.evaluate_local_ai import SCENARIOS, fixture
from tests.model_stub import assessment, complete_reply
from tests.test_local_ai_unit import model


REFERENCES = [
    {"id": "F1", "text": "Saved inventory has 2 entries."},
    {"id": "F2", "text": "Coverage is partial."},
    {"id": "G1", "text": "General concept text."},
    {"id": "A-inferences-1", "text": "A prior tentative interpretation."},
]


@pytest.mark.parametrize("inline", ["F2", "F999", "G1", "G999", "A-inferences-1", "A-unknown-99"])
def test_chat_withholds_unknown_or_unlisted_inline_references(inline):
    content = {"overview": [{"statement": f"Review {inline}.", "facts": ["F1"]},
                            {"statement": "Review F1.", "facts": ["F1"]}], "review": []}
    kept, _, withheld = case_brief.check_statements(json.dumps(content), REFERENCES)
    assert withheld == 1 and [item["text"] for item in kept] == ["Review F1."]


def test_matching_inline_references_allow_multiple_namespaces_without_number_support():
    text = "Review F2, G1 and A-inferences-1."
    kept, _, withheld = case_brief.check_statements(json.dumps({"overview": [
        {"statement": text, "facts": ["F2", "G1", "A-inferences-1"]}], "review": []}), REFERENCES)
    assert withheld == 0 and kept[0]["text"] == text
    assert case_brief.prose_numbers("F5, F9 and A-inferences-1 mention 7 files.") == {"7"}


def test_reported_inline_citation_pattern_is_withheld_without_claiming_semantic_checks():
    text = "The run failed (F10, F11, F12), while another completed (F2). Coverage is partial (F3); scope is limited (F19)."
    facts = [{"id": f"F{i}", "text": "Saved scope information."} for i in range(1, 20)]
    with pytest.raises(local_model.ModelError, match="mechanical checks"):
        case_brief.check_statements(json.dumps({"overview": [
            {"statement": text, "facts": ["F10", "F11", "F12", "F15"]}], "review": []}), facts)


def inference():
    return {"statement": "A common origin is possible.", "facts": ["F1"],
            "reasoning": "The observations could have a shared explanation.",
            "alternatives": ["Independent observations could also agree."],
            "limitations": ["The relationship remains unverified."]}


@pytest.mark.parametrize("field", ["statement", "reasoning", "alternatives", "limitations"])
@pytest.mark.parametrize("inline", ["F2", "F999", "G1", "A-inferences-1"])
def test_assessment_checks_inline_references_in_every_inference_field(field, inline):
    item = inference()
    item[field] = [f"Review {inline}."] if field in ("alternatives", "limitations") else f"Review {inline}."
    content = assessment() | {"inferences": [item]}
    with pytest.raises(local_model.ModelError, match="inline references"):
        case_brief.check_assessment(json.dumps(content), REFERENCES[:2])


@pytest.mark.parametrize("section", ["overall_summary", "observations", "review_next"])
def test_assessment_checks_inline_references_in_non_inference_categories(section):
    content = assessment() | {section: [{"statement": "Review F2.", "facts": ["F1"]}]}
    with pytest.raises(local_model.ModelError, match="inline references"):
        case_brief.check_assessment(json.dumps(content), REFERENCES[:2])


def prior_assessment():
    # The only digits are metadata/reference labels. They must not support quantitative claims.
    item = {"id": "A-inferences-1", "text": "A common origin is possible.", "facts": ["F5", "F9"],
            "verification": "unverified", "reasoning": "Compare F5 and F9.",
            "alternatives": ["Independent observations could agree."], "limitations": ["No event was recorded."]}
    return {"id": "synthetic-brief", "version": 1, "context": {},
            "overall_summary": [], "observations": [], "inferences": [item], "review_next": []}


def discussion_sheet():
    facts = [{"id": "F1", "kind": "case", "text": "Synthetic case.", "source_id": None},
             {"id": "F5", "kind": "source", "text": "Recorded observation.", "source_id": "first"},
             {"id": "F9", "kind": "source", "text": "Another recorded observation.", "source_id": "second"},
             {"id": "F10", "kind": "scope", "text": "Saved scope only.", "source_id": None}]
    return {"facts": facts, "facts_digest": case_brief.digest(facts), "provenance": {}}


@pytest.mark.parametrize("number", [1, 5, 9])
def test_assessment_reference_metadata_digits_do_not_support_quantities(model, number):
    settings, state, _ = model
    state["reply"] = complete_reply({"answer": [{"statement": f"There are {number} files.",
                                                 "facts": ["A-inferences-1"]}], "cannot_answer": False})
    with pytest.raises(local_model.ModelError, match="mechanical checks"):
        learning.generate(settings, learning.ChatInput(mode="case", message="Explain"),
                          discussion_sheet(), prior_assessment())


@pytest.mark.parametrize("field", ["text", "reasoning", "alternatives", "limitations"])
def test_assessment_numeric_comparison_uses_each_prose_field_only(model, field):
    settings, state, _ = model
    prior = prior_assessment()
    prior["inferences"][0][field] = ["The prior interpretation mentions 7."] if field in (
        "alternatives", "limitations") else "The prior interpretation mentions 7."
    state["reply"] = complete_reply({"answer": [{"statement": "The prior interpretation mentions 7.",
                                                 "facts": ["A-inferences-1"]}], "cannot_answer": False})
    result = learning.generate(settings, learning.ChatInput(mode="case", message="Explain"), discussion_sheet(), prior)
    assert result["answer"][0]["verification"] == "unverified"
    assert result["assessment_items"][0][field] == prior["inferences"][0][field]


def test_generation_schemas_do_not_force_string_length_but_server_still_caps_it():
    for schema in (case_brief.response_schema(REFERENCES), learning.response_schema(REFERENCES)):
        assert '"maxLength"' not in json.dumps(schema)
    valid = "A" * 399 + "."
    content = assessment(valid)
    assert case_brief.check_assessment(json.dumps(content), REFERENCES)["overall_summary"][0]["text"] == valid
    content["overall_summary"][0]["statement"] = valid + "."
    with pytest.raises(local_model.ModelError, match="length"):
        case_brief.check_assessment(json.dumps(content), REFERENCES)
    kept, _, withheld = case_brief.check_statements(json.dumps({"overview": [
        {"statement": valid + ".", "facts": ["F1"]}, {"statement": "Complete sentence.", "facts": ["F1"]}],
        "review": []}), REFERENCES)
    assert withheld == 1 and kept[0]["text"] == "Complete sentence."


@pytest.mark.parametrize("field", ["reasoning", "alternatives", "limitations"])
def test_inference_length_limit_remains_server_enforced(field):
    item = inference()
    item[field] = ["x" * 401] if field != "reasoning" else "x" * 401
    with pytest.raises(local_model.ModelError):
        case_brief.check_assessment(json.dumps(assessment() | {"inferences": [item]}), REFERENCES)


@pytest.mark.parametrize("ending", [",", ";", ":"])
def test_dangling_punctuation_is_rejected_without_rewriting(ending):
    content = assessment("The source was examined" + ending)
    before = copy.deepcopy(content)
    with pytest.raises(local_model.ModelError, match="dangling punctuation"):
        case_brief.check_assessment(json.dumps(content), REFERENCES)
    assert content == before
    with pytest.raises(local_model.ModelError, match="mechanical checks"):
        case_brief.check_statements(json.dumps({"overview": content["overall_summary"], "review": []}), REFERENCES)


def test_punctuation_check_does_not_claim_complete_grammar_or_semantics():
    for text in ("Review the scope", "These results are of", "Claims can be wrong."):
        assert not case_brief.dangling_punctuation(text)
    # A dangling word without punctuation is still a manual quality failure, not a semantic pass.


@pytest.mark.parametrize("context", [4096, 8191, 32769])
def test_unusable_context_settings_fail_with_actionable_minimum(tmp_path, context):
    with pytest.raises(ValueError, match="8192.*32768.*minimum supported"):
        Settings(tmp_path, ai_context=context)


def test_supported_minimum_fits_short_concept_and_assessment_requests(model):
    settings, state, _ = model
    assert settings.ai_context == 8192
    result = learning.generate(settings, learning.ChatInput(mode="concepts", message="Explain SHA-256"))
    assert result["context"]["input_bytes"] < result["context"]["input_limit_bytes"]
    state["reply"] = complete_reply(assessment())
    result = case_brief.generate(settings, discussion_sheet()["facts"])
    assert result["context"]["sources_omitted"] == 0
    assert result["context"]["input_bytes"] < result["context"]["input_limit_bytes"]


def test_launcher_reports_context_floor_before_locks(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CDS_AI_CONTEXT", "4096")
    monkeypatch.delenv("CDS_AI_SERVICE", raising=False)
    assert run.main(tmp_path) == 1
    assert "CDS_AI_CONTEXT must be between 8192 and 32768" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def test_evaluator_never_sends_diagnostic_labels_and_includes_hypothesis_relevant_evidence():
    for scenario in SCENARIOS:
        prompt = case_brief.build_prompt(fixture(scenario))
        assert scenario["name"] not in prompt
        assert fixture(scenario)[0]["text"] == "Synthetic case."
        assert all(rubric not in prompt for rubric in scenario["rubric"])
    matching = next(s for s in SCENARIOS if s["name"] == "matching_contents")
    assert "identical recorded SHA-256" in " ".join(matching["observations"])
    assert "No copy event or actor was established" in " ".join(matching["observations"])
