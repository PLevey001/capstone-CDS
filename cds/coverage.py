"""Coverage describes the scope examined, independently of job progress."""

from datetime import datetime, timezone
from uuid import uuid4


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def step(identifier, label, parser, **scope):
    return {"id": identifier, "label": label, "parser": parser, "status": "skipped",
            "reason": "not_reached", "detail": "Not reached in this analysis.",
            "processed": 0, "total": None, "unit": "records", **scope}


def mark(item, status, reason, detail, **counts):
    item.update(status=status, reason=reason, detail=detail, **counts)


def begin(kind, size, settings, parser, run_id=None):
    image = kind == "raw_image"
    steps = [step("hash", "Source integrity · SHA-256", parser, total=size, unit="bytes")]
    if image:
        steps.extend([
            step("partitions", "Partition discovery", "sleuthkit/mmls", unit="partitions"),
            step("inventory", "Filesystem inventory", "sleuthkit/fls"),
        ])
    else:
        steps.append(step("content", "File metadata extraction", parser, total=size, unit="bytes"))
    return {"schema_version": 1, "run_id": run_id or str(uuid4()), "status": "unknown",
            "started_at": timestamp(), "finished_at": None,
            "scope": ("Source hash, partition discovery, and filesystem directory entries. "
                      "File contents, unallocated space, and historical activity are not examined."
                      if image else "Source hash and supported file metadata. No event interpretation or forensic conclusions."),
            "limits": {**settings, "text_sample_bytes": 65536, "json_bytes": 4 * 1024**2,
                       "tool_output_bytes": 16 * 1024**2, "partitions": 128,
                       "json_keys": 50, "csv_columns": 100, "metadata_text_chars": 256}, "steps": steps}


def finish(coverage, error=None):
    for item in coverage["steps"]:
        if item["status"] == "running":
            mark(item, "failed", "analysis_error", error or "Analysis ended before this step finished.")
    coverage["status"] = ("failed" if error else "complete" if all(
        item["status"] == "complete" for item in coverage["steps"]) else "partial")
    coverage["finished_at"] = timestamp()
