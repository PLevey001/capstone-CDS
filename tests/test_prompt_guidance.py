import hashlib
from pathlib import Path

import pytest
from cds import case_brief, local_model
from cds.config import Settings


def test_prompt_file_is_active_and_hashed():
    text = (Path(case_brief.__file__).parent / 'prompts' / 'case-brief.txt').read_text().strip()
    assert text == case_brief.SYSTEM_PROMPT
    assert hashlib.sha256(text.encode()).hexdigest() == case_brief.PROMPT_SHA256
    assert case_brief.PROMPT_VERSION == 'case-assessment.v3'


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
