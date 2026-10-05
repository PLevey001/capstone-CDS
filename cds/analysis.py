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
import sys
import tempfile
import time
from pathlib import Path

import cds.artifacts as artifact_rules
import cds.config as config
import cds.coverage as coverage_tools
from cds.evtx_records import SCOPE as EVTX_SCOPE
from cds.history_records import SIDECAR_GAP_SCOPE, SNAPSHOT_SCOPE
from cds.records import record_size
from cds.registry_records import SCOPE as REGISTRY_SCOPE

PARSER_VERSION = "cds/0.8.0"

# Allow a modest multi-user image while capping extraction attempts and one worker's content stage at five minutes.
# Preserve 5A's maximum extraction allowance (32 candidates of 64 MiB), now including sidecars.
IMAGE_CONTENT_LIMITS = {"image_content_candidates": 32, "image_content_timeout": 300,
                        "image_content_bytes": 32 * 64 * 1024**2}


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


def _icat(path, sector_size, artifact, timeout, max_bytes):
    if not shutil.which("icat"):
        raise ValueError("File extraction requires The Sleuth Kit (icat). Install it, then retry.")
    args = ["icat"]
    if artifact["deleted"]:
        args.append("-r")
    args.extend(["-i", "raw", "-b", str(sector_size), "-o", str(artifact["partition_offset"] or 0),
                 str(path), str(artifact["metadata_address"])])
    code, data, error = _capture(args, timeout, max_bytes)
    if code != 0:
        detail = error.strip()[:300] or "icat could not read this file's contents."
        raise subprocess.CalledProcessError(code, args, stderr=detail)
    return data


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
        try:
            return _icat(target["source_path"], target["sector_size"], target, tool_timeout, max_bytes)
        except subprocess.CalledProcessError as error:
            raise ValueError(f"Extraction failed; no file was downloaded. {error.stderr}") from error
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


def _image_candidate_skip_reason(item, deadline, attempted, record_count, payload_stopped):
    limits = config.CONTENT_LIMITS
    if time.monotonic() >= deadline:
        return "image_content_timeout", "Image content-stage time budget reached."
    if attempted >= IMAGE_CONTENT_LIMITS["image_content_candidates"]:
        return "image_candidate_limit", "Image candidate attempt budget reached."
    if record_count >= limits["parsed_records"]:
        return "record_limit", "Image saved-record count budget reached."
    if payload_stopped:
        return "record_payload_limit", "Image saved-record payload budget reached."
    if item["size"] > limits["content_file_bytes"]:
        return "content_file_limit", f"Candidate exceeds the {limits['content_file_bytes']}-byte content limit."
    if not artifact_rules.valid_metadata_address(item["metadata_address"]):
        # Pre-save artifacts have not passed the download path's metadata-address eligibility check.
        return "invalid_metadata_address", "Candidate has no usable metadata address to extract."
    return None


