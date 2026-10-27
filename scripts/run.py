"""Start the local app using this project's virtual environment."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8000"
READINESS_SECONDS = 10

# This launcher uses only the standard library until it execs the virtualenv.
sys.path.insert(0, str(ROOT))
from cds.config import Settings
from cds.instance_lock import InstanceAlreadyRunning, InstanceLock
from cds import local_model

ENV_KEYS = {
    "CDS_DATA_DIR", "CDS_WORKERS", "CDS_MAX_UPLOAD_BYTES", "CDS_MAX_ARTIFACTS", "CDS_TOOL_TIMEOUT",
    "CDS_AI_URL", "CDS_AI_MODEL", "CDS_AI_TIMEOUT", "CDS_AI_CONTEXT", "CDS_AI_SERVICE",
}


def load_config(root):
    """Small literal KEY=value format, not a shell or general dotenv interpreter."""
    path = root / ".env"
    if not path.exists():
        return
    try:
        if path.stat().st_size > 65536:
            raise ValueError(".env exceeds 64 KiB")
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError("Cannot read .env as UTF-8") from error
    values = {}
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in ENV_KEYS or key in values:
            raise ValueError(f".env line {number}: malformed, unknown or duplicate key")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f".env line {number}: unmatched quote wrapper")
            value = value[1:-1]
        elif value.endswith(("'", '"')):
            raise ValueError(f".env line {number}: unmatched quote wrapper")
        if any(not char.isprintable() for char in value):
            raise ValueError(f".env line {number}: nonprintable value")
        values[key] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)  # Shell wins, including an explicitly empty value.


def service_name():
    unit = os.environ.get("CDS_AI_SERVICE", "")
    if unit and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}\.service", unit):
        raise ValueError("CDS_AI_SERVICE must name one explicit user .service unit")
    return unit


def prepare_ai(settings, unit):
    """Optional explicit user service; never install, pull, spawn Ollama, or stop it."""
    warning = None
    service_started = False
    if unit:
        if sys.platform != "linux":
            warning = "Configured user service startup is unavailable on this platform. Start the local runtime yourself."
        else:
            try:
                result = subprocess.run(["systemctl", "--user", "start", unit], timeout=10,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        check=False)
                if result.returncode:
                    warning = "Could not start the configured user service. Check it with systemctl --user status."
                else:
                    service_started = True
            except (OSError, subprocess.TimeoutExpired):
                warning = "User service startup failed or timed out. Start the local runtime yourself."
    if service_started:
        print(f"Configured user service started; waiting up to {READINESS_SECONDS} seconds for local AI readiness…", flush=True)
        deadline = time.monotonic() + READINESS_SECONDS
        while True:
            try:
                # A successful systemctl start can precede the HTTP listener.
                local_model.call(settings.ai_url, "/api/tags",
                                 deadline=min(deadline, time.monotonic() + local_model.STATUS_TIMEOUT))
            except local_model.ModelError as error:
                if error.status not in (503, 504):
                    availability = {"available": False, "reason": str(error)}
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    availability = {"available": False, "reason":
                        "The configured service was started, but its API is not ready yet. "
                        "Check systemctl --user status for that unit, then use Check again in the interface."}
                    break
                time.sleep(min(0.25, remaining))
            else:
                availability = local_model.status(settings, deadline=deadline)
                break
    else:
        availability = local_model.status(settings)  # Direct loopback discovery; no inference or proxy.
    print(f"CDS: {URL}/\nConfigured local model: {settings.ai_model}")
    if warning:
        print(warning)
    print("Local model available. Review AI status in the interface." if availability["available"] else
          f"AI unavailable: {availability['reason']}\nThe app will run; saved briefs and facts remain accessible.", flush=True)


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
    try:
        load_config(root)
        unit = service_name()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    try:
        settings = Settings.from_env()
    except ValueError as error:
        # Settings' named errors contain only controlled guidance; int conversion errors can echo values.
        message = str(error) if str(error).startswith("CDS_") else (
            "Invalid CDS settings. Check .env or the shell environment against .env.example.")
        print(message, file=sys.stderr)
        return 1
    lock = InstanceLock(settings.data_dir)
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
    prepare_ai(settings, unit)
    os.execv(str(python), [str(python), "-m", "uvicorn", "cds.main:app", "--host", "127.0.0.1", "--port", "8000", "--no-access-log"])


if __name__ == "__main__":
    raise SystemExit(main())
