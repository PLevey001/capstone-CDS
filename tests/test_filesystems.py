import hashlib
import json
import struct

import pytest

import cds.analysis as analysis
from cds.artifacts import valid_metadata_address
from cds.store import Store
from tests.filesystem_fixtures import CHROME, FIREFOX, make_filesystem_image, make_sources, missing_tools, run

LIMITS = {"tool_timeout": 10, "max_artifacts": 500}
VARIANTS = [(filesystem, sector, partitioned) for filesystem in ("ntfs", "ext4")
            for sector in (512, 4096) for partitioned in (False, True)]


@pytest.fixture(scope="module", params=VARIANTS,
                ids=[f"{fs}-{sector}-{'mbr' if partitioned else 'bare'}" for fs, sector, partitioned in VARIANTS])
def filesystem_image(request, tmp_path_factory):
    missing = missing_tools()
    if missing:
        pytest.skip("Missing image fixture tools: " + ", ".join(missing))
    root = tmp_path_factory.mktemp(request.node.name or "filesystems")
    filesystem, sector, partitioned = request.param
    files = make_sources(root / "browser-sources")
    image, manifest = make_filesystem_image(root / "fixture", filesystem, sector, partitioned, files)
    assert json.loads((image.parent / "manifest.json").read_text(encoding="utf-8")) == manifest
    store = Store(root / "workspace")
    store.initialize()
    case = store.create_case("Filesystem validation", "Synthetic known answers")
    before = hashlib.sha256(image.read_bytes()).hexdigest()
    store.register("image", case["id"], image.name, image.stat().st_size, image, "raw_image", sector)
    job = store.claim(LIMITS, analysis.PARSER_VERSION)
    work = store.root / "work" / job["run_id"]
    work.mkdir()
    result = analysis.analyze(job, LIMITS, str(work))
    assert "error" not in result, result.get("error")
    assert store.finish(job, result)
    assert result["sha256"] == store.detail("image")["sha256"] == before
    yield image, manifest, store, job, result
    # Analysis, SQLite WAL handling, and every download must leave evidence intact.
    assert hashlib.sha256(image.read_bytes()).hexdigest() == before
    assert not list(work.glob("content-*"))


def test_filesystem_inventory_timestamps_and_exact_extraction(filesystem_image):
    image, manifest, store, job, result = filesystem_image
    artifacts = {item["path"]: item for item in store.artifacts("image", "", 0, 500)["items"]}
    for path in manifest["directories"]:
        artifact = artifacts[path]
        assert artifact["kind"] == "directory" and not artifact["deleted"]
        assert artifact["details"]["timestamps_unix"] == manifest["directory_timestamps"]
        assert not artifact["download"]["available"]
    for path, expected in manifest["files"].items():
        listed_path = path + (" (deleted)" if expected["deleted"] else "")
        if "symlink_target" in expected:
            listed_path += " -> " + expected["symlink_target"]
        artifact = artifacts[listed_path]
        assert artifact["deleted"] == expected["deleted"]
        assert artifact["kind"] == "file"
        assert artifact["size"] == expected.get("inventory_size", expected["size"])
        assert artifact["partition_offset"] == manifest["partition_offset"]
        assert artifact["details"]["parser"] == "sleuthkit/fls"
        assert artifact["details"]["timestamps_unix"] == expected["timestamps"]
        assert artifact["run_id"] == job["run_id"] and artifact["evidence_id"] == "image"
        assert valid_metadata_address(artifact["metadata_address"])
        if manifest["filesystem"] == "ntfs":
            assert len(artifact["metadata_address"].split("-")) == 3
            assert artifact["metadata_address"].split("-")[1] == "128"  # unnamed $DATA, not $FILE_NAME
        else:
            assert artifact["metadata_address"].isdigit()
        original = (image.parent / expected["content_file"]).read_bytes()
        assert len(original) == expected["size"]
        assert hashlib.sha256(original).hexdigest() == expected["sha256"]
        if "symlink_target" in expected:
            assert not artifact["download"]["available"]
            assert "Symbolic links" in artifact["download"]["reason"]
            with pytest.raises(ValueError, match="Symbolic links"):
                analysis.extract_artifact(store.extraction_target("image", artifact["id"]))
            continue
        assert artifact["download"]["available"]
        extracted = analysis.extract_artifact(store.extraction_target("image", artifact["id"]))
        if "recovered_hex" in expected:
            # A zero-length deleted inode is downloadable, but none of its
            # original bytes were recovered. Eligibility is not a recovery claim.
            assert extracted == bytes.fromhex(expected["recovered_hex"]) == b""
            assert extracted != original and artifact["size"] == 0
        else:
            assert extracted == original
            assert hashlib.sha256(extracted).hexdigest() == expected["sha256"]
    if manifest["filesystem"] == "ext4":
        assert artifacts["/small.txt"]["metadata_address"] == artifacts["/hardlink.txt"]["metadata_address"]
        assert artifacts["/symlink.txt -> small.txt"]["details"]["mode"].startswith("l/l")
    inventory = next(step for step in result["coverage"]["steps"] if step["id"].startswith("filesystem:"))
    assert inventory["status"] == "complete" and inventory["malformed_records"] == 0
    assert inventory["parser"] == "sleuthkit/fls"
    assert "File contents were not examined" in inventory["detail"]
    assert "unallocated space" in result["coverage"]["scope"]


