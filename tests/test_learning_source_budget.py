"""Whole-case questions must not silently operate on an incomplete source set."""
import pytest

from cds import case_brief, learning, local_model
from cds.config import Settings


def test_omitted_source_rejects_question_before_any_model_call(monkeypatch, tmp_path):
    facts = [
        {"id": "F1", "kind": "case", "text": "Case has two sources.", "source_id": None},
        {"id": "F2", "kind": "source", "text": "First source.", "source_id": "a"},
        {"id": "F3", "kind": "source_scope", "text": "Long scope. " * 2000, "source_id": "a"},
        {"id": "F4", "kind": "source", "text": "Second source failed.", "source_id": "b"},
        {"id": "F5", "kind": "scope", "text": case_brief.SCOPE_NOTE, "source_id": None},
    ]
    def unexpected(*args, **kwargs):
        pytest.fail("An incomplete source set reached model generation")
    monkeypatch.setattr(local_model, "chat", unexpected)
    with pytest.raises(local_model.ModelError, match="omit 2 evidence sources") as failure:
        learning.generate(Settings(tmp_path), learning.ChatInput(mode="case", message="Which source failed?"),
                          {"facts": facts, "facts_digest": case_brief.digest(facts), "provenance": {}})
    assert failure.value.status == 422


def test_invalid_unicode_model_reply_is_controlled_error(monkeypatch, tmp_path):
    settings = Settings(tmp_path)
    monkeypatch.setattr(local_model, "discover", lambda *args: None)
    monkeypatch.setattr(local_model, "call", lambda *args, **kwargs: {
        "model": settings.ai_model, "done": True, "done_reason": "stop",
        "message": {"role": "assistant", "content": "\ud800"},
    })
    with pytest.raises(local_model.ModelError, match="invalid Unicode") as failure:
        local_model.chat(settings, [], {})
    assert failure.value.status == 502
