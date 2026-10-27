"""Offline evaluation provenance and routing, never a real-model quality test."""
import hashlib

import pytest

from cds import local_model, prompt_guidance
from cds.config import Settings
from scripts.evaluate_local_ai import evaluate
from tests.model_stub import LOCAL_MODEL, cite_first_facts

@pytest.fixture
def scripted_model(tmp_path, monkeypatch):
    calls = []
    def call(url, path, payload=None, **kwargs):
        if path == "/api/tags":
            return {"models": [LOCAL_MODEL]}
        if path == "/api/show":
            return {"details": {"format": "gguf"}, "capabilities": ["completion"]}
        if path == "/api/version":
            return {"version": "synthetic-runtime"}
        assert path == "/api/chat"
        calls.append(payload)
        return cite_first_facts(payload)[1]
    monkeypatch.setattr(local_model, "call", call)
    return Settings(tmp_path, ai_context=32768), calls


OPERATIONS = {"assessment", "case_chat", "concept_chat", "prior_assessment_chat"}


def test_default_manifest_covers_operations_without_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(local_model, "call", lambda *a, **kw: pytest.fail("No opt-in"))
    report = evaluate(Settings(tmp_path))
    assert {op["kind"] for row in report["scenarios"] for op in row["operations"]} == OPERATIONS
    assert {row["name"] for row in report["scenarios"]} >= {
        "sparse_registry", "mixed_supported_unknown", "false_premise_hash", "prior_wrong_ai",
        "concept_registry", "concept_mixed", "hostile_filename"}
    for row in report["scenarios"]:
        for op in row["operations"]:
            assert op["prompt_sha256"] and op["prompt_version"] and op["guidance_version"] == "0.2"
            assert op["manual_status"] == "not_reviewed" and op["manual_findings"] is None
            assert op["mechanical_status"] == "not_run" and not op["raw_model_replies"]


def test_filters_are_selective_and_invalid_selection_fails(tmp_path):
    report = evaluate(Settings(tmp_path), scenarios=["sparse_registry"], operations=["case_chat"])
    assert [row["name"] for row in report["scenarios"]] == ["sparse_registry"]
    assert [op["kind"] for op in report["scenarios"][0]["operations"]] == ["case_chat"]
    for kwargs in ({"scenarios": ["missing"]}, {"operations": ["missing"]},
                   {"scenarios": ["concept_registry"], "operations": ["assessment"]}):
        with pytest.raises(ValueError):
            evaluate(Settings(tmp_path), **kwargs)


@pytest.mark.parametrize("failure", ["malformed", "length", "transport", "context"])
def test_rejected_operations_keep_guidance_and_raw_replies(scripted_model, monkeypatch, failure):
    settings, calls = scripted_model
    original_call = local_model.call
    def broken(url, path, *args, **kwargs):
        reply = original_call(url, path, *args, **kwargs)
        if path == "/api/chat":
            if failure == "transport":
                raise local_model.ModelError(503, "Synthetic connection failure")
            if failure == "length":
                reply["done_reason"] = "length"
            else:
                reply["message"]["content"] = "not JSON"
        return reply
    monkeypatch.setattr(local_model, "call", broken)
    if failure == "context":
        monkeypatch.setattr(local_model, "input_limit", lambda *a, **kw: 1)
    report = evaluate(settings, real_model=True)
    assert local_model.call is broken
    for row in report["scenarios"]:
        for op in row["operations"]:
            assert op["mechanical_status"] == "rejected" and op["error"]
            assert op["manual_status"] == "not_reviewed"
            assert op["prompt_version"] and len(op["prompt_sha256"]) == 64
            assert bool(op["raw_model_replies"]) == (failure in ("malformed", "length"))
    if failure == "context":
        assert not calls
    else:
        ops = [op for row in report["scenarios"] for op in row["operations"]]
        for payload, op in zip(calls, ops, strict=True):
            assert op["prompt_sha256"] == hashlib.sha256(payload["messages"][0]["content"].encode()).hexdigest()


def test_all_outgoing_evaluation_requests_exclude_rubrics_and_route_modes(scripted_model):
    settings, calls = scripted_model
    report = evaluate(settings, real_model=True)
    operations = {"assessment": "brief", "case_chat": "case", "concept_chat": "concepts",
                  "prior_assessment_chat": "prior_assessment"}
    paired = [(row, op) for row in report["scenarios"] for op in row["operations"]]
    for payload, (row, op) in zip(calls, paired, strict=True):
        text = str(payload["messages"])
        assert all(other["name"] not in text and all(r not in text for r in other["rubric"])
                   for other in report["scenarios"])
        assert payload["messages"][0]["content"] == prompt_guidance.get_prompt(operations[op["kind"]]).text
        assert op["mechanical_status"] == "accepted_unverified"
        assert op["prompt_sha256"] == op["result"]["prompt_sha256"]
        if op["kind"] == "concept_chat":
            assert "F1:" not in text and not op["result"]["facts"]
        if op["kind"] == "prior_assessment_chat":
            assert "prior_UNVERIFIED_assessment_NOT_evidence" in text
            assert row["prior_assessment"]["inferences"][0]["limitations"][0] in text


def test_saved_assessment_discussion_fits_default_context(scripted_model):
    from cds import case_brief, learning
    from cds.store import Store
    from tests.test_saved_assessments import seed
    settings, _ = scripted_model
    store = Store(settings.data_dir)
    store.initialize()
    case, _, _ = seed(store)
    sheet = case_brief.fact_sheet(store.brief_inputs(case))
    brief = store.save_brief(sheet, case_brief.generate(settings, sheet["facts"]))
    result = learning.generate(Settings(store.root), learning.ChatInput(mode="case", message="Explain the assessment"),
                               sheet, brief)
    assert result["selected_brief"]["id"] == brief["id"]
    assert result["context"]["input_bytes"] <= result["context"]["input_limit_bytes"]
