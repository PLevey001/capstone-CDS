"""Generate fixtures and import them into the running local CDS server."""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from make_demo import make_demo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[1] / "demo-evidence"
    manifest = make_demo(directory)

    def post(path, data, content_type):
        request = Request(args.url + "/api" + path, data=data,
                          headers={"Content-Type": content_type, "X-CDS-Request": "local-ui"}, method="POST")
        with urlopen(request, timeout=60) as response:
            return json.load(response)

    case = post("/cases", json.dumps({"name": "USB activity · Demo investigation",
                "description": "Synthetic evidence for the CDS capstone demo. A disk image, activity export, and device metadata."}).encode(), "application/json")
    def upload(name):
        return post(f"/cases/{case['id']}/evidence?" + urlencode({"filename": name}),
                    (directory / name).read_bytes(), "application/octet-stream")
    with ThreadPoolExecutor(max_workers=3) as pool:
        for item in pool.map(upload, manifest):
            print(f"Queued {item['name']}")
    print(f"Open {args.url} to review the synthetic demo case.")


if __name__ == "__main__":
    main()
