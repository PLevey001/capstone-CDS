"""Read-only parsers. Workers never write to the case database."""

import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import selectors
import shutil
import subprocess
import time
from pathlib import Path

import cds.artifacts as artifact_rules
import cds.config as config
import cds.coverage as coverage_tools

PARSER_VERSION = "cds/0.2.1"


class ToolLimitError(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _capture(args, timeout, max_bytes):
    """Bound total output and duration, with a separate 64 KiB stderr ceiling."""
    output = bytearray()
    errors = bytearray()
    stderr_limit = min(max_bytes, config.TOOL_STDERR_BYTES)
    deadline = time.monotonic() + timeout
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}) as process:
        try:
            with selectors.DefaultSelector() as streams:
                streams.register(process.stdout, selectors.EVENT_READ, (output, max_bytes, "stdout"))
                streams.register(process.stderr, selectors.EVENT_READ, (errors, stderr_limit, "stderr"))
                # Drain both pipes so a tool writing diagnostics cannot block its output.
                while streams.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(args, timeout)
                    for key, _ in streams.select(remaining):
                        buffer, limit, name = key.data
                        budget = max_bytes - len(output) - len(errors)
                        chunk = os.read(key.fd, min(65536, limit - len(buffer) + 1, budget + 1))
                        if not chunk:
                            streams.unregister(key.fileobj)
                            continue
                        if len(buffer) + len(chunk) > limit or len(chunk) > budget:
                            raise ToolLimitError("tool_output_limit",
                                                 f"{Path(args[0]).name} {name} exceeded the output budget "
                                                 f"({max_bytes} bytes total; up to {stderr_limit} bytes of stderr)")
                        buffer.extend(chunk)
            # A tool may close its output streams before the process exits.
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise ToolLimitError("tool_timeout", f"{Path(args[0]).name} exceeded the {timeout}s limit") from error
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        return process.returncode, bytes(output), errors[:4096].decode("utf-8", errors="replace")


def extract_artifact(target, tool_timeout=90, max_bytes=64 * 1024**2):
    """Return the raw bytes of one artifact's contents.

    For disk-image sources, runs The Sleuth Kit's icat against the recorded
    metadata address, which recovers deleted files whose contents survive.
    For logical file sources, returns the imported file's own bytes. Raises
    ToolLimitError on limits and ValueError on anything not extractable.
    """
    status = artifact_rules.download_status(target, target["source_kind"], target["result_run_id"])
    if not status["available"]:
        raise ValueError(status["reason"])
    if target["source_kind"] == "raw_image":
        if not shutil.which("icat"):
            raise ValueError("File extraction requires The Sleuth Kit (icat). Install it, then retry.")
        args = ["icat"]
        if target["deleted"]:
            args.append("-r")
        args.extend(["-i", "raw", "-b", str(target["sector_size"]),
                     "-o", str(target["partition_offset"] or 0), target["source_path"], str(target["metadata_address"])])
        code, data, error = _capture(args, tool_timeout, max_bytes)
        if code != 0:
            detail = error.strip()[:300] or "icat could not read this file's contents."
            raise ValueError(f"Extraction failed; no file was downloaded. {detail}")
        return data
    # Logical file source: the artifact is the imported file itself.
    path = Path(target["source_path"])
    if not path.is_file():
        raise ValueError("The stored source file is no longer available.")
    if path.stat().st_size > max_bytes:
        raise ToolLimitError("tool_output_limit", "File exceeds the extraction size limit for this prototype")
    return path.read_bytes()


def report(work_dir, stage, progress):
    root = Path(work_dir)
    temporary = root / "progress.tmp"
    temporary.write_text(json.dumps({"stage": stage, "progress": progress}), encoding="utf-8")
    temporary.replace(root / "progress.json")


