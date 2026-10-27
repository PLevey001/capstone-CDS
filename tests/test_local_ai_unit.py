"""Socket-free contract checks, complementing the real HTTP model stub tests."""
import asyncio
import json
import threading
import time

import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError

from cds import case_brief, prompt_guidance, learning, local_model
from cds.config import Settings
from cds.main import create_app
from cds.store import Store
from cds.history_records import SNAPSHOT_SCOPE
from cds.registry_records import SCOPE as REGISTRY_SCOPE
from cds.evtx_records import SCOPE as EVTX_SCOPE
from tests.model_stub import LOCAL_MODEL, assessment, complete_reply


@pytest.fixture
def model(tmp_path, monkeypatch):
    requests = []
    state = {"reply": complete_reply({"answer": [{"statement": "A digest compares bytes.", "facts": ["G1"]}],
                                      "cannot_answer": False}),
             "models": [LOCAL_MODEL], "show": {"details": {"format": "gguf"}, "capabilities": ["completion"]}}

    def call(url, path, payload=None, **kwargs):
        requests.append((path, payload, kwargs))
        return {"/api/tags": {"models": state["models"]}, "/api/show": state["show"], "/api/version": {"version": "synthetic"}, "/api/chat": state["reply"]}[path]

    monkeypatch.setattr(local_model, "call", call)
    return Settings(tmp_path), state, requests


@pytest.mark.parametrize("patch", [{"done": False}, {"done_reason": "length"}, {"done_reason": None},
                                  {"model": "other:3b"}, {"message": {"role": "system", "content": "{}"}},
                                  {"message": {"role": "assistant", "content": "{}", "tool_calls": [{}]}}])
def test_completion_contract(model, patch):
    settings, state, _ = model
    state["reply"].update(patch)
    with pytest.raises(local_model.ModelError) as error:
        local_model.chat(settings, [], {})
    assert error.value.status == 502


def test_completion_requires_done_and_bounds_content(model):
    settings, state, _ = model
    del state["reply"]["done"]
    with pytest.raises(local_model.ModelError, match="incomplete"):
        local_model.chat(settings, [], {})
    state["reply"] = complete_reply("x" * (local_model.OUTPUT_BYTES + 1))
    with pytest.raises(local_model.ModelError, match="output limit"):
        local_model.chat(settings, [], {})


@pytest.mark.parametrize("entry", [{"remote_host": "https://ollama.com"}, {"remote_model": "remote"}])
def test_cloud_metadata_never_reaches_chat(model, entry):
    settings, state, requests = model
    state["show"].update(entry)
    with pytest.raises(local_model.ModelError) as error:
        local_model.chat(settings, [], {})
    assert error.value.status == 503
    assert all(path != "/api/chat" for path, _, _ in requests)


@pytest.mark.parametrize("name", ["x-cloud", "x:cloud", "x-cloud:latest", "cloud/x", "X-CLOUD:3b"])
def test_cloud_names_rejected_before_discovery(tmp_path, name):
    settings = Settings(tmp_path, ai_model=name)
    assert local_model.status(settings)["available"] is False


def test_one_deadline_and_bounded_input_before_discovery(model):
    settings, _, requests = model
    local_model.chat(settings, [{"role": "user", "content": "hi"}], {})
    deadlines = {kwargs["deadline"] for path, _, kwargs in requests if path != "/api/version"}
    assert len(deadlines) == 1
    assert next(kwargs["deadline"] for path, _, kwargs in requests if path == "/api/version") <= next(iter(deadlines))
    requests.clear()
    with pytest.raises(local_model.ModelError) as error:
        local_model.chat(settings, [{"role": "user", "content": "界" * 3000}], {})
    assert error.value.status == 422 and not requests


def test_context_selection_retains_scope_and_counts_omissions(model):
    settings, _, _ = model
    facts = [{"id": "F1", "kind": "case", "text": "Case with synthetic sources.", "source_id": None}]
    facts += [{"id": f"F{i}", "kind": "source", "text": "界" * 300, "source_id": f"s{i}", "run_id": f"r{i}"}
              for i in range(2, 100)]
    facts.append({"id": "F100", "kind": "scope", "text": case_brief.SCOPE_NOTE, "source_id": None})

    def parts(selected, notice):
        return [{"role": "system", "content": prompt_guidance.get_prompt("brief").text},
                {"role": "user", "content": case_brief.build_prompt(selected) + notice}], case_brief.response_schema(selected)

    selected, limits = case_brief.select_facts(settings, facts, parts)
    assert selected == case_brief.select_facts(settings, facts, parts)[0]
    assert selected[0]["id"] == "F1" and selected[-1]["kind"] == "scope"
    assert limits["facts_omitted"] == len(facts) - len(selected) > 0
    assert limits["sources_omitted"] == limits["facts_omitted"]
    messages, schema = parts(selected, limits["notice"])
    assert len(local_model.encoded({"messages": messages, "format": schema})) <= local_model.input_limit(settings)


