from cds.instance_lock import InstanceLock
from scripts import run


def test_launcher_reuses_healthy_server(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CDS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(run, "server_is_healthy", lambda: True)
    monkeypatch.setattr(run.os, "execv", lambda *args: (_ for _ in ()).throw(AssertionError("Must not launch a second server")))
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
    invoked = []
    monkeypatch.setattr(run.os, "execv", lambda *args: invoked.append(args))
    run.main(tmp_path)
    assert invoked[0][0] == str(python)
    assert "cds.main:app" in invoked[0][1]
