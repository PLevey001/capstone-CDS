"""Pure synthetic saved inputs with repeated, whole source scopes. No local data."""
import json

from cds import case_brief


def repeated_scope_inputs(count=14):
    notes = [
        "Saved filesystem inventory only. Allocated file metadata was enumerated within the recorded limits; "
        "unallocated space, encrypted data and other filesystems were not examined. A completed step does not "
        "establish that every part of the source was examined.",
        "All supplied source bytes were hashed. This is the initial recorded analysis hash, not a comparison "
        "against an acquisition reference. The digest does not establish authenticity, safety, authorship or "
        "the truth of any content within the source.",
        "Supported browser databases were parsed from the saved snapshot only. Separate WAL and journal "
        "files were not supplied or examined. Visits can include synchronized or imported activity and do "
        "not identify the person using a device or prove deliberate viewing.",
        "Registry keys and values were parsed within the saved limits. A key last-write time describes "
        "changes to the containing key collectively; values have no independent change timestamp. These "
        "results cannot establish which entry changed or identify an actor.",
        "Windows event records were parsed within the saved limits. Interpret event identifiers with their "
        "provider, channel, version and recorded fields. Provider message templates were not rendered, "
        "and a record alone does not establish a person's intent or guilt.",
        "Deleted entries are inventory observations only. No recovery outcome is reported here. A deleted "
        "entry does not identify who deleted the file, when it was deleted, or why. Recovery depends on "
        "surviving metadata and content outside this bounded inventory.",
    ]
    sources = []
    for index in range(count):
        sources.append({
            "id": f"source-{index}", "name": f"synthetic-workstation-{index}.img", "kind": "raw_image",
            "size": 4096, "imported_at": "2026-01-01T00:00:00+00:00", "run_id": f"run-{index}",
            "active_run_id": f"run-{index}", "job_status": "completed", "run_number": 1,
            "run_status": "completed", "sha256": "ab12" * 16, "error": None, "warnings": [],
            "coverage": {"status": "complete", "scope": notes[0], "steps": [
                {"id": f"step-{number}", "status": "complete", "detail": note}
                for number, note in enumerate(notes[1:])]},
            "files": 8, "directories": 1, "deleted_total": 0, "deleted": [],
            "first_file_time": None, "last_file_time": None,
            "records": [{"kind": kind, "count": 7, "files": 1, "first": None, "last": None}
                        for kind in ("browser_visit", "registry_value")],
            "hosts": [], "host_total": 0, "events": [], "event_types_total": 0,
        })
    return {"case": {"id": "synthetic-case", "name": "Repeated scope regression"}, "sources": sources}


def selected_assessment(facts):
    detail = next(fact["id"] for fact in reversed(facts) if fact["kind"] == "records")
    omitted = next(fact for fact in facts if fact["kind"] == "inventory")
    return {"id": "synthetic-assessment", "version": 1,
            "context": {"facts_total": len(facts), "facts_sent": len(facts) - 1, "facts_omitted": 1,
                        "omitted_fact_ids": [omitted["id"]], "sources_omitted": 0, "omitted_source_ids": [],
                        "detail_omitted_source_ids": [omitted["source_id"]], "history_omitted": 0,
                        "model_fact_ids": [fact["id"] for fact in facts if fact["id"] != omitted["id"]],
                        "notice": "One detail was omitted from the original assessment context."},
            **case_brief.check_assessment(json.dumps({
                "overall_summary": [{"statement": "Saved results have limited scope.", "facts": ["F1"]}],
                "observations": [], "review_next": [], "inferences": [{
                    "statement": "A common origin is possible.", "facts": [detail],
                    "reasoning": "The saved observations could share an origin.",
                    "alternatives": ["Independent observations could agree."],
                    "limitations": ["These counts do not measure the amount of user activity."]}]}), facts)}