def test_concepts_followup_and_fixed_refusal(model):
    settings, state, requests = model
    first = learning.generate(settings, learning.ChatInput(mode="concepts", message="What is a hash?"))
    assert first["glossary"][0]["url"] == learning.GLOSSARY[0]["url"]
    assert first["facts"] == [] and first["provenance"] is None
    question = learning.ChatInput(mode="concepts", message="Does that prove identity?", history=[
        {"role": "user", "content": "What is a hash?"}, {"role": "assistant", "content": first["answer"][0]["text"]}])
    state["reply"] = complete_reply({"answer": [], "cannot_answer": True})
    answer = learning.generate(settings, question)
    assert answer["uncertainty"] and not answer["answer"]
    assert "local concept references" in answer["uncertainty"]
    assert "switch" not in answer["uncertainty"]
    assert "What is a hash?" in requests[-1][1]["messages"][1]["content"]


def test_reviewed_lessons_preserve_integrity_and_registry_distinctions():
    lessons = {entry["id"]: entry["text"] for entry in learning.GLOSSARY}
    assert "byte integrity relative to that reference" in lessons["G1"]
    assert "does not prove the reference is trustworthy or the software is safe" in lessons["G1"]
    assert "authenticity, authorship, intent" in lessons["G1"]
    assert "Individual Registry values have no independent change timestamp" in lessons["G6"]
    assert "not a change time for a particular value" in lessons["G6"]
    assert "Prefer one concise answer item" in prompt_guidance.get_prompt("concepts").text


@pytest.mark.parametrize("content", ["not json", "[]", '{"answer": [], "cannot_answer": "yes"}',
                                     '{"answer": [], "cannot_answer": false}',
                                     '{"answer": [{}], "cannot_answer": true}'])
def test_chat_malformed_output(model, content):
    settings, state, _ = model
    state["reply"] = complete_reply(content)
    with pytest.raises(local_model.ModelError) as error:
        learning.generate(settings, learning.ChatInput(mode="concepts", message="Explain"))
    assert error.value.status == 502


def test_case_answer_only_references_server_facts(model):
    settings, state, _ = model
    sheet = {"facts": [{"id": "F1", "kind": "case", "text": "There are 2 saved sources.", "source_id": None},
                       {"id": "F2", "kind": "scope", "text": case_brief.SCOPE_NOTE, "source_id": None}],
             "facts_digest": "a" * 64, "provenance": {"case_id": "a"}}
    state["reply"] = complete_reply({"cannot_answer": False, "answer": [
        {"statement": "Wrong facts.", "facts": ["F1", "G1"]},
        {"statement": "There are 3 sources.", "facts": ["F1"]},
        {"statement": "There are 2 saved sources.", "facts": ["F1"]}]})
    result = learning.generate(settings, learning.ChatInput(mode="case", message="What is saved?"), sheet)
    assert len(result["answer"]) == 1 and result["withheld"] == 2
    assert result["facts_digest"] == sheet["facts_digest"] and result["provenance"] == sheet["provenance"]


@pytest.mark.parametrize("patch", [{"message": " "}, {"message": "界" * 700}, {"message": "x" * 2001},
                                  {"history": [{"role": "system", "content": "x"}]},
                                  {"history": [{"role": "user", "content": "x"}] * 10}, {"facts": []}])
def test_input_bounds_and_roles(patch):
    with pytest.raises(ValidationError):
        learning.ChatInput.model_validate({"mode": "concepts", "message": "hello", **patch})


