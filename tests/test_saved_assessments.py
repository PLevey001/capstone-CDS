"""Saved JSON contracts using synthetic observations and a scripted, socket-free model."""
import copy
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from cds import case_brief, local_model
from cds.config import Settings
from cds.main import create_app
from cds.store import Store
from tests.model_stub import LOCAL_MODEL, assessment, cite_first_facts, complete_reply

HEADERS = {"X-CDS-Request": "local-ui"}


def seed(store, name="Synthetic case", source="image"):
    case = store.create_case(name, "Synthetic fixture; never original evidence")
    path = store.root / "evidence" / source
    path.write_bytes(b"synthetic")
    store.register(source, case["id"], "sample.img", 9, path, "raw_image", 512)
    job = store.claim()
    store.finish(job, {"sha256": "b" * 64, "coverage": {"status": "partial", "steps": [
        {"id": "inventory", "status": "partial", "label": "Inventory", "detail": "Synthetic limit reached."}]},
        "artifacts": [{"path": "/a.txt", "kind": "file"}, {"path": "/b.txt", "kind": "file"},
                      {"path": "/removed.txt", "kind": "file", "deleted": True},
                      {"path": "/folder", "kind": "directory"}]})
    return case["id"], source, job["run_id"]


@pytest.fixture
def saved_case(tmp_path, monkeypatch):
    calls = []
    state = {"reply": None, "during_generation": lambda: None}

    def call(url, path, payload=None, **kwargs):
        if path == "/api/tags":
            return {"models": [LOCAL_MODEL]}
        if path == "/api/show":
            return {"details": {"format": "gguf"}, "capabilities": ["completion"]}
        if path == "/api/version":
            return {"version": "synthetic-runtime"}
        assert path == "/api/chat"
        calls.append(payload)
        state["during_generation"]()
        return state["reply"] or cite_first_facts(payload)[1]

    monkeypatch.setattr(local_model, "call", call)
    settings = Settings(tmp_path, ai_context=32768)
    app = create_app(settings, start_workers=False)
    with TestClient(app) as client:
        store = app.state.store
        case, source, run = seed(store)
        yield client, store, state, calls, case, source, run