def _parse_image_candidate(path, job, item, step, sidecars, result, settings, work_dir,
                           candidate_deadline, timeout_reason, payload_bytes):
    # The parent subtracts payload_bytes from the subprocess budget (which counts placeholder "source" keys),
    # rechecks after substituting longer artifact keys, and keeps payload_stopped set to skip later candidates.
    limits = config.CONTENT_LIMITS
    content = coverage_tools.find(result["coverage"], "image-content")
    reserved_bytes = 0
    with tempfile.TemporaryDirectory(prefix="content-", dir=work_dir) as directory:
        copy = Path(directory) / ("history.sqlite" if step["parser"] == "browser-history/1" else "content.bin")
        if len({part["path"] for part in sidecars}) != len(sidecars):
            raise ValueError("Multiple allocated entries match a sidecar path; the working set is ambiguous.")
        for part in [item, *sidecars]:
            suffix = part["path"][len(item["path"]):]
            label = "Candidate" if not suffix else f"Sidecar {part['path']}"
            if part["size"] < 0 or not artifact_rules.valid_metadata_address(part["metadata_address"]):
                raise ValueError(f"{label} has no usable size or metadata address to extract.")
            if reserved_bytes + part["size"] > limits["content_file_bytes"]:
                raise ToolLimitError("content_file_limit", f"{label} would exceed the candidate working-set byte budget.")
            if content["extraction_bytes_reserved"] + part["size"] > IMAGE_CONTENT_LIMITS["image_content_bytes"]:
                raise ToolLimitError("image_content_limit", f"{label} would exceed the image extraction-byte budget.")
            remaining = candidate_deadline - time.monotonic()
            if remaining <= 0:
                raise ToolLimitError(timeout_reason, f"{label} was not extracted; content candidate time budget reached.")
            # Charge attempts even on failure; a failed tool may already have read the reserved bytes.
            reserved_bytes += part["size"]
            content["extraction_bytes_reserved"] += part["size"]
            try:
                data = _icat(path, job["sector_size"], part, min(settings["tool_timeout"], remaining), part["size"])
            except subprocess.CalledProcessError as error:
                prefix = "Extraction failed." if not suffix else f"Sidecar extraction failed ({part['path']})."
                raise ValueError(f"{prefix} {error.stderr}") from error
            except ToolLimitError as error:
                raise ToolLimitError(error.reason, f"{label}: {error}") from error
            part["details"]["content"] = {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                                          "evidence_id": job.get("id"), "run_id": result["coverage"]["run_id"],
                                          "artifact_key": part["key"], "parser_version": PARSER_VERSION, "parser": None}
            if not suffix:
                step["extracted"] = True
                if "sidecars" in step:
                    part["details"]["content"]["sidecars"] = step["sidecars"]
            else:
                provenance = next(entry for entry in step["sidecars"] if entry["artifact_key"] == part["key"])
                provenance.update(sha256=part["details"]["content"]["sha256"], size_bytes=len(data), extracted=True)
            if len(data) != part["size"]:
                raise ValueError(f"{label}: extracted byte count differs from the inventory size; candidate was not parsed.")
            copy.with_name(copy.name + suffix).write_bytes(data)
            del data
        remaining = candidate_deadline - time.monotonic()
        if remaining <= 0:
            raise ToolLimitError(timeout_reason, "Content candidate time budget reached before parsing.")
        candidate_limits = {**limits, "parsed_records": limits["parsed_records"] - len(result["records"]),
                            "record_payload_bytes": limits["record_payload_bytes"] - payload_bytes + 2}
        args = [sys.executable, "-m", "cds.content_parser", step["parser"].split("/")[0], str(copy), json.dumps(candidate_limits)]
        if sidecars:
            args.append("--journal-aware")
        code, output, error = _capture(args, min(settings["tool_timeout"], remaining),
                                       candidate_limits["record_payload_bytes"] + config.TOOL_STDERR_BYTES)
        if code:
            raise ValueError(error.strip()[:500] or "The content parser exited without usable results.")
        parsed = json.loads(output)
        if time.monotonic() >= candidate_deadline:
            raise ToolLimitError(timeout_reason, "Content candidate time budget reached; parser output was not saved.")
        if sidecars and parsed["status"] in ("complete", "partial"):
            step["sidecar_status"] = "included"
            step["sidecar_scope"] = parsed["scope"]
            for provenance in step["sidecars"]:
                provenance["included"] = True
    return parsed


