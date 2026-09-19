"""Read-only parsers. Workers never write to the case database."""

import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

PARSER_VERSION = "cds/0.1.0"


def report(work_dir, stage, progress):
    root = Path(work_dir)
    temporary = root / "progress.tmp"
    temporary.write_text(json.dumps({"stage": stage, "progress": progress}), encoding="utf-8")
    temporary.replace(root / "progress.json")


def run_tool(args, timeout, max_bytes=16 * 1024**2):
    """Bound command duration and output size; never invoke a shell."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(args, stdout=output, stderr=errors, env={**os.environ, "TZ": "UTC", "LC_ALL": "C"})
        started = time.monotonic()
        try:
            while process.poll() is None:
                if time.monotonic() - started > timeout:
                    raise ValueError(f"{Path(args[0]).name} exceeded the {timeout}s analysis limit")
                if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > max_bytes:
                    raise ValueError("Tool output exceeded the analysis limit; use a smaller image for this prototype")
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > max_bytes:
            raise ValueError("Tool output exceeded the analysis limit; use a smaller image for this prototype")
        output.seek(0)
        errors.seek(0)
        return process.returncode, output.read().decode("utf-8", errors="replace"), errors.read(4096).decode("utf-8", errors="replace")


def parse_partitions(output, sector_size):
    partitions = []
    for line in output.splitlines():
        match = re.match(r"^\s*\d+:\s+(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.+)$", line)
        if match:
            slot, start, _end, length, description = match.groups()
            partitions.append({"slot": slot, "start_sector": int(start), "length_sectors": int(length),
                               "sector_size": sector_size, "description": description.strip()})
    return partitions


def parse_bodyfile(output, partition_offset, limit):
    items = []
    malformed = 0
    truncated = False
    for line in output.splitlines():
        if not line:
            continue
        if len(items) >= limit:
            truncated = True
            break
        try:
            # Work from the right so a pipe in a filename does not shift fields.
            _hash, rest = line.split("|", 1)
            name, address, mode, uid, gid, size, atime, mtime, ctime, crtime = rest.rsplit("|", 9)
            deleted = name.endswith(" (deleted)") or name.endswith(" (deleted-realloc)")
            items.append({"path": name, "kind": "directory" if "d" in mode[:3] else "file",
                          "size": int(size), "deleted": deleted, "partition_offset": partition_offset,
                          "metadata_address": address, "details": {
                              "parser": "sleuthkit/fls", "mode": mode, "uid": uid, "gid": gid,
                              "timestamps_unix": {"accessed": int(atime), "modified": int(mtime),
                                                  "metadata_changed": int(ctime), "created": int(crtime)},
                              "timestamp_note": "Zero may mean unavailable. UTC is assumed for timestamps without a timezone. Filesystem timestamps do not prove user actions."}})
        except (ValueError, TypeError):
            malformed += 1
    return items, malformed, truncated


def inspect_image(path, job, result, settings, work_dir):
    if not shutil.which("mmls") or not shutil.which("fls"):
        raise ValueError("Raw image analysis requires The Sleuth Kit (mmls and fls). Install it, then retry.")
    _, version, _ = run_tool(["fls", "-V"], settings["tool_timeout"])
    result["metadata"]["sleuthkit_version"] = version.strip()
    sector = job["sector_size"]
    report(work_dir, "Discovering partitions", 60)
    code, output, _ = run_tool(["mmls", "-i", "raw", "-b", str(sector), "-a", str(path)], settings["tool_timeout"])
    partitions = parse_partitions(output, sector) if code == 0 else []
    result["partitions"] = partitions
    # A filesystem-only image has no partition table and starts at sector zero.
    offsets = list(dict.fromkeys(p["start_sector"] for p in partitions)) or [0]
    if len(offsets) > 128:
        raise ValueError("Image has more than 128 partitions; outside prototype limits")
    readable = 0
    for index, offset in enumerate(offsets):
        remaining = settings["max_artifacts"] - len(result["artifacts"])
        if remaining <= 0:
            result["warnings"].append(f"Artifact limit ({settings['max_artifacts']}) reached; inventory is incomplete.")
            break
        report(work_dir, f"Indexing filesystem {index + 1}/{len(offsets)}", 65 + int(25 * index / len(offsets)))
        args = ["fls", "-i", "raw", "-b", str(sector), "-o", str(offset), "-r", "-p", "-m", "/", str(path)]
        code, listing, error = run_tool(args, settings["tool_timeout"])
        if code:
            result["warnings"].append(f"Sector {offset}: {error.strip()[:500] or 'filesystem could not be read'}")
            continue
        readable += 1
        items, malformed, truncated = parse_bodyfile(listing, offset, remaining)
        result["artifacts"].extend(items)
        if malformed:
            result["warnings"].append(f"Sector {offset}: {malformed} malformed tool records were skipped.")
        if truncated:
            result["warnings"].append(f"Artifact limit ({settings['max_artifacts']}) reached; inventory is incomplete.")
        if error.strip():
            result["warnings"].append(f"Sector {offset}: {error.strip()[:500]}")
    if not readable:
        raise ValueError("No supported filesystem could be read. Check image format and sector size; encrypted images are unsupported.")
    result["metadata"]["readable_filesystems"] = readable
    result["metadata"]["sector_size"] = sector
    result["metadata"]["partition_note"] = "Observed layout at import; not historical device changes."


def inspect_file(path, name, result):
    suffix = Path(name).suffix.lower()
    size = path.stat().st_size
    with path.open("rb") as source:
        sample = source.read(65536)
    meta = result["metadata"]
    meta["mime_type_hint"] = mimetypes.guess_type(name)[0] or "application/octet-stream"
    meta["mime_note"] = "MIME hint is based on the filename, not verified content type."
    if suffix == ".json":
        if size > 4 * 1024**2:
            result["warnings"].append("JSON exceeds the 4 MiB parser limit; only source metadata was indexed.")
            return
        with path.open("r", encoding="utf-8-sig") as source:
            data = json.load(source)
        meta.update({"format": "JSON", "root_type": type(data).__name__})
        if isinstance(data, dict):
            meta["top_level_keys"] = [key[:256] for key in list(data)[:50]]
            meta["key_count"] = len(data)
        elif isinstance(data, list):
            meta["item_count"] = len(data)
    elif suffix in {".txt", ".log", ".csv", ".tsv"}:
        text = sample.decode("utf-8-sig", errors="replace")
        meta.update({"format": "Delimited text" if suffix in {".csv", ".tsv"} else "Text",
                     "sample_bytes": len(sample), "sample_truncated": size > len(sample),
                     "sample_line_count": len(text.splitlines()), "encoding": "UTF-8 (replacement on invalid bytes)"})
        if suffix in {".csv", ".tsv"}:
            reader = csv.reader(io.StringIO(text), delimiter="\t" if suffix == ".tsv" else ",")
            meta["columns"] = [value[:256] for value in next(reader, [])[:100]]
    else:
        meta["format"] = "Generic file"
        result["warnings"].append("No content parser for this file type; SHA-256 and source metadata only.")


def analyze(job, settings, work_dir):
    """One job per process. Always return serializable results, including failures."""
    result = {"sha256": job.get("sha256"), "metadata": {"parser_version": PARSER_VERSION},
              "artifacts": [], "partitions": [], "warnings": []}
    try:
        path = Path(job["source_path"])
        size = path.stat().st_size
        if size != job["size"]:
            raise ValueError("Stored evidence size changed since import")
        digest = hashlib.sha256()
        consumed = 0
        last_report = 0
        with path.open("rb") as source:
            while chunk := source.read(1024**2):
                digest.update(chunk)
                consumed += len(chunk)
                if time.monotonic() - last_report > 0.2:
                    report(work_dir, "Computing SHA-256", int(55 * consumed / max(size, 1)))
                    last_report = time.monotonic()
        observed_hash = digest.hexdigest()
        if job.get("sha256") and observed_hash != job["sha256"]:
            result["metadata"]["observed_sha256"] = observed_hash
            raise ValueError("Stored evidence hash changed since the previous analysis. The original recorded hash has been retained.")
        result["sha256"] = observed_hash
        result["metadata"].update({"size_bytes": size, "imported_at": job["imported_at"],
                                    "timestamp_note": "Import time is a CDS action, not the original file creation time."})
        result["artifacts"].append({"path": job["name"], "kind": "source", "size": size,
                                    "details": {"sha256": result["sha256"], "parser": PARSER_VERSION}})
        if job["kind"] == "raw_image":
            inspect_image(path, job, result, settings, work_dir)
        else:
            report(work_dir, "Parsing file metadata", 70)
            inspect_file(path, job["name"], result)
        report(work_dir, "Saving findings", 95)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"[:2000]
    return result
