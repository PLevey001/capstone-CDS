"""Generate fixtures and import them into the running local CDS server."""
import argparse
import json
import tempfile
import time
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.make_demo import make_acceptance, make_demo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--acceptance", action="store_true", help="Build, import, and verify the combined Stage 10 case")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "demo-evidence")
    args = parser.parse_args()
    directory = args.output
    directory.mkdir(parents=True, exist_ok=True)
    if args.acceptance:
        directory = Path(tempfile.mkdtemp(prefix="acceptance-", dir=directory)) / "sources"
        manifest = make_acceptance(directory)
        sources = manifest["sources"]
    else:
        sources = make_demo(directory)

    def post(path, data, content_type):
        request = Request(args.url + "/api" + path, data=data,
                          headers={"Content-Type": content_type, "X-CDS-Request": "local-ui"}, method="POST")
        with urlopen(request, timeout=60) as response:
            return json.load(response)

    case = post("/cases", json.dumps({"name": manifest["name"] if args.acceptance else "USB activity · Demo investigation",
                "description": "Synthetic evidence with source-side known answers. See scripts/make_demo.py and docs/acceptance.md."}).encode(), "application/json")
    def upload(name):
        expected = sources[name]
        return post(f"/cases/{case['id']}/evidence?" + urlencode({"filename": name,
                    "kind": expected.get("kind", "auto"), "sector_size": expected.get("sector_size", 512)}),
                    (directory / name).read_bytes(), "application/octet-stream")
    with ThreadPoolExecutor(max_workers=3) as pool:
        for item in pool.map(upload, sources):
            print(f"Queued {item['name']}")
    if args.acceptance:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            with urlopen(args.url + f"/api/cases/{case['id']}/evidence", timeout=30) as response:
                rows = json.load(response)
            if all(row["status"] not in ("queued", "running") for row in rows):
                break
            time.sleep(0.2)
        else:
            raise SystemExit("Analysis did not finish within 180 seconds; inspect the case before retrying.")
        if {row["name"] for row in rows} != set(sources):
            raise SystemExit("Imported source set differs from the acceptance manifest.")
        for row in rows:
            expected = sources[row["name"]]
            if row["sha256"] != expected["sha256"] or row["record_count"] != len(expected["records"]):
                raise SystemExit(f"Known-answer mismatch for {row['name']}: inspect the saved run and manifest.")
            print(f"{row['name']}: {row['record_count']} records, job {row['status']}, coverage {row['coverage']['status']}")
        receipt = {"case_id": case["id"], "url": args.url, "sources": {row["name"]: row["id"] for row in rows}}
        (directory / "import.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"Verified {len(rows)} sources and {manifest['record_count']} records. Manifest: {directory / 'manifest.json'}")
    print(f"Open {args.url} to review the synthetic demo case.")


if __name__ == "__main__":
    main()
