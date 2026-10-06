"""Disposable backend for Playwright. Never run against a user's workspace."""
import json
from pathlib import Path
import re
import tempfile
from uuid import uuid4

import uvicorn
from fastapi.responses import FileResponse, HTMLResponse

from cds.analysis import analyze
from cds.config import Settings
from cds.main import create_app
from scripts.make_browser_demo import make_chrome_history, make_firefox_history
from tests.model_stub import LOCAL_MODEL, ModelStub, cite_first_facts


def flagged_brief(payload):
    """Scripted wording with one wrong number and one statement that cites nothing."""
    facts = dict(re.findall(r"^(F\d+): (.*)$", payload["messages"][-1]["content"], re.MULTILINE))
    inventory = next(fact for fact, text in facts.items() if text.startswith("The inventory of"))
    coverage = next(fact for fact, text in facts.items() if text.startswith("Coverage for"))
    content = {"overview": [{"statement": "The case holds one analyzed disk image.", "facts": ["F1", "F2"]},
                            {"statement": "The image lists 999 file entries.", "facts": [inventory]},
                            {"statement": "Someone hid these files on purpose.", "facts": []}],
               "review": [{"statement": "Coverage was not recorded, so check what was examined.", "facts": [coverage]}]}
    return 200, {"message": {"role": "assistant", "content": json.dumps(content)}}


def build_app(root, model=None):
    settings = Settings(root, ai_url=model.url) if model else Settings(root)
    app = create_app(settings, start_workers=False)
    store = app.state.store

    @app.post("/test/model/{mode}")
    def model_mode(mode: str):
        # The model server is a scripted stub; these modes are the states the interface must explain.
        model.models = [] if mode == "missing" else [LOCAL_MODEL]
        model.reply = flagged_brief if mode == "flagged" else cite_first_facts
        return {"mode": mode}

    @app.get("/test/chrome-file")
    def chrome_file():
        path = root / "work" / f"{uuid4()}.sqlite"
        make_chrome_history(path, count=120)
        return FileResponse(path)

    @app.get("/test/firefox-file")
    def firefox_file():
        path = root / "work" / f"{uuid4()}.sqlite"
        make_firefox_history(path, count=120)
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
    with tempfile.TemporaryDirectory(prefix="cds-browser-tests-") as directory, ModelStub() as stub:
        uvicorn.run(build_app(Path(directory), stub), host="127.0.0.1", port=8765, log_level="warning")