def run_tool(args, timeout, max_bytes=config.TOOL_OUTPUT_BYTES):
    """Read bounded text output from a tool without invoking a shell."""
    code, output, error = _capture(args, timeout, max_bytes)
    return code, output.decode("utf-8", errors="replace"), error


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
    coverage = result["coverage"]
    discovery = coverage["steps"][1]
    coverage_tools.mark(discovery, "running", "discovering", "Discovering allocated partitions.")
    if not shutil.which("mmls") or not shutil.which("fls"):
        coverage_tools.mark(discovery, "failed", "missing_tool", "Install The Sleuth Kit (mmls and fls), then retry.")
        raise ValueError("Raw image analysis requires The Sleuth Kit (mmls and fls). Install it, then retry.")
    _, version, _ = run_tool(["fls", "-V"], settings["tool_timeout"])
    result["metadata"]["sleuthkit_version"] = version.strip()
    coverage["tool_version"] = version.strip()
    sector = job["sector_size"]
    report(work_dir, "Discovering partitions", 60)
    code, output, error = run_tool(["mmls", "-i", "raw", "-b", str(sector), "-a", str(path)], settings["tool_timeout"])
    partitions = parse_partitions(output, sector) if code == 0 else []
    result["partitions"] = partitions
    partition_rows = sum(bool(re.match(r"^\s*\d+:\s+", line)) for line in output.splitlines())
    unparsed = partition_rows - len(partitions) if code == 0 else 0
    if partitions:
        limited = unparsed > 0 or bool(error.strip())
        coverage_tools.mark(discovery, "partial" if limited else "complete",
                            "malformed_partition_records" if unparsed else "tool_warning" if limited else "partition_table_read",
                            (f"{unparsed} partition records could not be parsed; their filesystems were not examined. " if unparsed else "")
                            + (error.strip()[:500] or "Allocated partitions reported by mmls; unallocated space is outside this inventory."),
                            processed=len(partitions), total=None if unparsed else len(partitions))
    else:
        coverage_tools.mark(discovery, "unknown", "partition_layout_unknown",
                            "No partition layout was established. Trying a filesystem at sector 0; this does not establish whole-image coverage.")
    offsets = list(dict.fromkeys(p["start_sector"] for p in partitions)) or [0]
    # Create every scope before applying limits so untouched partitions remain visible.
    inventories = [coverage_tools.step(f"filesystem:{offset}", f"Filesystem at sector {offset:,}", "sleuthkit/fls",
                                       partition_offset=offset, sector_size=sector) for offset in offsets]
    coverage["steps"][2:] = inventories
    if len(offsets) > 128:
        for item in inventories:
            coverage_tools.mark(item, "skipped", "partition_limit", "More than 128 partitions; outside prototype limits.")
        raise ValueError("Image has more than 128 partitions; outside prototype limits")
    readable = 0
    for index, (offset, inventory) in enumerate(zip(offsets, inventories)):
        remaining = settings["max_artifacts"] - len(result["artifacts"])
        if remaining <= 0:
            message = f"Artifact limit ({settings['max_artifacts']}) reached; inventory is incomplete."
            coverage_tools.mark(inventory, "skipped", "artifact_limit", message)
            if message not in result["warnings"]:
                result["warnings"].append(message)
            continue
        report(work_dir, f"Indexing filesystem {index + 1}/{len(offsets)}", 65 + int(25 * index / len(offsets)))
        coverage_tools.mark(inventory, "running", "indexing", "Reading directory entries.")
        args = ["fls", "-i", "raw", "-b", str(sector), "-o", str(offset), "-r", "-p", "-m", "/", str(path)]
        try:
            code, listing, error = run_tool(args, settings["tool_timeout"])
        except (ValueError, OSError) as exc:
            coverage_tools.mark(inventory, "failed", getattr(exc, "reason", "tool_error"), str(exc)[:500])
            result["warnings"].append(f"Sector {offset}: {exc}"[:600])
            continue
        if code:
            message = error.strip()[:500] or "Filesystem could not be read; check format, sector size, and encryption."
            coverage_tools.mark(inventory, "failed", "filesystem_unreadable", message)
            result["warnings"].append(f"Sector {offset}: {message}")
            continue
        readable += 1
        items, malformed, truncated = parse_bodyfile(listing, offset, remaining)
        result["artifacts"].extend(items)
        reasons = []
        if malformed:
            reasons.append(f"{malformed} malformed tool records were skipped")
        if truncated:
            reasons.append(f"Artifact limit ({settings['max_artifacts']}) reached; inventory is incomplete")
        if error.strip():
            reasons.append(error.strip()[:500])
        coverage_tools.mark(inventory, "partial" if reasons else "complete",
                            "artifact_limit" if truncated else "malformed_records" if malformed else "tool_warning" if reasons else "listing_read",
                            "; ".join(reasons) if reasons else "Directory entries returned by fls were indexed. File contents were not examined.",
                            processed=len(items), records_returned=sum(bool(line) for line in listing.splitlines()),
                            malformed_records=malformed)
        for reason in reasons:
            result["warnings"].append(f"Sector {offset}: {reason}.")
    if not readable and any(item["status"] == "failed" for item in inventories):
        raise ValueError("No supported filesystem could be read. Check image format and sector size; encrypted images are unsupported.")
    result["metadata"]["readable_filesystems"] = readable
    result["metadata"]["sector_size"] = sector
    result["metadata"]["partition_note"] = "Observed layout at import; not historical device changes."


