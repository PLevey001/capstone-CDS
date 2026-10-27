"""A larger local context must not be defeated by the old 60-fact ceiling."""
import pytest
from cds import case_brief, learning, local_model
from cds.config import Settings


def test_many_source_scope_notes_fit_large_context_without_dropping_sources(tmp_path, monkeypatch):
    facts = [{"id": "F1", "kind": "case", "text": "Synthetic case with 14 sources.", "source_id": None}]
    for source in range(14):
        for note in range(7):
            facts.append({"id": f"F{len(facts)+1}", "kind": "source" if note == 0 else "source_scope",
                          "source_id": f"s{source}", "text": f"Source {source}: " + "Saved scope limitation. " * 6})
    facts.append({"id": f"F{len(facts)+1}", "kind": "scope", "text": case_brief.SCOPE_NOTE, "source_id": None})
    calls = []
    def reply(settings, messages, schema):
        calls.append(messages)
        assert len(local_model.encoded({'messages': messages, 'format': schema})) <= local_model.input_limit(settings)
        return '{"answer":[],"cannot_answer":true}', {"context": {}}
    monkeypatch.setattr(local_model, "chat", reply)
    sheet = {"facts": facts, "facts_digest": case_brief.digest(facts), "provenance": {}}
    question = learning.ChatInput(mode="case", message="What is the source scope?")
    with pytest.raises(local_model.ModelError, match="omit"):
        learning.generate(Settings(tmp_path), question, sheet)
    assert not calls
    result = learning.generate(Settings(tmp_path, ai_context=32768), question, sheet)
    assert result['context']['sources_omitted'] == 0
    assert result['context']['facts_sent'] == len(facts) == 100
    assert len(calls) == 1