def test_real_filesystem_browser_discovery_wal_and_saved_provenance(filesystem_image):
    image, manifest, store, job, result = filesystem_image
    artifacts = {item["path"]: item for item in store.artifacts("image", "", 0, 500)["items"]}
    records = store.records("image", "", 0, 100)["items"]
    actual = []
    for record in records:
        artifact = store.artifact("image", record["artifact_id"])
        content = artifact["details"]["content"]
        actual.append({"path": artifact["path"], "parser": record["parser"], "source_key": record["source_key"],
                       "at": record["at"], "url": record["details"]["url"]})
        assert content["parser"] == record["parser"]
        assert content["sha256"] == manifest["files"][artifact["path"]]["sha256"]
        assert content["parser_version"] == analysis.PARSER_VERSION
        assert content["run_id"] == record["run_id"] == job["run_id"]
        assert content["evidence_id"] == record["evidence_id"] == "image"
    assert sorted(actual, key=lambda row: (row["path"], row["source_key"])) == manifest["browser_records"]
    steps = {step["label"]: step for step in result["coverage"]["steps"]}
    chrome = steps[CHROME]
    assert chrome["sidecar_status"] == "included" and chrome["status"] == "complete"
    assert "committed WAL" in chrome["detail"]
    assert len(chrome["sidecars"]) == 2
    for sidecar in chrome["sidecars"]:
        expected = manifest["files"][sidecar["path"]]
        artifact = artifacts[sidecar["path"]]
        assert sidecar["extracted"] and sidecar["included"]
        assert sidecar["sha256"] == expected["sha256"] == artifact["details"]["content"]["sha256"]
        assert sidecar["size_bytes"] == expected["size"]
        assert sidecar["metadata_address"] == artifact["metadata_address"]
        assert sidecar["partition_offset"] == manifest["partition_offset"]
        assert sidecar["artifact_key"] == artifact["details"]["content"]["artifact_key"]
    assert steps[FIREFOX]["sidecar_status"] == "absent"
    assert "base database only" in steps[FIREFOX]["detail"]
    summary = next(step for step in result["coverage"]["steps"] if step["id"] == "image-browser-history")
    assert summary["candidates_found"] == summary["extracted"] == summary["parsed"] == 2
    assert summary["sidecars_included"] == summary["sidecars_absent"] == 1
    assert summary["sidecars_unusable"] == 0