def test_overbudget_history_hard_rejected(model):
    settings, _, requests = model
    history = [{"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 1800} for index in range(8)]
    with pytest.raises(local_model.ModelError) as error:
        learning.generate(settings, learning.ChatInput(mode="concepts", message="hello", history=history))
    assert error.value.status == 422 and not requests


@pytest.fixture
def transport(monkeypatch):
    """A bounded fake HTTP connection tests transport decisions without sockets."""
    state = {"raw": b'{}', "status": 200, "delay": False, "closed": False, "length": 0}
    stopped = threading.Event()

    class Response:
        status = 200
        length = 0

        def read(self, limit):
            state["read_limit"] = limit
            if state["delay"]:
                assert stopped.wait(1)
            return state["raw"][:limit]

        def close(self):
            state["response_closed"] = True

    class Connection:
        def __init__(self, host, port, timeout):
            state.update(host=host, port=port, timeout=timeout)
            self.sock = self

        def connect(self):
            pass

        def shutdown(self, how):
            stopped.set()

        def request(self, method, path, body, headers):
            state.update(method=method, path=path, body=body)

        def getresponse(self):
            response = Response()
            response.status, response.length = state["status"], state["length"]
            return response

        def close(self):
            state["closed"] = True

    monkeypatch.setattr(local_model.http.client, "HTTPConnection", Connection)
    return state


def test_literal_loopback_and_no_proxy_redirect_transport(transport, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://example.invalid")
    monkeypatch.setenv("ALL_PROXY", "http://example.invalid")
    assert local_model.call("http://localhost:11434/", "/api/tags") == {}
    assert transport["host"] == "127.0.0.1" and transport["path"] == "/api/tags"
    assert transport["closed"] and transport["response_closed"]
    for code in (302, 307, 400):
        transport["status"] = code
        with pytest.raises(local_model.ModelError) as error:
            local_model.call("http://127.0.0.1:11434", "/api/chat", {"text": "synthetic"})
        assert error.value.status == 502
    with pytest.raises(local_model.ModelError, match="not on this machine"):
        local_model.call("http://example.invalid", "/api/tags")


@pytest.mark.parametrize("raw,length", [(b"x" * (local_model.RESPONSE_BYTES + 1), 0), (b"not json", 0),
                                       (b'{"error":"synthetic"}', 0), (b"{}", 12), (b"[]", 0)],
                         ids=["oversized", "invalid-json", "model-error", "incomplete-body", "wrong-shape"])
def test_transport_reply_limits(transport, raw, length):
    transport.update(raw=raw, length=length)
    with pytest.raises(local_model.ModelError) as error:
        local_model.call("http://127.0.0.1", "/api/chat", {})
    assert error.value.status == 502 and transport["closed"] and transport["response_closed"]
    assert transport["read_limit"] == local_model.RESPONSE_BYTES + 1


def test_transport_deadline_interrupts_body_read(transport):
    transport["delay"] = True
    started = time.monotonic()
    with pytest.raises(local_model.ModelError) as error:
        local_model.call("http://127.0.0.1", "/api/chat", {}, timeout=0.03)
    assert error.value.status == 504 and time.monotonic() - started < 0.5
    assert transport["closed"] and transport["response_closed"]


def test_failed_inventory_and_current_job_have_honest_saved_provenance(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    case = store.create_case("Synthetic inventory", "")
    source = tmp_path / "synthetic.img"
    source.write_bytes(b"synthetic")
    store.register("synthetic-source", case["id"], "synthetic.img", 9, source, "raw_image", 512)
    job = store.claim()
    store.finish(job, {"error": "Synthetic inventory failure", "artifacts": []})
    first = case_brief.build_facts(store.brief_inputs(case["id"]))
    assert any("counts are unavailable" in fact["text"] for fact in first)
    assert not any("0 file entries" in fact["text"] for fact in first)
    store.retry("synthetic-source")
    queued = case_brief.build_facts(store.brief_inputs(case["id"]))
    assert case_brief.digest(first) != case_brief.digest(queued)
    assert any("not the current job" in fact["text"] for fact in queued)
    linked = [fact for fact in queued if fact.get("source_id")]
    assert all(fact["run_id"] == job["run_id"] for fact in linked)
    new_job = store.claim()
    store.finish(new_job, {"artifacts": [], "coverage": {"status": "complete", "steps": [
        {"id": "filesystem:0", "status": "complete"}]}})
    measured = case_brief.build_facts(store.brief_inputs(case["id"]))
    assert any("0 file entries" in fact["text"] for fact in measured)
    assert all(fact["run_id"] == new_job["run_id"] for fact in measured if fact.get("source_id"))


@pytest.mark.parametrize("declared", [False, True])
def test_body_bound_precedes_json_parsing_including_chunked_input(tmp_path, monkeypatch, declared):
    app = create_app(Settings(tmp_path), start_workers=False)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/cases/{case_id}/chat")
    received = []

    async def receive():
        received.append(True)
        return {"type": "http.request", "body": b"x" * 4096, "more_body": True}

    def must_not_parse(*args):
        pytest.fail("Oversized body reached JSON parsing")

    monkeypatch.setattr(learning.ChatInput, "model_validate_json", must_not_parse)
    headers = [(b"content-length", b"999999")] if declared else []
    request = Request({"type": "http", "headers": headers}, receive)
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint("synthetic", request))
    assert error.value.status_code == 422
    assert len(received) == (0 if declared else 5)


def saved_source(store, result, name="synthetic.img", kind="raw_image"):
    store.initialize()
    case = store.create_case("Synthetic corrections", "")
    path = store.root / name
    path.write_bytes(b"synthetic")
    store.register("synthetic-source", case["id"], name, 9, path, kind, 512)
    job = store.claim()
    assert store.finish(job, result)
    return case["id"], job["run_id"]


def test_file_time_extrema_keep_pre_epoch_and_exclude_invalid_values(tmp_path):
    store = Store(tmp_path)
    values = [-315619200, 1700000000, -62135596801, 253402300800, 0, "1700000001"]
    case, _ = saved_source(store, {"artifacts": [
        {"path": f"/time-{index}", "kind": "file", "details": {"timestamps_unix": {"modified": value}}}
        for index, value in enumerate(values)]})
    source = store.brief_inputs(case)["sources"][0]
    assert source["first_file_time"] == "1960-01-01T00:00:00+00:00"
    assert source["last_file_time"] == "2023-11-14T22:13:20+00:00"


def test_browser_host_omissions_include_truncated_missing_and_invalid_urls(tmp_path):
    store = Store(tmp_path)
    prefix = ("https://" + "u" * 5000 + "@actual.example/path")[:4096]
    details = [{"url": prefix, "truncated_fields": ["url"]}, {}, {"url": "not a url"},
               {"url": "https://[broken"}, {"url": "https://actual.example/path"}]
    case, _ = saved_source(store, {
        "artifacts": [{"key": "source", "path": "History", "kind": "source"}],
        "records": [{"artifact_key": "source", "kind": "browser_visit", "source_key": str(index),
                     "summary": "Synthetic visit", "parser": "test", "event_time_us": None, "details": item}
                    for index, item in enumerate(details)]})
    source = store.brief_inputs(case)["sources"][0]
    assert source["hosts"] == [("actual.example", 1)] and source["host_total"] == 1
    assert source["host_omissions"] == {"truncated": 1, "missing": 1, "unclassifiable": 2}
    text = next(fact["text"] for fact in case_brief.source_facts(source) if fact["kind"] == "browser")
    assert "4 visits excluded from host counts" in text
    assert "1 truncated URL" in text and "1 missing URL" in text and "2 unclassifiable URLs" in text
    # An entirely unclassifiable set must still disclose the measured omissions.
    source.update(hosts=[], host_total=0)
    assert "4 visits excluded" in next(fact["text"] for fact in case_brief.source_facts(source)
                                      if fact["kind"] == "browser")


@pytest.mark.parametrize("scope", [SNAPSHOT_SCOPE, REGISTRY_SCOPE, EVTX_SCOPE], ids=["history", "registry", "evtx"])
def test_complete_coverage_retains_source_scope_details_and_warning_in_model(model, scope):
    settings, state, requests = model
    store = Store(settings.data_dir)
    detail = "Synthetic parser completed. " + scope
    warning = "Synthetic saved warning remains relevant after completion."
    case, run = saved_source(store, {"coverage": {"status": "complete", "scope": scope, "steps": [
        {"id": "content", "status": "complete", "detail": detail}]}, "warnings": [warning]}, "History", "file")
    facts = case_brief.build_facts(store.brief_inputs(case))
    notes = [fact for fact in facts if fact["kind"] == "source_scope"]
    assert all(fact["run_id"] == run and fact["source_id"] == "synthetic-source" for fact in notes)
    text = " ".join(fact["text"] for fact in notes)
    assert scope in text and detail in text and warning in text
    state["reply"] = complete_reply(assessment("Review saved scope.", notes[0]["id"]))
    case_brief.generate(settings, facts)
    prompt = requests[-1][1]["messages"][1]["content"]
    assert scope in prompt and detail in prompt and warning in prompt


@pytest.mark.parametrize("fact_limit,expected_ids,omitted_sources,partial_sources", [
    (8, ["F1", "F2", "F3", "F4", "F5", "F7", "F8", "F10"], 0, 2),
    (6, ["F1", "F2", "F3", "F4", "F5", "F10"], 1, 1),
])
def test_context_selection_counts_partial_multi_fact_sources(model, monkeypatch, fact_limit, expected_ids,
                                                           omitted_sources, partial_sources):
    settings, _, _ = model
    facts = [{"kind": "case", "text": "Synthetic case", "source_id": None}]
    for source, kinds in [("a", ["source", "source_scope", "inventory", "deleted", "deleted"]),
                          ("b", ["source", "source_scope", "inventory"])]:
        facts.extend({"kind": kind, "source_id": source, "run_id": "run-" + source,
                      "text": f"Saved {kind} for {source}."} for kind in kinds)
    facts.append({"kind": "scope", "text": case_brief.SCOPE_NOTE, "source_id": None})
    facts = [dict(fact, id=f"F{index}") for index, fact in enumerate(facts, 1)]
    if omitted_sources:
        facts[7]["text"] = "Long source scope. " * 1000
    monkeypatch.setattr(case_brief, "MAX_FACTS", fact_limit)

    def parts(selected, notice):
        return [{"role": "user", "content": case_brief.build_prompt(selected) + notice}], case_brief.response_schema(selected)

    selected, limits = case_brief.select_facts(settings, facts, parts)
    assert [fact["id"] for fact in selected] == expected_ids
    assert (limits["facts_total"], limits["facts_sent"], limits["facts_omitted"]) == (10, fact_limit, 10 - fact_limit)
    assert limits["sources_omitted"] == omitted_sources
    assert f"{partial_sources} included sources have omitted detail" in limits["notice"]
    assert selected == case_brief.select_facts(settings, facts, parts)[0]


def test_number_tokens_normalize_unicode_without_integer_conversion():
    assert case_brief.numbers("٠٠٩ ９ 2,048 000") == {"9", "2048", "0"}
    assert case_brief.numbers("9" * 10000) == {"9" * 10000}
    assert case_brief.numbers("² Ⅷ") == set()  # These are not decimal digits.


def test_brief_endpoint_hostile_numeric_scope_and_audit_omissions(model, monkeypatch):
    settings, state, _ = model
    settings = Settings(settings.data_dir, ai_context=32768)
    app = create_app(settings, start_workers=False)
    case, _ = saved_source(app.state.store, {"artifacts": [{"path": "/９².txt", "kind": "file"}],
        "coverage": {"status": "complete", "scope": "9" * 10000, "steps": []}})
    get = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/cases/{case_id}/brief"
               and "GET" in route.methods)
    post = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/cases/{case_id}/brief"
                and "POST" in route.methods)
    facts = get(case)["facts"]
    scope = next(fact for fact in facts if fact["kind"] == "source_scope")
    state["reply"] = complete_reply(assessment("Review this saved scope.", scope["id"]))
    monkeypatch.setattr(case_brief, "MAX_FACTS", 4)
    result = post(case)["document"]
    assert result["overall_summary"][0]["verification"] == "unverified"
    assert result["facts_digest"] == case_brief.digest(facts)
    assert result["context"]["facts_sent"] == 4 and result["context"]["facts_omitted"] == 1
    audit = app.state.store.audit(case)[0]["detail"]
    assert "from 4 facts sent; 1 facts omitted" in audit
    assert result["input_digest"][:12] in audit and "Review this saved scope" not in audit


@pytest.mark.parametrize("length", [b"\xb2", b"9" * 10000], ids=["unicode-digit", "long-digits"])
def test_chat_endpoint_hostile_content_length_is_422(tmp_path, length):
    app = create_app(Settings(tmp_path), start_workers=False)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/cases/{case_id}/chat")
    request = Request({"type": "http", "headers": [(b"content-length", length)]})
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint("synthetic", request))
    assert error.value.status_code == 422


def test_full_history_per_field_limits_can_exceed_joint_body_limit(tmp_path):
    value = {"mode": "concepts", "message": "x" * learning.MESSAGE_BYTES, "history": [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * learning.MESSAGE_BYTES}
        for index in range(learning.HISTORY_MESSAGES)]}
    learning.ChatInput.model_validate(value)  # Field limits alone are not an advertised joint capacity.
    body = json.dumps(value).encode()
    assert len(body) > learning.BODY_BYTES
    app = create_app(Settings(tmp_path), start_workers=False)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/cases/{case_id}/chat")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    with pytest.raises(HTTPException, match="16 KiB") as error:
        asyncio.run(endpoint("synthetic", Request({"type": "http", "headers": []}, receive)))
    assert error.value.status_code == 422
