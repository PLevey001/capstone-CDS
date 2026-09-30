import os
import sys

import pytest

import cds.analysis as analysis


def command(script):
    return [sys.executable, "-c", script]


@pytest.mark.parametrize("capture", [analysis._capture, analysis.run_tool])
@pytest.mark.parametrize("fd", [1, 2])
def test_tool_output_limits_apply_to_both_streams(capture, fd):
    with pytest.raises(analysis.ToolLimitError) as caught:
        capture(command(f"import os; os.write({fd}, b'x' * 2048)"), 5, 64)
    assert caught.value.reason == "tool_output_limit"


def test_tool_handles_binary_output_and_concurrent_diagnostics():
    script = """
import os
import threading
thread = threading.Thread(target=lambda: os.write(2, b'w' * 60000))
thread.start()
os.write(1, b'\\x00\\xff' * 60000)
thread.join()
"""
    code, data, diagnostic = analysis._capture(command(script), 5, 256 * 1024)
    assert code == 0
    assert data == b"\x00\xff" * 60000
    assert diagnostic == "w" * 4096


def test_tool_text_wrapper_decodes_invalid_utf8():
    assert analysis.run_tool(command("import os; os.write(1, b'\\xff')"), 5) == (0, "\ufffd", "")


@pytest.mark.parametrize("capture", [analysis._capture, analysis.run_tool])
def test_combined_output_cannot_exceed_budget(capture):
    with pytest.raises(analysis.ToolLimitError) as caught:
        capture(command("import os; os.write(1, b'x' * 32); os.write(2, b'y' * 33)"), 5, 64)
    assert caught.value.reason == "tool_output_limit"


def test_stderr_has_its_own_ceiling():
    with pytest.raises(analysis.ToolLimitError) as caught:
        analysis._capture(command("import os; os.write(2, b'x' * 70000)"), 5, 1024 * 1024)
    assert caught.value.reason == "tool_output_limit"


@pytest.mark.parametrize("size", [0, 64])
def test_tool_allows_output_at_exact_limit(size):
    assert analysis._capture(command(f"import os; os.write(1, b'x' * {size})"), 5, size) == (0, b"x" * size, "")


@pytest.mark.parametrize("failure", ["timeout", "closed_pipes", "stdout", "stderr"])
def test_tool_limit_kills_and_reaps_child(tmp_path, failure):
    pid_file = tmp_path / "child.pid"
    script = f"""
import os
import pathlib
import time
pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))
if {failure!r} == 'stdout':
    os.write(1, b'x' * 2048)
if {failure!r} == 'stderr':
    os.write(2, b'x' * 2048)
if {failure!r} == 'closed_pipes':
    os.close(1)
    os.close(2)
time.sleep(20)
"""
    with pytest.raises(analysis.ToolLimitError) as caught:
        analysis._capture(command(script), 1, 64)
    assert caught.value.reason == ("tool_timeout" if failure in {"timeout", "closed_pipes"} else "tool_output_limit")
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.fixture
def image_target():
    return {"kind": "file", "source_kind": "raw_image", "source_path": "fixture.img",
            "sector_size": 512, "partition_offset": 2048, "metadata_address": "12-128-1",
            "deleted": False, "run_id": "current", "result_run_id": "current"}


@pytest.mark.parametrize("data", [b"", b"partial bytes"])
def test_extraction_rejects_unsuccessful_tool_output(monkeypatch, image_target, data):
    monkeypatch.setattr(analysis.shutil, "which", lambda name: "/usr/bin/icat")
    monkeypatch.setattr(analysis, "_capture", lambda *args: (1, data, "Unreadable file"))
    with pytest.raises(ValueError, match="Unreadable file"):
        analysis.extract_artifact(image_target)


@pytest.mark.parametrize("deleted", [False, True])
def test_extraction_allows_empty_file_and_requests_deleted_recovery(monkeypatch, image_target, deleted):
    commands = []

    def capture(args, timeout, max_bytes):
        commands.append(args)
        return 0, b"", ""

    monkeypatch.setattr(analysis.shutil, "which", lambda name: "/usr/bin/icat")
    monkeypatch.setattr(analysis, "_capture", capture)
    image_target["deleted"] = deleted
    assert analysis.extract_artifact(image_target) == b""
    assert ("-r" in commands[0]) == deleted
    assert commands[0][-2:] == ["fixture.img", "12-128-1"]


def test_extraction_reports_missing_tool_and_source(monkeypatch, image_target):
    monkeypatch.setattr(analysis.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="requires The Sleuth Kit"):
        analysis.extract_artifact(image_target)
    image_target["source_kind"] = "file"
    with pytest.raises(ValueError, match="no longer available"):
        analysis.extract_artifact(image_target)
