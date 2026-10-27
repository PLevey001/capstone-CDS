"""Runtime composition and provenance checks; scripted replies do not grade meaning."""
import asyncio
import hashlib
import importlib
import json
from pathlib import Path
from importlib.resources import files
import tomllib

import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError
from cds import case_brief, learning, local_model, prompt_guidance
from cds.config import Settings
from tests.model_stub import assessment, complete_reply
from cds.main import create_app
from tests.test_local_ai_unit import model, saved_source

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "cds" / "prompts"
COMPOSITIONS = {
    "brief": ("common.txt", "case-brief.txt"),
    "concepts": ("common.txt", "chat-common.txt", "chat-concepts.txt"),
    "case": ("common.txt", "chat-common.txt", "chat-case.txt"),
    "prior_assessment": ("common.txt", "chat-common.txt", "chat-case.txt", "chat-prior-assessment.txt"),
}


def expected_system(operation):
    return "\n\n".join((PROMPTS / name).read_text(encoding="utf-8").strip()
                        for name in COMPOSITIONS[operation])


def run_operation(settings, state, operation, facts=None):
    facts = facts or [{"id": "F1", "kind": "case", "text": "Synthetic case.", "source_id": None},
             {"id": "F2", "kind": "source", "text": "Only saved observations are available.", "source_id": "s"},
             {"id": "F3", "kind": "scope", "text": case_brief.SCOPE_NOTE, "source_id": None}]
    if operation == "brief":
        state["reply"] = complete_reply(assessment())
        return case_brief.generate(settings, facts)
    state["reply"] = complete_reply({"answer": [], "cannot_answer": True})
    prior = None
    if operation == "prior_assessment":
        prior = {**case_brief.check_assessment(json.dumps(assessment()), facts),
                 "id": "synthetic-brief", "version": 1, "context": {}}
    return learning.generate(settings, learning.ChatInput(
        mode="concepts" if operation == "concepts" else "case", message="What is supported?"),
        {"facts": facts, "facts_digest": case_brief.digest(facts), "provenance": {}}, prior)


@pytest.mark.parametrize("operation", COMPOSITIONS)
def test_outgoing_system_is_composed_from_actual_files_and_exactly_hashed(model, operation):
    settings, state, requests = model
    result = run_operation(settings, state, operation)
    payload = next(payload for path, payload, _ in requests if path == "/api/chat")
    system = payload["messages"][0]
    assert system == {"role": "system", "content": expected_system(operation)}
    assert result["prompt_sha256"] == hashlib.sha256(system["content"].encode("utf-8")).hexdigest()
    assert result["prompt_version"] == ("case-assessment.v5" if operation == "brief" else "learning.v4")
    assert result["guidance_version"] == "0.2"
    assert result["context"]["input_bytes"] == len(local_model.encoded({
        "messages": payload["messages"], "format": payload["format"]}))
    assert payload["options"]["num_predict"] == (1800 if operation == "brief" else 900)
    assert all(path.name not in system["content"] for path in (ROOT / "docs" / "ai-guidance").iterdir())
    for path in (ROOT / "docs" / "ai-guidance").iterdir():
        assert path.read_text().strip() not in json.dumps(payload)
    if operation == "prior_assessment":
        assert "historical" in system["content"] and "pinned" in system["content"]
        assert "fresh" not in system["content"]


def test_chat_mode_and_selected_assessment_have_distinct_checksums(model):
    settings, state, _ = model
    results = [run_operation(settings, state, op) for op in COMPOSITIONS]
    assert len({result["prompt_sha256"] for result in results}) == 4


