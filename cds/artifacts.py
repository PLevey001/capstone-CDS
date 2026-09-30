"""Download rules shared by artifact listings and extraction."""

import re


def download_status(artifact, source_kind, current_run_id):
    reason = None
    if artifact["run_id"] != current_run_id:
        reason = "Downloads are available from the current saved run only. Select the latest result to download."
    elif artifact["kind"] == "directory":
        reason = "Directories have no file contents to extract."
    elif source_kind == "raw_image":
        address = artifact.get("metadata_address")
        if address is None or not re.fullmatch(r"[0-9]+(?:-[0-9]+)*", str(address)):
            reason = "This artifact has no usable metadata address to extract."
    return {"available": reason is None, "reason": reason}
