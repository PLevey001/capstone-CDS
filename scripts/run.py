"""Start the local app using this project's virtual environment."""
import json
import os
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8000"

# This launcher uses only the standard library until it execs the virtualenv.
sys.path.insert(0, str(ROOT))
from cds.config import Settings
from cds.instance_lock import InstanceAlreadyRunning, InstanceLock


def server_is_healthy():
    try:
        # Always check loopback directly, even when the shell has a proxy set.
        with build_opener(ProxyHandler({})).open(f"{URL}/api/health", timeout=2) as response:
            health = json.load(response)
        return (isinstance(health, dict) and health.get("status") == "ok"
                and health.get("coordinator_running") is True
                and isinstance(health.get("sleuthkit_available"), bool))
    except (OSError, URLError, ValueError):
        return False


def main(root=ROOT):
    os.chdir(root)
    lock = InstanceLock(Settings.from_env().data_dir)
    try:
        lock.acquire()
    except InstanceAlreadyRunning:
        if server_is_healthy():
            print(f"CDS is already running.\nOpen {URL}/ in your browser.")
            return 0
        print("Another CDS process is using this workspace, but the server at "
              f"{URL}/ is not ready.\nIt may be starting or stopping. Wait briefly and try again. "
              "If it persists, check the original server terminal.", file=sys.stderr)
        return 1
    finally:
        lock.release()

    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        raise SystemExit("Create .venv and install requirements first. See README.md.")
    if not (root / "frontend/dist/index.html").exists():
        raise SystemExit("Build the interface first: cd frontend && npm ci && npm run build")
    os.execv(str(python), [str(python), "-m", "uvicorn", "cds.main:app", "--host", "127.0.0.1", "--port", "8000", "--no-access-log"])


if __name__ == "__main__":
    raise SystemExit(main())