def test_shared_file_edit_affects_both_paths_only_after_reload(model, monkeypatch):
    settings, state, requests = model
    original = {op: run_operation(settings, state, op)["prompt_sha256"] for op in COMPOSITIONS}
    read_text = Path.read_text
    marker = "Shared edit sentinel — preserved as UTF-8."
    def edited(path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        return text + "\n" + marker if path == PROMPTS / "common.txt" else text
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", edited)
            assert run_operation(settings, state, "brief")["prompt_sha256"] == original["brief"]
            importlib.reload(prompt_guidance)
            for op in COMPOSITIONS:
                result = run_operation(settings, state, op)
                outgoing = requests[-1][1]["messages"][0]["content"]
                assert marker in outgoing
                assert result["prompt_sha256"] != original[op]
                assert result["prompt_sha256"] == hashlib.sha256(outgoing.encode()).hexdigest()
    finally:
        importlib.reload(prompt_guidance)


@pytest.mark.parametrize("failure", ["missing", "blank", "invalid_utf8"])
def test_startup_fails_clearly_for_unusable_guidance(monkeypatch, failure):
    read_text = Path.read_text
    def broken(path, *args, **kwargs):
        if path == PROMPTS / "chat-case.txt":
            if failure == "missing":
                raise FileNotFoundError(path)
            if failure == "invalid_utf8":
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
            return " \n\t "
        return read_text(path, *args, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", broken)
            with pytest.raises(RuntimeError, match=r"guidance.*chat-case.txt"):
                importlib.reload(prompt_guidance)
    finally:
        importlib.reload(prompt_guidance)


@pytest.mark.parametrize("name", ["../common.txt", "/tmp/common.txt", "chat-case.txt", "docs/ai-guidance/README.md"])
def test_callers_cannot_select_prompt_paths(name):
    with pytest.raises(ValueError, match="operation"):
        prompt_guidance.get_prompt(name)
    with pytest.raises(ValueError, match="filename"):
        prompt_guidance._read_file("/tmp/" + name)


def test_allowlisted_file_cannot_be_an_external_symlink(tmp_path, monkeypatch):
    (tmp_path / "common.txt").symlink_to(PROMPTS / "common.txt")
    monkeypatch.setattr(prompt_guidance, "_DIRECTORY", tmp_path)
    with pytest.raises(RuntimeError, match="symlink"):
        prompt_guidance._read_file("common.txt")


def test_guidance_is_counted_before_omitting_mandatory_source_scope(model, monkeypatch):
    settings, state, requests = model
    settings = Settings(settings.data_dir, ai_context=10000)
    facts = [{"id": "F1", "kind": "case", "text": "Synthetic case.", "source_id": None},
             {"id": "F2", "kind": "source", "text": "Saved source.", "source_id": "s"},
             {"id": "F3", "kind": "source_scope", "text": "Mandatory saved source scope. " * 90, "source_id": "s"},
             {"id": "F4", "kind": "scope", "text": case_brief.SCOPE_NOTE, "source_id": None}]
    for op in ("brief", "case", "prior_assessment"):
        assert run_operation(settings, state, op, facts)["context"]["sources_omitted"] == 0
    requests.clear()
    real_read = Path.read_text
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", lambda path, *a, **kw: real_read(path, *a, **kw) +
                          ("Shared guidance. " * 150 if path == PROMPTS / "common.txt" else ""))
            importlib.reload(prompt_guidance)
            for op in ("brief", "case", "prior_assessment"):
                with pytest.raises(local_model.ModelError, match="omit.*sources") as error:
                    run_operation(settings, state, op, facts)
                assert error.value.status == 422
            assert not requests
    finally:
        importlib.reload(prompt_guidance)


def test_saved_and_downloaded_metadata_cover_actual_system(model):
    settings, state, calls = model
    app = create_app(settings, start_workers=False)
    case, _ = saved_source(app.state.store, {"artifacts": []})
    post = next(route.endpoint for route in app.routes if getattr(route, "path", "") ==
                "/api/cases/{case_id}/brief" and "POST" in route.methods)
    download = next(route.endpoint for route in app.routes if getattr(route, "path", "") ==
                    "/api/cases/{case_id}/briefs/{brief_id}/download")
    state["reply"] = complete_reply(assessment())
    document = post(case)["document"]
    digest = hashlib.sha256(calls[-1][1]["messages"][0]["content"].encode()).hexdigest()
    saved = app.state.store.saved_brief(case, document["id"])
    for result in (document, saved["document"], json.loads(download(case, document["id"]).body)):
        assert result["prompt_sha256"] == digest
        assert result["prompt_version"] == "case-assessment.v5"
        assert result["guidance_version"] == "0.2"
    assert download(case, document["id"]).body == saved["serialized"].encode()


def test_all_runtime_resources_exist_and_are_in_package_data():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = config["tool"]["setuptools"]["package-data"]["cds"]
    packaged = {path.relative_to(ROOT / "cds").as_posix()
                for pattern in patterns for path in (ROOT / "cds").glob(pattern)}
    for name in {name for names in COMPOSITIONS.values() for name in names}:
        assert "prompts/" + name in packaged
        assert files("cds").joinpath("prompts", name).read_text(encoding="utf-8").strip()



def test_brief_reserves_more_output_without_changing_chat(tmp_path):
    settings = Settings(tmp_path, ai_context=32768)
    assert local_model.OUTPUT_TOKENS == 900
    assert local_model.BRIEF_OUTPUT_TOKENS == 1800
    assert local_model.input_limit(settings) == 30844
    assert local_model.input_limit(settings, local_model.BRIEF_OUTPUT_TOKENS) == 29944


def test_length_stop_is_not_reported_as_connection_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(local_model, 'discover', lambda *args: {})
    def reply(url, path, *args, **kwargs):
        if path == '/api/version': return {'version': 'test'}
        return {'done': True, 'done_reason': 'length', 'eval_count': 1800}
    monkeypatch.setattr(local_model, 'call', reply)
    with pytest.raises(local_model.ModelError, match='1800-token output limit') as error:
        local_model.chat(Settings(tmp_path), [], {}, output_tokens=1800)
    assert error.value.status == 502


def test_generation_schema_limits_citation_padding():
    schema = case_brief.response_schema([{'id': 'F1'}])
    assert schema['$defs']['item']['properties']['facts']['maxItems'] == 3
    assert schema['$defs']['inference']['properties']['facts']['maxItems'] == 3


@pytest.mark.parametrize("field", ["prompt_file", "guidance_path", "system"])
def test_requests_cannot_supply_guidance_files_or_system_content(field):
    with pytest.raises(ValidationError):
        learning.ChatInput.model_validate({"mode": "case", "message": "Explain", field: "/tmp/instructions.txt"})


def test_legacy_export_bytes_unchanged_after_guidance_reload(model):
    settings, state, _ = model
    app = create_app(settings, start_workers=False)
    case, _ = saved_source(app.state.store, {"artifacts": []})
    sheet = case_brief.fact_sheet(app.state.store.brief_inputs(case))
    state["reply"] = complete_reply(assessment())
    legacy = case_brief.generate(settings, sheet["facts"])
    legacy.update(prompt_version="case-assessment.v3", prompt_sha256="a" * 64)
    del legacy["guidance_version"]
    document = app.state.store.save_brief(sheet, legacy)
    before = app.state.store.saved_brief(case, document["id"])["serialized"].encode()
    importlib.reload(prompt_guidance)
    restarted = create_app(settings, start_workers=False)
    download = next(route.endpoint for route in restarted.routes if getattr(route, "path", "") ==
                    "/api/cases/{case_id}/briefs/{brief_id}/download")
    assert download(case, document["id"]).body == before
    assert "guidance_version" not in json.loads(before)


def test_selected_chat_uses_pinned_facts_and_digest_after_fact_wording_changes(model, monkeypatch):
    settings, state, calls = model
    app = create_app(settings, start_workers=False)
    case, _ = saved_source(app.state.store, {"artifacts": []})
    post = next(route.endpoint for route in app.routes if getattr(route, "path", "") ==
                "/api/cases/{case_id}/brief" and "POST" in route.methods)
    state["reply"] = complete_reply(assessment())
    document = post(case)["document"]
    original_facts = case_brief.build_facts
    def changed_wording(inputs):
        facts = original_facts(inputs)
        facts[0]["text"] = "New computed wording after a code update."
        return facts
    monkeypatch.setattr(case_brief, "build_facts", changed_wording)
    # Unit-call the real endpoint with scheduling inline; HTTP transport is covered by the API suite.
    async def inline_thread(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    chat = next(route.endpoint for route in app.routes if getattr(route, "path", "") ==
                "/api/cases/{case_id}/chat")
    state["reply"] = complete_reply({"answer": [{"statement": "The supplied snapshot describes saved results.",
                                                "facts": ["F1"]}], "cannot_answer": False})
    body = {"mode": "case", "message": "Explain the brief", "brief_id": document["id"]}
    def request(data):
        async def receive():
            return {"type": "http.request", "body": json.dumps(data).encode(), "more_body": False}
        return Request({"type": "http", "headers": []}, receive)
    first = asyncio.run(chat(case, request(body)))
    assert first["facts"] == document["facts"]
    body = body | {"message": "Explain its limitations", "facts_digest": first["facts_digest"], "history": [
        {"role": "user", "content": body["message"]}, {"role": "assistant", "content": first["answer"][0]["text"]}]}
    result = asyncio.run(chat(case, request(body)))
    assert "Explain the brief" in calls[-1][1]["messages"][1]["content"]
    assert first["answer"][0]["text"] in calls[-1][1]["messages"][1]["content"]
    assert result["facts"] == document["facts"]
    assert result["facts_digest"] == document["facts_digest"]
    assert result["provenance"] == document["provenance"]
    assert "New computed wording" not in calls[-1][1]["messages"][1]["content"]
    assert result["prompt_sha256"] == hashlib.sha256(calls[-1][1]["messages"][0]["content"].encode()).hexdigest()
    current_digest = case_brief.fact_sheet(app.state.store.brief_inputs(case))["facts_digest"]
    with pytest.raises(HTTPException) as error:
        asyncio.run(chat(case, request(body | {"facts_digest": current_digest})))
    assert error.value.status_code == 409
    app.state.store.rename_case(case, "Changed input")
    with pytest.raises(HTTPException) as error:
        asyncio.run(chat(case, request(body)))
    assert error.value.status_code == 409 and "stale" in error.value.detail


def test_compact_brief_system_keeps_combined_case_budget_headroom():
    # The supplied combined acceptance snapshot failed whole-source selection above this budget.
    assert len(prompt_guidance.get_prompt("brief").text.encode("utf-8")) < 1500
