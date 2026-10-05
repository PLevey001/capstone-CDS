"""Repeat Stage 10's five-source API benchmark and real worker limit probe.

The query fixture is synthetic saved data (not 50,000 extracted image events).
The separate worker probes actually parse a browser database at default limits.
"""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import os
import resource
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cds.analysis import PARSER_VERSION, analyze
from cds.config import CONTENT_LIMITS, Settings
from cds.main import create_app
from scripts.make_browser_demo import make_chrome_history
from tests.filesystem_fixtures import run


def worker_probe(root):
    # One fresh process keeps worker/parser peak RSS separate from API seeding.
    import cds.analysis as analysis

    path = make_chrome_history(root / "limit.sqlite", count=10001)
    original_size = path.stat().st_size
    work = root / "work"
    work.mkdir()
    largest_copy = 0
    capture = analysis._capture

    def measure_copy(args, timeout, max_bytes):
        nonlocal largest_copy
        # At subprocess launch all evidence copies are present; standalone
        # immutable parsing cannot grow them or create SQLite journal files.
        largest_copy = max(largest_copy, sum(file.stat().st_size for folder in work.glob("content-*")
                                            for file in folder.rglob("*") if file.is_file()))
        return capture(args, timeout, max_bytes)

    analysis._capture = measure_copy
    rows = []
    for scenario in ("record_count", "payload", "input_at_limit", "input_over_limit"):
        if scenario == "payload":
            # sqlite3's context manager commits the transaction but does not close
            # the connection; close it so the file handle is released here.
            db = sqlite3.connect(path)
            try:
                with db:
                    db.execute("UPDATE urls SET title=?", ("x" * CONTENT_LIMITS["record_text_chars"],))
            finally:
                db.close()
        if scenario == "input_at_limit":
            with path.open("ab") as output:
                output.truncate(CONTENT_LIMITS["content_file_bytes"])
        if scenario == "input_over_limit":
            with path.open("ab") as output:
                output.write(b"x")
        job = {"id": "probe", "run_id": scenario, "name": path.name, "source_path": str(path),
               "size": path.stat().st_size, "kind": "file", "sector_size": 512,
               "imported_at": "2026-01-01T00:00:00Z"}
        largest_copy = 0
        started = time.perf_counter()
        result = analyze(job, {"tool_timeout": 90, "max_artifacts": 20000}, str(work))
        elapsed = time.perf_counter() - started
        step = result["coverage"]["steps"][-1]
        reason = "record_limit" if scenario == "record_count" else "content_file_limit" if scenario == "input_over_limit" else "record_payload_limit"
        assert step["reason"] == reason, step
        assert not list(work.glob("content-*"))
        rows.append({"scenario": scenario, "input_bytes": path.stat().st_size, "seconds": elapsed,
                     "saved_records": len(result["records"]), "reason": step["reason"],
                     "temporary_copy_peak_bytes": largest_copy, "temporary_copy_remaining_bytes": 0})
        del result
    return {"initial_fixture_bytes": original_size, "limits": CONTENT_LIMITS, "probes": rows,
            "worker_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "parser_child_peak_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}


def benchmark(root):
    from fastapi.testclient import TestClient

    app = create_app(Settings(root / "workspace"), start_workers=False)
    with TestClient(app) as client:
        store = app.state.store
        case = store.create_case("Stage 10 performance", "Synthetic saved query load, not an image parser benchmark")
        # Five sources, each with 2,500 files x four MAC times and 2,000
        # records cycling through the four timeline families' record kinds.
        for source_index in range(5):
            path = store.root / "evidence" / str(source_index)
            path.write_bytes(b"synthetic query fixture\n")
            store.register(str(source_index), case["id"], f"load-{source_index}.img", path.stat().st_size, path, "raw_image", 512)
            artifacts = [{"key": str(index), "path": f"/folder-{index % 10}/file-{index}.txt", "kind": "file",
                          "deleted": index % 3 == 0, "metadata_address": str(index + 10), "partition_offset": 2048,
                          "details": {"timestamps_unix": {kind: 1700000000 + index * 4 + shift
                                      for shift, kind in enumerate(("created", "modified", "accessed", "metadata_changed"))}}}
                         for index in range(2500)]
            records = []
            for index in range(2000):
                kind, parser = (("browser_visit", "chrome-history/1"), ("browser_visit", "firefox-history/1"),
                                ("registry_key", "windows-registry/1"), ("windows_event", "windows-event-log/1"))[index % 4]
                records.append({"artifact_key": str(index), "kind": kind, "parser": parser, "source_key": str(index),
                                "event_time_us": (1700000000 + index * 5) * 1000000 + 123456,
                                "summary": f"Synthetic {kind} {index}", "details": {"fixture": True}})
            assert store.finish(store.claim(), {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                                "artifacts": artifacts, "records": records})
        route = f"/api/cases/{case['id']}/timeline"
        assert client.get(route).json()["total"] == 60000
        measurements = {}
        for label, params in (("first", {}), ("filtered_first", {"start": "2023-11-14T23:00:00Z", "end": "2023-11-15T00:00:00Z"}),
                              ("later", {"offset": 30000})):
            client.get(route, params=params)  # warm request outside measured samples
            samples = []
            for _ in range(7):
                started = time.perf_counter()
                response = client.get(route, params=params)
                samples.append(time.perf_counter() - started)
                assert response.status_code == 200
            data = response.json()
            assert len(data["events"]) == 100
            measurements[label] = {"seconds": samples, "median_seconds": statistics.median(samples),
                                   "max_seconds": max(samples), "response_bytes": len(response.content),
                                   "returned_events": len(data["events"]), "matching_events": data["total"]}
        db_bytes = sum(path.stat().st_size for path in store.root.glob("cds.sqlite3*"))
    probe_root = root / "worker-probe"
    probe_root.mkdir()
    child = subprocess.run([sys.executable, __file__, "--worker-probe", str(probe_root)], capture_output=True, text=True, check=True)
    cpu = next(line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name"))
    memory = next(line for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:"))
    return {"source_count": 5, "file_artifacts": 12500, "filesystem_events": 50000, "parsed_records": 10000,
            "fixture_database_bytes": db_bytes, "source_placeholder_bytes": 5 * len(b"synthetic query fixture\n"),
            "fixture_scope": "Synthetic saved query data; separate real standalone parser probes below.",
            "hardware": {"cpu": cpu, "memory": memory, "logical_cpus": os.cpu_count(),
                         "platform": platform.platform()},
            "versions": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "cds": PARSER_VERSION,
                         **{name: importlib.metadata.version(name) for name in ("python-registry", "python-evtx", "fastapi", "httpx")},
                         **{tool: run(tool, flag).strip() if shutil.which(tool) else "not installed"
                            for tool, flag in (("fls", "-V"), ("mke2fs", "-V"), ("mkntfs", "-V"))}},
            "requests": measurements, "target_met": measurements["filtered_first"]["max_seconds"] < 1,
            "worker": json.loads(child.stdout)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the measured JSON report")
    parser.add_argument("--worker-probe", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_probe:
        report = worker_probe(args.worker_probe)
    else:
        with tempfile.TemporaryDirectory(prefix="cds-performance-") as temporary:
            report = benchmark(Path(temporary))
    data = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(data)
    print(data)