def generate(client, case):
    response = client.post(f"/api/cases/{case}/brief", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["document"]


def test_known_answer_snapshot_and_inference_separation(saved_case):
    client, store, _, calls, case, source, run = saved_case
    document = generate(client, case)
    inventory = next(f for f in document["facts"] if f["kind"] == "inventory")
    # Independent fixture expectation, not a copy of a fact-generation function.
    assert inventory["text"] == ('The inventory of "sample.img" lists 3 file entries and 1 directory; '
                                 '1 file entry is marked deleted.')
    assert inventory["source_id"] == source and inventory["run_id"] == run
    assert inventory["source_sha256"] == "b" * 64
    assert document["inferences"][0]["alternatives"] and document["inferences"][0]["limitations"]
    assert all(item["verification"] == "unverified" for section in case_brief.SECTION_LIMITS for item in document[section])
    assert all("A-" not in fact["id"] for fact in document["facts"])
    assert document["schema_version"] == "cds.case-assessment.v1"
    assert document["input_digest"] == case_brief.digest(store.brief_inputs(case))
    assert document["model_digest"] == LOCAL_MODEL["digest"]
    assert document["runtime_version"] == "synthetic-runtime"
    assert document["model_facts"] == [{"id": f["id"], "text": f["text"]} for f in document["facts"]]
    assert case_brief.build_prompt(document["model_facts"]) in calls[-1]["messages"][1]["content"]


def test_export_byte_stability_history_staleness_restart_and_safe_filename(saved_case):
    client, store, state, _, case, source, run = saved_case
    first = generate(client, case)
    route = f"/api/cases/{case}/briefs/{first['id']}"
    original = client.get(route + "/download")
    assert original.headers["content-type"].startswith("application/json")
    assert json.loads(original.content) == first
    assert hashlib.sha256(original.content).hexdigest() == client.get(route).json()["document_sha256"]
    store.rename_case(case, '../../escape"\r\nInjected: value')
    assert client.get(route).json()["stale"] is True
    assert client.get(route + "/download").content == original.content
    assert "escape" not in original.headers["content-disposition"]
    second = generate(client, case)
    assert second["version"] == 2 and second["id"] != first["id"]
    assert [(v["version"], v["stale"]) for v in client.get(f"/api/cases/{case}/briefs").json()] == [(2, False), (1, True)]
    store.retry(source)
    store.finish(store.claim(), {"artifacts": [{"path": "/new.txt", "kind": "file"}]})
    assert client.get(route + "/download").content == original.content
    assert any(f["run_id"] == run for f in client.get(route).json()["document"]["facts"])
    assert store.detail(source, run)["run"]["id"] == run
    # New app/Store and unavailable AI cannot change the saved bytes.
    state["reply"] = complete_reply("invalid")
    other_app = create_app(Settings(store.root), start_workers=False)
    with TestClient(other_app) as restarted:
        assert restarted.get(route + "/download").content == original.content
        assert restarted.get(route).json()["stale"] is True


def test_ownership_guard_and_case_delete_isolation(saved_case):
    client, store, _, _, case, _, _ = saved_case
    first = generate(client, case)
    other, _, _ = seed(store, "Other", "other-image")
    second = generate(client, other)
    assert client.post(f"/api/cases/{case}/brief").status_code == 403
    assert client.get("/api/cases/missing/briefs").status_code == 404
    for suffix in ("", "/download"):
        assert client.get(f"/api/cases/{other}/briefs/{first['id']}{suffix}").status_code == 404
    assert client.post(f"/api/cases/{other}/chat", headers=HEADERS,
        json={"mode": "case", "message": "Explain", "brief_id": first["id"]}).status_code == 404
    assert client.put(f"/api/cases/{case}/briefs/{first['id']}", headers=HEADERS, json={}).status_code == 405
    assert client.delete(f"/api/cases/{case}", headers=HEADERS).status_code == 200
    assert store.saved_brief(case, first["id"]) is None
    assert store.saved_brief(other, second["id"])["document"] == second


def test_migrate_prebrief_database_and_immutable_rows(saved_case):
    client, store, _, _, case, _, _ = saved_case
    with store.connect() as db:
        db.execute("DROP TABLE case_briefs")
    store.initialize()
    assert store.case_exists(case) and store.saved_briefs(case) == []
    document = generate(client, case)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.connect() as db:
            db.execute("UPDATE case_briefs SET document='{}' WHERE id=?", (document["id"],))
    store.initialize()
    assert store.saved_brief(case, document["id"])["document"] == document


def test_concurrent_inserts_assign_unique_monotonic_versions(saved_case):
    client, store, _, _, case, _, _ = saved_case
    first = generate(client, case)
    sheet = case_brief.fact_sheet(store.brief_inputs(case))
    # API generation is serialized; Store still defends concurrent writers at the transaction boundary.
    with ThreadPoolExecutor(max_workers=4) as pool:
        documents = list(pool.map(lambda _: store.save_brief(sheet, first), range(8)))
    assert sorted(doc["version"] for doc in documents) == list(range(2, 10))
    assert len({doc["id"] for doc in documents}) == 8
    assert len([e for e in store.audit(case) if e["action"] == "case_brief_saved"]) == 9


@pytest.mark.parametrize("change", ["rename", "reanalysis", "import", "delete"])
def test_change_during_generation_never_commits(saved_case, change):
    client, store, state, _, case, source, _ = saved_case
    def mutate():
        if change == "rename":
            store.rename_case(case, "New name")
        elif change == "reanalysis":
            store.retry(source)
            store.finish(store.claim(), {"artifacts": []})
        elif change == "import":
            path = store.root / "new-source"
            path.write_bytes(b"synthetic")
            store.register("new", case, "new", 9, path, "file", 512)
        else:
            store.delete_case(case)
    state["during_generation"] = mutate
    response = client.post(f"/api/cases/{case}/brief", headers=HEADERS)
    assert response.status_code == (404 if change == "delete" else 409)
    assert "No brief was saved" in response.json()["detail"]
    assert not store.saved_briefs(case)


def test_successful_save_does_not_depend_on_postcommit_database_read(saved_case, monkeypatch):
    client, store, _, _, case, _, _ = saved_case
    def unexpected(*args, **kwargs):
        raise RuntimeError("Synthetic postcommit read failure")
    monkeypatch.setattr(store, "saved_brief", unexpected)
    response = client.post(f"/api/cases/{case}/brief", headers=HEADERS)
    assert response.status_code == 200
    document = response.json()["document"]
    assert store.saved_briefs(case)[0]["id"] == document["id"]
    with store.connect() as db:
        serialized = db.execute("SELECT document FROM case_briefs WHERE id=?", (document["id"],)).fetchone()[0]
    assert hashlib.sha256(serialized.encode()).hexdigest() == response.json()["document_sha256"]


def test_audit_failure_rolls_back_insert_and_version(saved_case, monkeypatch):
    client, store, _, _, case, _, _ = saved_case
    sheet = case_brief.fact_sheet(store.brief_inputs(case))
    generated = case_brief.generate(Settings(store.root, ai_context=32768), sheet["facts"])
    original = store.event
    def fail(*args):
        raise RuntimeError("synthetic audit failure")
    monkeypatch.setattr(store, "event", fail)
    with pytest.raises(RuntimeError):
        store.save_brief(sheet, generated)
    assert store.saved_briefs(case) == []
    monkeypatch.setattr(store, "event", original)
    assert store.save_brief(sheet, generated)["version"] == 1


BAD_ITEMS = ["not an object", {"statement": "Mixed refs", "facts": ["F1", "F999"]},
             {"statement": "Wrong type", "facts": ["F1", 1]}, {"statement": "x" * 401, "facts": ["F1"]},
             {"statement": "Invented 999999", "facts": ["F1"]}, {"statement": "", "facts": ["F1"]},
             {"statement": "No refs", "facts": []},
             {"statement": "Review F2.", "facts": ["F1"]},
             {"statement": "The measured scope includes,", "facts": ["F1"]}]


@pytest.mark.parametrize("bad", BAD_ITEMS)
def test_invalid_item_rejects_whole_document_without_a_partial_success(saved_case, bad):
    client, store, state, _, case, _, _ = saved_case
    content = assessment()
    content["observations"] = [bad, {"statement": "Valid", "facts": ["F1"]}]
    state["reply"] = complete_reply(content)
    assert client.post(f"/api/cases/{case}/brief", headers=HEADERS).status_code == 502
    assert store.saved_briefs(case) == []


def test_optional_inferences_and_matching_inline_reference_can_be_saved(saved_case):
    client, _, state, _, case, _, _ = saved_case
    state["reply"] = complete_reply(assessment("Review the recorded case scope (F1).") | {"inferences": []})
    document = generate(client, case)
    assert document["inferences"] == []
    assert document["overall_summary"][0]["text"] == "Review the recorded case scope (F1)."
    assert document["prompt_version"] == "case-assessment.v2"


@pytest.mark.parametrize("bad", ["not JSON", "[]", '{"overall_summary":[],"overall_summary":[]}',
                                 assessment() | {"extra": []}, assessment() | {"overall_summary": []},
                                 assessment() | {"observations": [{"statement": "x", "facts": ["F1"]}] * 7},
                                 assessment() | {"inferences": [{"statement": "Maybe", "facts": ["F1"]}]}])
def test_invalid_schema_never_saved(saved_case, bad):
    client, store, state, _, case, _, _ = saved_case
    state["reply"] = complete_reply(bad)
    assert client.post(f"/api/cases/{case}/brief", headers=HEADERS).status_code == 502
    assert store.saved_briefs(case) == []


def test_incomplete_model_response_no_saved_version(saved_case):
    client, store, state, _, case, _, _ = saved_case
    state["reply"] = complete_reply(assessment()) | {"done_reason": "length"}
    assert client.post(f"/api/cases/{case}/brief", headers=HEADERS).status_code == 502
    assert store.saved_briefs(case) == []
    state["reply"] = None
    assert generate(client, case)["version"] == 1


def test_whole_source_omission_rejected_and_detail_omissions_explicit(saved_case, monkeypatch):
    client, store, _, calls, case, _, _ = saved_case
    monkeypatch.setattr(case_brief, "MAX_FACTS", 2)
    response = client.post(f"/api/cases/{case}/brief", headers=HEADERS)
    assert response.status_code == 422 and "whole evidence sources" in response.json()["detail"]
    assert not calls and store.saved_briefs(case) == []
    monkeypatch.setattr(case_brief, "MAX_FACTS", 5)
    document = generate(client, case)
    sent = {f["id"] for f in document["model_facts"]}
    omitted = [f["id"] for f in document["facts"] if f["id"] not in sent]
    assert document["context"]["omitted_fact_ids"] == omitted and omitted
    assert document["context"]["facts_omitted"] == len(omitted)
    assert document["context"]["sources_omitted"] == 0


def test_selected_brief_chat_separation_staleness_and_budget(saved_case):
    client, store, _, calls, case, _, _ = saved_case
    brief = generate(client, case)
    route = f"/api/cases/{case}/chat"
    body = {"mode": "case", "message": "What does this inference mean?", "brief_id": brief["id"]}
    reply = client.post(route, headers=HEADERS, json=body)
    assert reply.status_code == 200, reply.text
    reply = reply.json()
    assert reply["selected_brief"] == {"id": brief["id"], "version": 1}
    assert reply["answer"][0]["facts"] == ["A-overall_summary-1", "F1"]
    assert all(f["id"].startswith("F") for f in reply["facts"])
    prompt = calls[-1]["messages"][1]["content"]
    assert "prior_UNVERIFIED_assessment_NOT_evidence" in prompt
    for text in brief["inferences"][0]["alternatives"] + brief["inferences"][0]["limitations"]:
        assert text in prompt
    assert client.post(route, headers=HEADERS, json=body | {"mode": "concepts"}).status_code == 422
    count = len(calls)
    store.rename_case(case, "Renamed")
    stale = client.post(route, headers=HEADERS, json=body)
    assert stale.status_code == 409 and "stale" in stale.json()["detail"]
    assert len(calls) == count
    assert client.get(f"/api/cases/{case}/briefs/{brief['id']}/download").status_code == 200
    # No persistence of raw conversations or edits to the selected document.
    assert store.saved_brief(case, brief["id"])["document"] == brief
    assert all("inference mean" not in e["detail"] for e in store.audit(case))


def test_assessment_budget_preserves_all_alternatives_or_refuses(saved_case):
    from cds import learning
    client, store, _, calls, case, _, _ = saved_case
    brief = generate(client, case)
    brief = copy.deepcopy(brief)
    brief["inferences"][0]["alternatives"] = ["Uncertainty. " * 1000]
    count = len(calls)
    with pytest.raises(local_model.ModelError) as error:
        learning.generate(Settings(store.root), learning.ChatInput(mode="case", message="Explain"),
                          case_brief.fact_sheet(store.brief_inputs(case)), brief)
    assert error.value.status == 422 and len(calls) == count


def test_selected_assessment_fits_default_context(saved_case):
    from cds import learning
    client, store, _, _, case, _, _ = saved_case
    brief = generate(client, case)
    result = learning.generate(Settings(store.root), learning.ChatInput(mode="case", message="Explain the assessment"),
                              case_brief.fact_sheet(store.brief_inputs(case)), brief)
    assert result["selected_brief"]["id"] == brief["id"]
    assert result["context"]["input_bytes"] <= result["context"]["input_limit_bytes"]


def test_evaluator_defaults_to_no_calls_and_no_semantic_grade(tmp_path, monkeypatch):
    from scripts.evaluate_local_ai import evaluate
    def unexpected(*args, **kwargs):
        pytest.fail("Default evaluator must never call a model")
    monkeypatch.setattr(local_model, "call", unexpected)
    report = evaluate(Settings(tmp_path))
    assert len(report["scenarios"]) == 6 and report["real_model_opt_in"] is False
    assert all(entry["manual_findings"] is None and entry["manual_status"] == "not_reviewed"
               for entry in report["scenarios"])
    assert all(op["mechanical_status"] == "not_run" for entry in report["scenarios"] for op in entry["operations"])


def test_evaluator_records_scripted_raw_replies_without_claiming_semantic_success(saved_case):
    from scripts.evaluate_local_ai import evaluate
    _, store, _, _, _, _, _ = saved_case
    report = evaluate(Settings(store.root, ai_context=32768), real_model=True)
    assert all(entry["manual_status"] == "not_reviewed" for entry in report["scenarios"])
    for entry in report["scenarios"]:
        for operation in entry["operations"]:
            assert operation["raw_model_replies"] and operation["wall_duration_ms"] >= 0
            assert operation["mechanical_status"] == "accepted_unverified"
            assert operation["result"]["model_digest"] == LOCAL_MODEL["digest"]
