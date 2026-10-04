"""Disposable backend for Playwright. Never run against a user's workspace."""
from pathlib import Path
import tempfile
from uuid import uuid4

import uvicorn
from fastapi.responses import FileResponse, HTMLResponse

from cds.analysis import analyze
from cds.config import Settings
from cds.main import create_app
from scripts.make_browser_demo import make_chrome_history


def build_app(root):
    app = create_app(Settings(root), start_workers=False)
    store = app.state.store

    @app.get("/test/chrome-file")
    def chrome_file():
        path = root / "work" / f"{uuid4()}.sqlite"
        make_chrome_history(path, count=120)
        return FileResponse(path)

    @app.post("/test/process")
    def process():
        job = store.claim()
        result = analyze(job, {"tool_timeout": 5, "max_artifacts": 50000}, str(root / "work"))
        store.finish(job, result)
        return {"run": job["run_id"]}

    @app.get("/test/visited/{name}")
    def visited(name: str):
        return HTMLResponse("<title>Browser calibration page</title><p>Synthetic local visit</p>")

    @app.post("/test/seed")
    def seed():
        case = store.create_case(f"Timeline {uuid4().hex[:8]}", "Synthetic browser fixture")
        other = store.create_case(f"Empty {uuid4().hex[:8]}", "Case-switch fixture")
        source = str(uuid4())
        path = root / "evidence" / source
        path.write_bytes(b"Synthetic image; file extraction is not part of these tests.")
        store.register(source, case["id"], "fixture.img", path.stat().st_size, path, "raw_image", 512)
        job = store.claim()
        # Reverse insertion order puts the earliest timestamp outside the first artifact page.
        store.finish(job, {"artifacts": [
            {"path": f"/file-{i:03}.txt", "kind": "file", "metadata_address": str(i + 2),
             "details": {"timestamps_unix": {"created": 1700000000 + i * 60}}}
            for i in reversed(range(205))
        ]})
        return {"case": case, "other": other, "source": source, "run": job["run_id"]}

    @app.post("/test/finish/{source}")
    def finish(source: str):
        store.retry(source)
        job = store.claim()
        store.finish(job, {"artifacts": [{"path": "/new-result.txt", "kind": "file", "details": {
            "timestamps_unix": {"created": 1700000500}}}]})
        return {"run": job["run_id"]}

    return app


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cds-browser-tests-") as directory:
        uvicorn.run(build_app(Path(directory)), host="127.0.0.1", port=8765, log_level="warning")