def inspect_image(path, job, result, settings, work_dir):
    coverage = result["coverage"]
    discovery = coverage_tools.find(coverage, "partitions")
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
    coverage["steps"].remove(coverage_tools.find(coverage, "inventory"))
    coverage["steps"].extend(inventories)
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

    limits = config.CONTENT_LIMITS
    deadline = time.monotonic() + IMAGE_CONTENT_LIMITS["image_content_timeout"]
    coverage["limits"].update({**limits, **IMAGE_CONTENT_LIMITS})
    coverage["scope"] = ("Source hash, partition discovery, filesystem directory entries, browser visits, Registry keys/values, and Windows events from "
                         "allocated content candidates in that inventory. Other file contents, unallocated space, "
                         "and unmatched paths are not examined. ")
    content = coverage_tools.step("image-content", "Image content candidates", PARSER_VERSION,
                                  unit="candidates", extraction_bytes_reserved=0)
    coverage["steps"].append(content)
    # Paths only nominate candidates; the existing readers decide which schema, if any, is present.
    profile = re.compile(
        r"/(?:AppData/Local/(?:Google/Chrome|Chromium)/User Data|"
        r"Library/Application Support/(?:Google/Chrome|Chromium)|\.config/(?:google-chrome|chromium))"
        r"/[^/]+/History$|"
        r"/(?:AppData/Roaming/Mozilla/Firefox/Profiles|Library/Application Support/Firefox/Profiles|"
        r"\.mozilla/firefox)/[^/]+/places\.sqlite$", re.IGNORECASE)
    hive = re.compile(r"^/(?:Users|Documents and Settings)/[^/]+/NTUSER\.DAT$|"
                      r"^/(?:Windows|WINNT)/System32/config/(?:SYSTEM|SOFTWARE)$", re.IGNORECASE)
    event_log = re.compile(r"^/(?:Windows|WINNT)/System32/winevt/Logs/[^/]+$", re.IGNORECASE)
    allocated_files = {}
    for item in result["artifacts"]:
        if item["kind"] == "file" and not item["deleted"]:
            allocated_files.setdefault((item["partition_offset"], item["path"]), []).append(item)
    candidates = []
    for item in result["artifacts"]:
        if item["kind"] != "file" or item["deleted"]:
            continue
        if hive.search(item["path"]):
            parser, scope = "windows-registry/1", REGISTRY_SCOPE
        elif event_log.search(item["path"]):
            # Every file in the standard log directory is a candidate, including renamed EVTX files.
            parser, scope = "windows-event-log/1", EVTX_SCOPE
        elif profile.search(item["path"]):
            parser, scope = "browser-history/1", SNAPSHOT_SCOPE
        else:
            continue
        browser = parser == "browser-history/1"
        # Exact full paths and partitions prevent adopting another profile's journal. Do not case-fold paths.
        sidecars = [] if not browser else [part for suffix in ("-wal", "-shm")
                    for part in allocated_files.get((item["partition_offset"], item["path"] + suffix), [])]
        for part in [item, *sidecars]:
            # Include the full path so hard links and equal metadata addresses in different partitions cannot collide.
            identity = json.dumps([part["partition_offset"], part["metadata_address"], part["path"]])
            part["key"] = "image:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        step = coverage_tools.step(item["key"], item["path"], parser, scope=scope,
                                   artifact_key=item["key"], partition_offset=item["partition_offset"],
                                   metadata_address=item["metadata_address"], extracted=False)
        if browser:
            step.update(sidecar_status="unusable" if sidecars else "absent",
                        sidecar_scope=SIDECAR_GAP_SCOPE if sidecars else (
                            "No allocated WAL/SHM sidecars were present in the available inventory; base database only. "
                            + SNAPSHOT_SCOPE),
                        sidecars=[{"artifact_key": part["key"], "path": part["path"],
                                   "partition_offset": part["partition_offset"],
                                   "metadata_address": part["metadata_address"],
                                   "extracted": False, "included": False} for part in sidecars])
        coverage["steps"].append(step)
        candidates.append((item, step, sidecars))
    inventory_complete = discovery["status"] == "complete" and all(item["status"] == "complete" for item in inventories)
    coverage_tools.mark(content, "running", "parsing", "Checking allocated browser, Registry, and event-log candidates from the inventory.",
                        total=len(candidates), candidates_found=len(candidates))
    attempted = 0
    payload_bytes = 2  # Match the saved-run array accounting in Store.finish, including remapped artifact keys.
    payload_stopped = False
    for item, step, sidecars in candidates:
        skip = _image_candidate_skip_reason(item, deadline, attempted, len(result["records"]), payload_stopped)
        if skip:
            reason, detail = skip
            coverage_tools.mark(step, "skipped", reason, detail + " " + step.get("sidecar_scope", step["scope"]))
            continue
        attempted += 1
        # The existing 90-second content allowance covers extraction AND parsing for each candidate.
        candidate_deadline = min(deadline, time.monotonic() + limits["content_timeout"])
        timeout_reason = "image_content_timeout" if candidate_deadline == deadline else "content_timeout"
        coverage_tools.mark(step, "running", "extracting", "Extracting an allocated content candidate.")
        report(work_dir, f"Reading content candidate {attempted}/{len(candidates)}", 90)
        try:
            parsed = _parse_image_candidate(path, job, item, step, sidecars, result, settings, work_dir,
                                            candidate_deadline, timeout_reason, payload_bytes)
            parser_status = parsed["status"]
            step["parser"] = item["details"]["content"]["parser"] = parsed["parser"]
            saved = 0
            for record in parsed.pop("records"):
                if time.monotonic() >= candidate_deadline:
                    parsed.update(status="partial", reason=timeout_reason,
                                  detail=f"Saved {saved} records; content candidate time budget reached.")
                    break
                record["artifact_key"] = item["key"]
                size = record_size(record)
                # Inner keys are longer than "source"; recheck bytes after linking to keep the store's run bound exact.
                if payload_bytes + size > limits["record_payload_bytes"]:
                    parsed.update(status="partial", reason="record_payload_limit",
                                  detail=f"Saved {saved} records; image record payload budget reached.")
                    break
                result["records"].append(record)
                payload_bytes += size
                saved += 1
            payload_stopped = parsed["reason"] == "record_payload_limit"
            detail = parsed["detail"].replace(SNAPSHOT_SCOPE, "").rstrip()
            scope = step.get("sidecar_scope", step["scope"])
            if scope not in detail:
                detail += " " + scope
            status = parsed["status"] if parser_status in ("complete", "partial") else parser_status
            if status not in coverage_tools.FINAL_STATUSES:
                detail += f" Unexpected parser status {status!r}; recorded as unknown."
                status = "unknown"
            coverage_tools.mark(step, status, parsed["reason"], detail,
                                processed=saved, total=parsed["total"],
                                **({"counts": parsed["counts"]} if "counts" in parsed else {}))
        except Exception as error:
            # A bad candidate must not discard the inventory or prevent other candidates from being read.
            reason = getattr(error, "reason", "content_parser_error")
            if reason == "tool_timeout" and time.monotonic() >= candidate_deadline:
                reason = timeout_reason
            if sidecars:
                step.update(sidecar_status="unusable", sidecar_scope=SIDECAR_GAP_SCOPE)
                for provenance in step["sidecars"]:
                    provenance["included"] = False
            coverage_tools.mark(step, "failed", reason, str(error)[:600] + (" " + step["sidecar_scope"] if sidecars else ""))
    counts = {"extracted": 0, "parsed": 0, "budget_stops": 0}
    sidecar_counts = {"sidecars_included": 0, "sidecars_absent": 0, "sidecars_unusable": 0}
    status_counts = {"skipped": 0, "failed": 0, "unsupported": 0}
    for _, step, _ in candidates:
        if "sidecar_status" in step:
            sidecar_counts["sidecars_" + step["sidecar_status"]] += 1
        counts["extracted"] += step["extracted"]
        if step["status"] in {"complete", "partial"}:
            counts["parsed"] += 1
        else:
            status = step["status"]
            status_counts[status] = status_counts.get(status, 0) + 1
        if step["reason"].endswith(("_limit", "_timeout")):
            counts["budget_stops"] += 1
    status_detail = ", ".join(f"{count} {status}" for status, count in status_counts.items())
    journal_detail = (f"Browser sidecar coverage by candidate: {sidecar_counts['sidecars_included']} included, "
                      f"{sidecar_counts['sidecars_absent']} with no sidecars in the available inventory, "
                      f"{sidecar_counts['sidecars_unusable']} with sidecars present but an unusable or unexamined working set. ")
    coverage["scope"] += journal_detail
    if any("sidecars" in step for _, step, _ in candidates) and not any(sidecars for _, _, sidecars in candidates):
        coverage["scope"] += SNAPSHOT_SCOPE
        result["warnings"].append(SNAPSHOT_SCOPE)
    detail = (f"Found {len(candidates)} allocated content candidates in the available inventory: "
              f"{counts['extracted']} extracted, {counts['parsed']} parsed, {status_detail}; "
              f"{counts['budget_stops']} budget stops. "
              + ("Inventory or partition discovery was incomplete; additional candidates may be missing. "
                 "Sidecars may also be missing from the available inventory. " if not inventory_complete else "")
              + "Only matching paths in the inventory were considered; this does not establish complete browser, Registry, or event-log coverage. "
              + journal_detail + REGISTRY_SCOPE + " " + EVTX_SCOPE)
    complete = inventory_complete and all(step["status"] == "complete" for _, step, _ in candidates)
    candidate_errors = any(status_counts.get(status, 0) for status in ("failed", "unsupported", "unknown"))
    coverage["scope"] += " " + REGISTRY_SCOPE + " " + EVTX_SCOPE
    coverage_tools.mark(content, "complete" if complete else "partial",
                        "inventory_incomplete" if not inventory_complete else "content_budget" if counts["budget_stops"]
                        else "candidate_errors" if candidate_errors else "candidates_examined" if complete else "candidates_partial",
                        detail, processed=counts["parsed"], **counts, **status_counts, **sidecar_counts)
    if not complete:
        result["warnings"].append(detail)


