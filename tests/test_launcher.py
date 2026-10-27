import os
import subprocess
from types import SimpleNamespace

import pytest

from cds.config import Settings
from cds.instance_lock import InstanceLock
from scripts import run


def test_launcher_reuses_healthy_server(tmp_path, monkeypatch, capsys):
    def unexpected_launch(*args):
        raise AssertionError("Must not launch a second server")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CDS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(run, "server_is_healthy", lambda: True)
    monkeypatch.setattr(run.os, "execv", unexpected_launch)
    lock = InstanceLock(tmp_path / "data")
    lock.acquire()
    try:
        assert run.main(tmp_path) == 0
        assert "CDS is already running" in capsys.readouterr().out
        # The launcher must not remove or release the existing process's lock.
        assert (tmp_path / "data/instance.lock").exists()
    finally:
        lock.release()


def test_launcher_reports_busy_but_unavailable_server(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CDS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(run, "server_is_healthy", lambda: False)
    lock = InstanceLock(tmp_path / "data")
    lock.acquire()
    try:
        assert run.main(tmp_path) == 1
        assert "not ready" in capsys.readouterr().err
    finally:
        lock.release()


def test_launcher_starts_when_old_lock_file_is_unlocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CDS_DATA_DIR", str(tmp_path / "data"))
    previous = InstanceLock(tmp_path / "data")
    previous.acquire()
    previous.release()
    python = tmp_path / ".venv" / ("Scripts/python.exe" if run.os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    frontend = tmp_path / "frontend/dist/index.html"
    frontend.parent.mkdir(parents=True)
    frontend.touch()
    monkeypatch.setattr(run, "prepare_ai", lambda *args: None)
    invoked = []
    monkeypatch.setattr(run.os, "execv", lambda *args: invoked.append(args))
    run.main(tmp_path)
    assert invoked[0][0] == str(python)
    assert "cds.main:app" in invoked[0][1]




@pytest.fixture
def clean_config_env(monkeypatch):
    for key in run.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_literal_config_quotes_shell_precedence_and_no_evaluation(tmp_path, monkeypatch, clean_config_env):
    marker = tmp_path / "must-not-exist"
    (tmp_path / ".env").write_text(
        '# Comment\n\nCDS_DATA_DIR="relative data"\nCDS_AI_CONTEXT=32768\n'
        "CDS_AI_MODEL='from-file'\nCDS_AI_TIMEOUT=300\n"
        f'CDS_AI_SERVICE="$(touch {marker})"\n')
    monkeypatch.setenv("CDS_AI_MODEL", "from-shell")
    monkeypatch.setenv("HOME", "unchanged-home")
    monkeypatch.setenv("PATH", "unchanged-path")
    # Track env mutations from the function under test for fixture cleanup.
    for key in run.ENV_KEYS - {"CDS_AI_MODEL"}:
        monkeypatch.delenv(key, raising=False)
    run.load_config(tmp_path)
    assert os.environ["CDS_AI_MODEL"] == "from-shell"
    assert os.environ["CDS_DATA_DIR"] == "relative data"
    assert os.environ["CDS_AI_SERVICE"] == f"$(touch {marker})"
    assert os.environ["HOME"] == "unchanged-home" and os.environ["PATH"] == "unchanged-path"
    assert not marker.exists()
    with pytest.raises(ValueError, match="explicit user"):
        run.service_name()


@pytest.mark.parametrize("line", ["HOME=secret", "PATH=secret", "CDS_UNKNOWN=secret", "export CDS_AI_MODEL=secret",
                                 "CDS_AI_MODEL", 'CDS_AI_MODEL="secret', "CDS_AI_MODEL=secret'",
                                 "CDS_AI_MODEL=secret\x00", "CDS_AI_MODEL=secret\nCDS_AI_MODEL=again"])
def test_malformed_forbidden_and_duplicate_config_reports_line_without_value(tmp_path, clean_config_env, line):
    (tmp_path / ".env").write_text("# comment\n" + line)
    with pytest.raises(ValueError, match=r".env line [23]:") as error:
        run.load_config(tmp_path)
    assert "secret" not in str(error.value)
    assert "CDS_AI_MODEL" not in os.environ  # No partial application after parse errors.


def test_absent_config_preserves_environment(tmp_path, clean_config_env, monkeypatch):
    monkeypatch.setenv("CDS_AI_CONTEXT", "32768")
    before = dict(os.environ)
    run.load_config(tmp_path)
    assert dict(os.environ) == before
    assert Settings.from_env().ai_context == 32768


@pytest.mark.parametrize("unit", ["*.service", "../evil.service", "--help.service", "a.service b.service", "ollama", "a;reboot.service"])
def test_invalid_service_never_executes(monkeypatch, unit):
    monkeypatch.setenv("CDS_AI_SERVICE", unit)
    with pytest.raises(ValueError):
        run.service_name()


@pytest.mark.parametrize("failure", [None, "exit", "timeout", "missing"])
def test_service_start_is_explicit_bounded_and_failure_still_probes(tmp_path, monkeypatch, capsys, failure):
    calls = []
    monkeypatch.setattr(run.sys, "platform", "linux")
    def start(argv, **kwargs):
        calls.append((argv, kwargs))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 10)
        if failure == "missing":
            raise FileNotFoundError()
        return SimpleNamespace(returncode=1 if failure == "exit" else 0)
    monkeypatch.setattr(run.subprocess, "run", start)
    monkeypatch.setattr(run.local_model, "status", lambda settings, **kwargs: {
        "available": False, "reason": "Synthetic model is not installed."})
    monkeypatch.setattr(run.local_model, "call", lambda *args, **kwargs: {"models": []})
    run.prepare_ai(Settings(tmp_path), "cds-ollama.service")
    assert len(calls) == 1 and calls[0][0] == ["systemctl", "--user", "start", "cds-ollama.service"]
    assert calls[0][1]["timeout"] == 10
    assert calls[0][1]["stdout"] == subprocess.DEVNULL and "shell" not in calls[0][1]
    out = capsys.readouterr().out
    assert "app will run" in out and "http://127.0.0.1:8000/" in out
    assert "llama3.2:3b" in out and "model is not installed" in out


def test_no_service_or_unsupported_platform_does_not_run_systemctl(tmp_path, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("No process should be started")
    monkeypatch.setattr(run.subprocess, "run", unexpected)
    monkeypatch.setattr(run.local_model, "status", lambda settings: {"available": True})
    run.prepare_ai(Settings(tmp_path), "")
    monkeypatch.setattr(run.sys, "platform", "darwin")
    run.prepare_ai(Settings(tmp_path), "cds-ollama.service")
    assert "unavailable on this platform" in capsys.readouterr().out


def test_missing_model_probe_does_not_generate_or_pull(tmp_path, monkeypatch, capsys):
    calls = []
    def missing(url, path, **kwargs):
        calls.append((path, kwargs))
        return {"models": []}
    monkeypatch.setattr(run.local_model, "call", missing)
    run.prepare_ai(Settings(tmp_path), "")
    assert [path for path, _ in calls] == ["/api/tags"]
    assert "deadline" in calls[0][1]
    out = capsys.readouterr().out
    assert "not installed" in out and "ollama pull llama3.2:3b" in out and "app will run" in out


def test_normal_launch_loads_repo_config_before_settings_and_exec(tmp_path, monkeypatch, clean_config_env):
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    index = tmp_path / "frontend/dist/index.html"
    index.parent.mkdir(parents=True)
    index.touch()
    (tmp_path / ".env").write_text("CDS_DATA_DIR=local-data\nCDS_AI_CONTEXT=32768\nCDS_AI_SERVICE=cds-ollama.service\n")
    observed = []
    monkeypatch.setattr(run, "prepare_ai", lambda settings, unit: observed.append((settings, unit)))
    monkeypatch.setattr(run.os, "execv", lambda *args: observed.append(args))
    monkeypatch.chdir(tmp_path)
    run.main(tmp_path)
    settings, unit = observed[0]
    assert settings.data_dir == tmp_path / "local-data" and settings.ai_context == 32768
    assert unit == "cds-ollama.service" and observed[1][0] == str(python)


def test_bad_config_exits_before_creating_lock(tmp_path, monkeypatch, clean_config_env, capsys):
    (tmp_path / ".env").write_text("PATH=secret-value\n")
    monkeypatch.chdir(tmp_path)
    assert run.main(tmp_path) == 1
    assert not (tmp_path / "data").exists()
    error = capsys.readouterr().err
    assert "line 1" in error and "secret-value" not in error


@pytest.fixture
def cold_runtime(monkeypatch):
    from tests.model_stub import LOCAL_MODEL
    clock = {"now": 100.0, "delays": [], "calls": [], "failures": 2, "models": [LOCAL_MODEL]}
    monkeypatch.setattr(run.sys, "platform", "linux")
    monkeypatch.setattr(run.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(run.time, "monotonic", lambda: clock["now"])
    def sleep(delay):
        clock["delays"].append(delay)
        clock["now"] += delay
    monkeypatch.setattr(run.time, "sleep", sleep)
    def call(url, path, payload=None, **kwargs):
        clock["calls"].append((path, kwargs["deadline"]))
        if clock["failures"]:
            clock["failures"] -= 1
            raise run.local_model.ModelError(503, "Synthetic listener not ready")
        if path == "/api/tags":
            return {"models": clock["models"]}
        assert path == "/api/show"  # No inference, install, pull or stop operation.
        return {"details": {"format": "gguf"}, "capabilities": ["completion"]}
    monkeypatch.setattr(run.local_model, "call", call)
    return clock


def test_successful_service_start_waits_for_listener_and_then_checks_model(tmp_path, cold_runtime, capsys):
    run.prepare_ai(Settings(tmp_path), "cds-ollama.service")
    assert len(cold_runtime["delays"]) == 2
    assert [path for path, _ in cold_runtime["calls"]] == ["/api/tags"] * 4 + ["/api/show"]
    assert all(deadline <= 100 + run.READINESS_SECONDS for _, deadline in cold_runtime["calls"])
    out = capsys.readouterr().out
    assert "waiting up to 10 seconds" in out and "Local model available" in out
    assert "Start Ollama" not in out and "AI unavailable" not in out


def test_cold_readiness_deadline_is_shared_and_exhaustion_does_not_claim_service_is_stopped(tmp_path, cold_runtime, monkeypatch, capsys):
    def timeout(url, path, **kwargs):
        cold_runtime["calls"].append((path, kwargs["deadline"]))
        cold_runtime["now"] = kwargs["deadline"]
        raise run.local_model.ModelError(504, "Synthetic probe timeout")
    monkeypatch.setattr(run.local_model, "call", timeout)
    run.prepare_ai(Settings(tmp_path), "cds-ollama.service")
    assert cold_runtime["now"] == 100 + run.READINESS_SECONDS
    assert all(deadline <= cold_runtime["now"] for _, deadline in cold_runtime["calls"])
    assert all(0 < delay <= 0.25 for delay in cold_runtime["delays"])
    out = capsys.readouterr().out
    assert "service was started" in out and "Check again" in out and "app will run" in out
    assert "Start Ollama" not in out


def test_ready_service_with_missing_model_reports_install_guidance_without_retrying(tmp_path, cold_runtime, capsys):
    cold_runtime.update(failures=0, models=[])
    run.prepare_ai(Settings(tmp_path), "cds-ollama.service")
    assert [path for path, _ in cold_runtime["calls"]] == ["/api/tags", "/api/tags"]
    assert cold_runtime["delays"] == []
    out = capsys.readouterr().out
    assert "not installed" in out and "ollama pull" in out and "app will run" in out


def test_readiness_does_not_retry_malformed_api_reply(tmp_path, cold_runtime, monkeypatch, capsys):
    def malformed(url, path, **kwargs):
        cold_runtime["calls"].append((path, kwargs["deadline"]))
        raise run.local_model.ModelError(502, "Synthetic invalid JSON reply")
    monkeypatch.setattr(run.local_model, "call", malformed)
    run.prepare_ai(Settings(tmp_path), "cds-ollama.service")
    assert len(cold_runtime["calls"]) == 1 and cold_runtime["delays"] == []
    out = capsys.readouterr().out
    assert "invalid JSON" in out and "app will run" in out