def test_actual_geometry_and_storage_variants(filesystem_image):
    image, manifest, store, job, result = filesystem_image
    sector = manifest["sector_size"]
    offset = manifest["partition_offset"]
    assert image.stat().st_size == offset * sector + manifest["volume_bytes"]
    with image.open("rb") as source:
        mbr = source.read(sector)
        source.seek(offset * sector)
        boot = source.read(4096)
    partition_step = next(step for step in result["coverage"]["steps"] if step["id"] == "partitions")
    if manifest["partitions"]:
        expected = manifest["partitions"][0]
        assert mbr[510:512] == b"\x55\xaa" and mbr[450] == expected["type"]
        assert struct.unpack_from("<II", mbr, 454) == (offset, expected["length_sectors"])
        assert len(result["partitions"]) == 1
        assert result["partitions"][0]["start_sector"] == offset
        assert result["partitions"][0]["length_sectors"] == expected["length_sectors"]
        assert result["partitions"][0]["sector_size"] == sector
        assert partition_step["status"] == result["coverage"]["status"] == "complete"
    else:
        assert result["partitions"] == []
        assert partition_step["status"] == "unknown"
        assert partition_step["reason"] == "partition_layout_unknown"
        assert result["coverage"]["status"] == "partial"
    info = run("fsstat", "-b", sector, "-o", offset, image)
    artifacts = {item["path"]: item for item in store.artifacts("image", "", 0, 500)["items"]}
    if manifest["filesystem"] == "ntfs":
        assert "File System Type: NTFS" in info and "Version: Windows XP" in info
        assert f"Sector Size: {sector}" in info and "Cluster Size: 4096" in info
        assert struct.unpack_from("<H", boot, 11)[0] == sector
        assert boot[13] * sector == manifest["block_size"]
        assert struct.unpack_from("<I", boot, 28)[0] == offset
        for path in ("/small.txt", "/large.bin", "/deleted.txt"):
            artifact = artifacts[path + (" (deleted)" if manifest["files"][path]["deleted"] else "")]
            inode = run("istat", "-b", sector, "-o", offset, image, artifact["metadata_address"])
            data = next(line for line in inode.splitlines() if line.startswith("Type: $DATA"))
            assert "   " + manifest["files"][path]["data_storage"] + "   " in data
    else:
        assert "File System Type: Ext4" in info and "Block Size: 4096" in info
        assert struct.unpack_from("<H", boot, 1024 + 56)[0] == 0xEF53
        assert 1024 << struct.unpack_from("<I", boot, 1024 + 24)[0] == manifest["block_size"]
        assert manifest["block_size"] % sector == 0 and offset * sector % 4096 == 0
        inode = run("istat", "-b", sector, "-o", offset, image, artifacts["/sparse.bin"]["metadata_address"])
        blocks = [int(number) for number in inode.split("Direct Blocks:\n", 1)[1].split()]
        assert len(blocks) == manifest["files"]["/sparse.bin"]["size"] // 4096
        assert sum(block != 0 for block in blocks) * 4096 == manifest["files"]["/sparse.bin"]["allocated_data_bytes"]


@pytest.mark.parametrize("filesystem", ["ntfs", "ext4"])
def test_rebuild_preserves_known_answers_across_host_timezones(tmp_path, monkeypatch, filesystem):
    missing = missing_tools()
    if missing:
        pytest.skip("Missing image fixture tools: " + ", ".join(missing))
    files = make_sources(tmp_path / "browser-sources")
    manifests = []
    for index, timezone in enumerate(("Pacific/Honolulu", "Asia/Tokyo")):
        monkeypatch.setenv("TZ", timezone)
        monkeypatch.setenv("LC_ALL", "C")
        image, manifest = make_filesystem_image(tmp_path / str(index), filesystem, 4096, True, files)
        manifests.append(manifest)
        work = tmp_path / f"work-{index}"
        work.mkdir()
        job = {"id": "image", "run_id": "run", "source_path": str(image), "name": image.name,
               "size": image.stat().st_size, "kind": "raw_image", "sector_size": 4096,
               "imported_at": "2026-01-01T00:00:00Z"}
        result = analysis.analyze(job, LIMITS, str(work))
        assert "error" not in result
        artifacts = {item["path"]: item for item in result["artifacts"]}
        for path, expected in manifest["files"].items():
            listed_path = path + (" (deleted)" if expected["deleted"] else "")
            if "symlink_target" in expected:
                listed_path += " -> " + expected["symlink_target"]
            item = artifacts[listed_path]
            assert item["details"]["timestamps_unix"] == expected["timestamps"]
            if "symlink_target" in expected:
                continue  # Inline symlinks are inventoried; icat cannot extract their target bytes.
            content = analysis._icat(image, 4096, item, 10, 2 * 1024**2)
            assert content == (bytes.fromhex(expected["recovered_hex"]) if "recovered_hex" in expected
                               else (image.parent / expected["content_file"]).read_bytes())
    assert manifests[0] == manifests[1]