def inspect_records(path, result, settings, work_dir, parser="browser-history"):
    coverage = result["coverage"]
    limits = config.CONTENT_LIMITS
    coverage["limits"].update(limits)
    if parser == "windows-registry":
        label, scope, candidate = "Windows Registry", REGISTRY_SCOPE, "Registry candidate"
    elif parser == "windows-event-log":
        label, scope, candidate = "Windows event log", EVTX_SCOPE, "EVTX candidate"
    else:
        label, scope, candidate = "Browser history", SNAPSHOT_SCOPE, "SQLite candidate"
    coverage["scope"] = "Source hash, file metadata, and supported content records. " + scope
    records_step = coverage_tools.step(parser, label, parser + "/1")
    coverage["steps"].append(records_step)
    coverage_tools.mark(records_step, "running", "parsing", "Reading a disposable content copy.")
    result["warnings"].append(scope)
    if path.stat().st_size > limits["content_file_bytes"]:
        coverage_tools.mark(records_step, "skipped", "content_file_limit",
                            f"File exceeds the {limits['content_file_bytes']}-byte content parser limit.")
        return
    try:
        with tempfile.TemporaryDirectory(prefix="content-", dir=work_dir) as directory:
            copy = Path(directory) / "content.bin"
            digest = hashlib.sha256()
            copied = 0
            with path.open("rb") as source, copy.open("xb") as target:
                while chunk := source.read(min(1024**2, limits["content_file_bytes"] - copied + 1)):
                    copied += len(chunk)
                    if copied > limits["content_file_bytes"]:
                        raise ToolLimitError("content_file_limit", "File grew beyond the content parser size limit.")
                    digest.update(chunk)
                    target.write(chunk)
            if digest.hexdigest() != result["sha256"]:
                raise ValueError("Source changed between hashing and the content parser copy; no records were saved.")
            timeout = min(settings["tool_timeout"], limits["content_timeout"])
            report(work_dir, "Reading " + label, 75)
            args = [sys.executable, "-m", "cds.content_parser", parser, str(copy), json.dumps(limits)]
            code, output, error = _capture(args, timeout, limits["record_payload_bytes"] + config.TOOL_STDERR_BYTES)
        if code:
            raise ValueError(error.strip()[:500] or "The content parser exited without usable results.")
        parsed = json.loads(output)
        result["records"] = parsed.pop("records")
        records_step.update({key: parsed[key] for key in ("id", "label", "parser")})
        coverage_tools.mark(records_step, parsed["status"], parsed["reason"], parsed["detail"],
                            processed=parsed["processed"], total=parsed["total"],
                            **({"counts": parsed["counts"]} if "counts" in parsed else {}))
        result["metadata"]["format"] = (parsed["label"] if parsed["status"] in {"complete", "partial"}
                                        else candidate)
        if parsed["status"] != "complete":
            result["warnings"].append(parsed["detail"])
    except (OSError, ValueError) as error:
        coverage_tools.mark(records_step, "failed", getattr(error, "reason", "content_parser_error"), str(error)[:600])
        result["warnings"].append(f"{label} was not parsed: {str(error)[:500]}")