def inspect_file(path, name, result):
    content = result["coverage"]["steps"][1]
    coverage_tools.mark(content, "running", "parsing", "Reading file metadata.")
    suffix = Path(name).suffix.lower()
    size = path.stat().st_size
    with path.open("rb") as source:
        sample = source.read(65536)
    meta = result["metadata"]
    meta["mime_type_hint"] = mimetypes.guess_type(name)[0] or "application/octet-stream"
    meta["mime_note"] = "MIME hint is based on the filename, not verified content type."
    if suffix == ".json":
        if size > 4 * 1024**2:
            coverage_tools.mark(content, "skipped", "json_size_limit", "JSON exceeds the 4 MiB parser limit; source metadata only.")
            result["warnings"].append("JSON exceeds the 4 MiB parser limit; only source metadata was indexed.")
            return
        with path.open("r", encoding="utf-8-sig") as source:
            data = json.load(source)
        coverage_tools.mark(content, "complete", "json_metadata_read", "JSON parsed for root type and top-level metadata; nested content was not interpreted.", processed=size)
        meta.update({"format": "JSON", "root_type": type(data).__name__})
        if isinstance(data, dict):
            meta["top_level_keys"] = [key[:256] for key in list(data)[:50]]
            meta["key_count"] = len(data)
            if len(data) > 50 or any(len(key) > 256 for key in list(data)[:50]):
                coverage_tools.mark(content, "partial", "metadata_limit",
                                    "JSON parsed, but saved key names are limited to 50 keys and 256 characters each. Nested content was not interpreted.")
        elif isinstance(data, list):
            meta["item_count"] = len(data)
    elif suffix in {".txt", ".log", ".csv", ".tsv"}:
        text = sample.decode("utf-8-sig", errors="replace")
        try:
            sample.decode("utf-8-sig")
            replaced = False
        except UnicodeDecodeError:
            replaced = True
        limited = size > len(sample)
        coverage_tools.mark(content, "partial" if limited or replaced else "complete",
                            "sample_limit" if limited else "encoding_replaced" if replaced else "text_metadata_read",
                            ("Only the first 64 KiB were sampled. " if limited else "All bytes were read for basic text metadata. ")
                            + ("Invalid or incomplete UTF-8 sequences were replaced. " if replaced else "")
                            + "Line counts and optional column headers only; events were not interpreted.", processed=len(sample))
        meta.update({"format": "Delimited text" if suffix in {".csv", ".tsv"} else "Text",
                     "sample_bytes": len(sample), "sample_truncated": size > len(sample),
                     "sample_line_count": len(text.splitlines()), "encoding": "UTF-8 (replacement on invalid bytes)"})
        if suffix in {".csv", ".tsv"}:
            reader = csv.reader(io.StringIO(text), delimiter="\t" if suffix == ".tsv" else ",")
            columns = next(reader, [])
            meta["columns"] = [value[:256] for value in columns[:100]]
            if len(columns) > 100 or any(len(value) > 256 for value in columns[:100]):
                coverage_tools.mark(content, "partial", content["reason"] if limited or replaced else "metadata_limit",
                                    content["detail"] + " Saved headers are limited to 100 columns and 256 characters each.")
    else:
        coverage_tools.mark(content, "unsupported", "no_content_parser", "No content parser for this file type; source hash and metadata only.")
        meta["format"] = "Generic file"
        result["warnings"].append("No content parser for this file type; SHA-256 and source metadata only.")


def analyze(job, settings, work_dir):
    """One job per process. Always return serializable results, including failures."""
    result = {"sha256": job.get("sha256"), "metadata": {"parser_version": PARSER_VERSION},
              "artifacts": [], "partitions": [], "warnings": []}
    coverage = result["coverage"] = coverage_tools.begin(job["kind"], job["size"], settings, PARSER_VERSION, job.get("run_id"))
    integrity = coverage["steps"][0]
    coverage_tools.mark(integrity, "running", "hashing", "Reading source bytes for SHA-256.")
    try:
        path = Path(job["source_path"])
        size = path.stat().st_size
        if size != job["size"]:
            coverage_tools.mark(integrity, "failed", "source_size_changed", "Stored evidence size changed since import.")
            raise ValueError("Stored evidence size changed since import")
        digest = hashlib.sha256()
        consumed = 0
        last_report = 0
        with path.open("rb") as source:
            while chunk := source.read(1024**2):
                digest.update(chunk)
                consumed += len(chunk)
                integrity["processed"] = consumed
                if time.monotonic() - last_report > 0.2:
                    report(work_dir, "Computing SHA-256", int(55 * consumed / max(size, 1)))
                    last_report = time.monotonic()
        observed_hash = digest.hexdigest()
        if job.get("sha256") and observed_hash != job["sha256"]:
            coverage_tools.mark(integrity, "failed", "source_hash_changed", "Source hash differs from the previous analysis; original hash retained.")
            result["metadata"]["observed_sha256"] = observed_hash
            raise ValueError("Stored evidence hash changed since the previous analysis. The original recorded hash has been retained.")
        result["sha256"] = observed_hash
        coverage_tools.mark(integrity, "complete", "hash_verified" if job.get("sha256") else "hash_recorded",
                            "All source bytes hashed." + (" Matches the previous analysis." if job.get("sha256") else " This is the initial recorded hash."), processed=consumed)
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
        for item in coverage["steps"]:
            if item["status"] == "running":
                coverage_tools.mark(item, "failed", getattr(error, "reason", "parser_error"), result["error"])
    coverage_tools.finish(coverage, result.get("error"))
    return result