def inspect_file(path, name, result, settings, work_dir):
    content = coverage_tools.find(result["coverage"], "content")
    coverage_tools.mark(content, "running", "parsing", "Reading file metadata.")
    suffix = Path(name).suffix.lower()
    size = path.stat().st_size
    with path.open("rb") as source:
        sample = source.read(65536)
    meta = result["metadata"]
    meta["mime_type_hint"] = mimetypes.guess_type(name)[0] or "application/octet-stream"
    meta["mime_note"] = "MIME hint is based on the filename, not verified content type."
    # Signatures take precedence over filename hints, including deliberately misleading names.
    if sample.startswith(b"ElfFile\0"):
        parser = "windows-event-log"
    elif sample.startswith(b"regf"):
        parser = "windows-registry"
    elif sample.startswith(b"SQLite format 3\0"):
        parser = "browser-history"
    elif suffix == ".evtx":
        parser = "windows-event-log"
    elif name.casefold() in {"ntuser.dat", "system", "software"}:
        parser = "windows-registry"
    elif name.casefold() == "history" or suffix in {".db", ".sqlite", ".sqlite3"}:
        parser = "browser-history"
    else:
        parser = None
    if parser == "windows-event-log":
        coverage_tools.mark(content, "complete", "evtx_candidate",
                            "File metadata recorded; EVTX structure is checked by the event-log parser.", processed=len(sample))
        inspect_records(path, result, settings, work_dir, parser)
    elif parser == "windows-registry":
        coverage_tools.mark(content, "complete", "registry_candidate",
                            "File metadata recorded; hive structure is checked by the Registry parser.", processed=len(sample))
        inspect_records(path, result, settings, work_dir, "windows-registry")
    elif parser == "browser-history":
        coverage_tools.mark(content, "complete", "sqlite_candidate", "File metadata recorded; database schema is checked by the history parser.", processed=len(sample))
        inspect_records(path, result, settings, work_dir)
    elif suffix == ".json":
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
              "artifacts": [], "records": [], "partitions": [], "warnings": []}
    coverage = result["coverage"] = coverage_tools.begin(job["kind"], job["size"], settings, PARSER_VERSION, job.get("run_id"))
    integrity = coverage_tools.find(coverage, "hash")
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
        result["artifacts"].append({"key": "source", "path": job["name"], "kind": "source", "size": size,
                                    "details": {"sha256": result["sha256"], "parser": PARSER_VERSION}})
        if job["kind"] == "raw_image":
            inspect_image(path, job, result, settings, work_dir)
        else:
            report(work_dir, "Parsing file metadata", 70)
            inspect_file(path, job["name"], result, settings, work_dir)
        report(work_dir, "Saving findings", 95)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"[:2000]
        for item in coverage["steps"]:
            if item["status"] == "running":
                coverage_tools.mark(item, "failed", getattr(error, "reason", "parser_error"), result["error"])
    coverage_tools.finish(coverage, result.get("error"))
    return result
