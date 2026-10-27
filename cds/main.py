import asyncio
import csv
import hashlib
import io
import json
import os
import shutil
import threading
import urllib.parse
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

import cds.case_brief as case_brief
from cds import learning, local_model
from cds.analysis import ToolLimitError, extract_artifact
from cds.config import Settings
from cds.coordinator import Coordinator
from cds.instance_lock import InstanceLock
from cds.store import CaseBusyError, Store, serialize_brief


def safe_filename(name):
    cleaned = "".join(c for c in name if c.isprintable() and c not in '"\\/')
    for marker in (" (deleted-realloc)", " (deleted)"):
        cleaned = cleaned.replace(marker, "")
    return cleaned.strip()[:200] or "extracted.bin"


def download_disposition(name):
    filename = safe_filename(name)
    fallback = "".join(char if char.isascii() else "_" for char in filename)
    header = f'attachment; filename="{fallback}"'
    if filename != fallback:
        header += "; filename*=UTF-8''" + urllib.parse.quote(filename, safe="")
    return header


def timeline_bound(value, name):
    if not value:
        return None
    try:
        bound = datetime.fromisoformat(value)
        if bound.utcoffset() is None:
            raise ValueError("Missing timezone")
        return bound.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise HTTPException(422, f"{name} must be a valid ISO-8601 timestamp with a timezone offset, "
                                 "for example 2026-01-01T00:00:00Z.") from error


def csv_record_value(value):
    # Keep saved text exact in JSON; spreadsheet exports escape formula-like cells.
    if isinstance(value, str) and (value.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@"))
                                   or value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return value


class CaseNameInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Case name cannot be blank")
        return value.strip()


class CaseInput(CaseNameInput):
    description: str = Field(default="", max_length=2000)


def create_app(settings=None, start_workers=True):
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir)
    coordinator = Coordinator(store, settings)
    instance_lock = InstanceLock(settings.data_dir)
    upload_slots = threading.BoundedSemaphore(3)
    case_mutations = threading.Lock()
    active_uploads = Counter()
    generation_slot = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        if start_workers:
            instance_lock.acquire()
        try:
            store.initialize()
            if start_workers:
                coordinator.start()
            yield
        finally:
            if start_workers:
                await asyncio.to_thread(coordinator.stop)
                instance_lock.release()

    request_header = APIKeyHeader(name="X-CDS-Request", auto_error=False,
                                  description="Enter local-ui for mutation requests. This is a local-browser guard, not an authentication secret.")
    app = FastAPI(title="CDS Forensics", version="0.1.0", lifespan=lifespan, dependencies=[Depends(request_header)])
    app.state.store = store
    app.state.coordinator = coordinator
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_mutations(request: Request, call_next):
        # Browser requests from another website cannot supply this header without
        # an approved CORS preflight. This local prototype does not enable CORS.
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("x-cds-request") != "local-ui":
            return JSONResponse({"detail": "Missing X-CDS-Request: local-ui header"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_case(case_id):
        if not store.case_exists(case_id):
            raise HTTPException(404, "Case not found")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "workers": settings.workers,
                "coordinator_running": bool(coordinator.thread and coordinator.thread.is_alive()),
                "sleuthkit_available": bool(shutil.which("fls") and shutil.which("mmls")),
                "max_upload_bytes": settings.max_upload_bytes, "max_artifacts": settings.max_artifacts}

    @app.get("/api/cases")
    def cases():
        return store.cases()

    @app.post("/api/cases", status_code=201)
    def create_case(body: CaseInput):
        return store.create_case(body.name, body.description)

    @app.patch("/api/cases/{case_id}")
    def rename_case(case_id: str, body: CaseNameInput):
        item = store.rename_case(case_id, body.name)
        if item is None:
            raise HTTPException(404, "Case not found")
        return item

    @app.delete("/api/cases/{case_id}")
    def delete_case(case_id: str):
        with case_mutations:
            if active_uploads[case_id]:
                raise HTTPException(409, "Wait for uploads to finish before deleting this case.")
            try:
                deleted = store.delete_case(case_id)
            except CaseBusyError as error:
                raise HTTPException(409, str(error)) from error
            if not deleted:
                raise HTTPException(404, "Case not found")
        pending = store.cleanup_deleted_files(case_id)
        return {"id": case_id, "cleanup_pending": pending}

    @app.get("/api/cases/{case_id}/evidence")
    def evidence(case_id: str):
        require_case(case_id)
        return store.evidence(case_id)

    @app.post("/api/cases/{case_id}/evidence", status_code=202)
    async def ingest(case_id: str, request: Request, filename: str = Query(min_length=1, max_length=255),
                     kind: str = Query(default="auto", pattern="^(auto|file|raw_image)$"),
                     sector_size: int = Query(default=512)):
        require_case(case_id)
        if sector_size not in (512, 4096):
            raise HTTPException(422, "Sector size must be 512 or 4096")
        name = filename.replace("\\", "/").split("/")[-1]
        if name in {"", ".", ".."} or any(ord(c) < 32 for c in name):
            raise HTTPException(422, "Invalid filename")
        if kind == "auto":
            kind = "raw_image" if Path(name).suffix.lower() in {".img", ".dd", ".raw"} else "file"
        if not upload_slots.acquire(blocking=False):
            raise HTTPException(429, "Three uploads are already active. Try again when one finishes.")
        evidence_id = str(uuid4())
        target = settings.data_dir / "evidence" / evidence_id
        partial = target.with_suffix(".part")
        size = 0
        registered = False
        tracking_upload = False
        try:
            # Recheck under the same lock used by deletion before streaming.
            with case_mutations:
                require_case(case_id)
                active_uploads[case_id] += 1
                tracking_upload = True
            with partial.open("xb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, f"File exceeds the {settings.max_upload_bytes}-byte upload limit")
                    await asyncio.to_thread(output.write, chunk)
                await asyncio.to_thread(output.flush)
                await asyncio.to_thread(os.fsync, output.fileno())
            partial.replace(target)
            target.chmod(0o400)
            await asyncio.to_thread(store.register, evidence_id, case_id, name, size, target, kind, sector_size)
            registered = True
            return {"id": evidence_id, "name": name, "size": size, "status": "queued"}
        finally:
            try:
                partial.unlink(missing_ok=True)
                if not registered:
                    target.unlink(missing_ok=True)
            finally:
                with case_mutations:
                    if tracking_upload:
                        active_uploads[case_id] -= 1
                        if not active_uploads[case_id]:
                            del active_uploads[case_id]
                upload_slots.release()

    @app.get("/api/evidence/{evidence_id}")
    def detail(evidence_id: str, run_id: str | None = Query(default=None, min_length=1, max_length=64)):
        item = store.detail(evidence_id, run_id)
        if item is None:
            raise HTTPException(404, "Evidence not found")
        return item

    @app.get("/api/evidence/{evidence_id}/runs")
    def runs(evidence_id: str):
        if store.detail(evidence_id) is None:
            raise HTTPException(404, "Evidence not found")
        return store.runs(evidence_id)

    @app.get("/api/evidence/{evidence_id}/artifacts/{artifact_id}")
    def artifact(evidence_id: str, artifact_id: int):
        item = store.artifact(evidence_id, artifact_id)
        if item is None:
            raise HTTPException(404, "Artifact not found for this evidence")
        return item

    @app.get("/api/evidence/{evidence_id}/artifacts/{artifact_id}/download")
    def download_artifact(evidence_id: str, artifact_id: int):
        """Extract one artifact's bytes; recovers deleted files from disk images."""
        target = store.extraction_target(evidence_id, artifact_id)
        if target is None:
            raise HTTPException(404, "Artifact not found for this evidence")
        try:
            data = extract_artifact(target, tool_timeout=settings.tool_timeout)
        except ToolLimitError as error:
            raise HTTPException(413, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        filename = target["path"].replace("\\", "/").rstrip("/").split("/")[-1]
        return Response(content=data, media_type="application/octet-stream",
                        headers={"Content-Disposition": download_disposition(filename)})

    @app.get("/api/evidence/{evidence_id}/artifacts")
    def artifacts(evidence_id: str, q: str = Query(default="", max_length=200),
                  offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
                  run_id: str | None = Query(default=None, min_length=1, max_length=64)):
        if store.detail(evidence_id, run_id) is None:
            raise HTTPException(404, "Evidence not found")
        return store.artifacts(evidence_id, q, offset, limit, run_id)

    @app.post("/api/evidence/{evidence_id}/retry", status_code=202)
    def retry(evidence_id: str):
        if store.detail(evidence_id) is None:
            raise HTTPException(404, "Evidence not found")
        if not store.retry(evidence_id):
            raise HTTPException(409, "Wait for the current analysis to finish before analyzing again")
        return {"status": "queued"}

    @app.get("/api/evidence/{evidence_id}/records")
    def records(evidence_id: str, q: str = Query(default="", max_length=200),
                offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
                run_id: str | None = Query(default=None, min_length=1, max_length=64)):
        if store.detail(evidence_id, run_id) is None:
            raise HTTPException(404, "Evidence or saved run not found")
        return store.records(evidence_id, q, offset, limit, run_id)

    @app.get("/api/evidence/{evidence_id}/records/{record_id}")
    def record(evidence_id: str, record_id: int):
        item = store.record(evidence_id, record_id)
        if item is None:
            raise HTTPException(404, "Record not found for this evidence")
        return item

    @app.get("/api/cases/{case_id}/records/export")
    def export_records(case_id: str, format: str = Query(default="json", pattern="^(csv|json)$"),
                       run_id: str | None = Query(default=None, min_length=1, max_length=64),
                       q: str = Query(default="", max_length=200)):
        """Export every matching saved record, independent of the visible page.

        CSV prefixes formula-like text cells with an apostrophe; JSON preserves saved values.
        Parser truncation and scope are retained in record details and saved coverage.
        """
        data = store.export_records(case_id, run_id, q)
        if data is None:
            raise HTTPException(404, "Case or saved run not found")
        stem = f"cds-records-{case_id[:8]}" + (f"-run-{run_id}" if run_id else "")
        if format == "json":
            return Response(json.dumps(data, indent=2), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{stem}.json"'})
        columns = ["id", "evidence_id", "run_id", "run_number", "artifact_id", "source_name", "source_sha256",
                   "artifact_path", "kind", "source_key", "at", "event_time_us", "summary", "parser",
                   "browser", "url", "title", "coverage_status", "coverage", "details"]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        coverage = {source["run_id"]: source["coverage"] for source in data["evidence"]}
        for item in data["items"]:
            row = {**item, **{key: item["details"].get(key) for key in ("browser", "url", "title")},
                   "coverage": json.dumps(coverage[item["run_id"]], ensure_ascii=True),
                   "details": json.dumps(item["details"], ensure_ascii=True)}
            writer.writerow({key: csv_record_value(value) for key, value in row.items()})
        return Response(buffer.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'})

    @app.get("/api/cases/{case_id}/audit")
    def audit(case_id: str):
        require_case(case_id)
        return store.audit(case_id)

    @app.get("/api/cases/{case_id}/timeline")
    def timeline(case_id: str, start: str = Query(default="", max_length=40),
                 end: str = Query(default="", max_length=40),
                 offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200),
                 revision: str = Query(default="", max_length=64)):
        """Filesystem timestamps and parsed-record times from a case's latest results.

        Optional start/end are ISO-8601 UTC bounds (inclusive), e.g. 2026-09-20T00:00:00+00:00.
        Supply the previous page's revision to reset pagination if saved results change.
        """
        require_case(case_id)
        start_time = timeline_bound(start, "start")
        end_time = timeline_bound(end, "end")
        if start_time is not None and end_time is not None and start_time > end_time:
            raise HTTPException(422, "start must be earlier than or equal to end.")
        data = store.timeline_rows(case_id, start_time, end_time, offset, limit, revision)
        if data is None:
            raise HTTPException(404, "Case not found")
        return data

    def fact_sheet(case_id):
        inputs = store.brief_inputs(case_id)
        if inputs is None:
            raise HTTPException(404, "Case not found")
        return case_brief.fact_sheet(inputs)

    @app.get("/api/ai/status")
    def ai_status():
        """Whether the configured local model is available for brief or chat generation."""
        return local_model.status(settings)

    @app.get("/api/cases/{case_id}/brief")
    def brief_facts(case_id: str):
        """Fact sheet computed from the latest saved results. No model is involved."""
        return fact_sheet(case_id)

    def brief_envelope(case_id, brief_id):
        saved = store.saved_brief(case_id, brief_id)
        if saved is None:
            raise HTTPException(404, "Saved brief not found in this case")
        return saved

    @app.get("/api/cases/{case_id}/briefs")
    def brief_history(case_id: str):
        history = store.saved_briefs(case_id)
        if history is None:
            raise HTTPException(404, "Case not found")
        return history

    @app.get("/api/cases/{case_id}/briefs/{brief_id}")
    def saved_brief(case_id: str, brief_id: str):
        saved = brief_envelope(case_id, brief_id)
        saved.pop("serialized")
        return saved

    @app.get("/api/cases/{case_id}/briefs/{brief_id}/download")
    def download_brief(case_id: str, brief_id: str):
        saved = brief_envelope(case_id, brief_id)
        document = saved["document"]
        # Generated ID/version only: case names and client paths never determine a download path.
        filename = f"case-brief-{document['id']}-v{document['version']}.json"
        return Response(saved["serialized"], media_type="application/json",
                        headers={"Content-Disposition": download_disposition(filename),
                                 "ETag": f'"{saved["document_sha256"]}"'})

    @app.post("/api/cases/{case_id}/brief")
    def brief(case_id: str):
        """Generate and transactionally save an immutable assessment; return its envelope."""
        sheet = fact_sheet(case_id)
        if not sheet["saved_sources"]:
            raise HTTPException(409, "There are no saved analysis results to summarize yet.")
        if not generation_slot.acquire(blocking=False):
            raise HTTPException(409, "A local AI answer is already being generated. Wait for it to finish.")
        try:
            result = case_brief.generate(settings, sheet["facts"])
            document = store.save_brief(sheet, result)
            # Fresh at the validated save transaction; subsequent changes are checked on reads.
            # Do not let another database read turn a committed save into a failed POST.
            return {"document": document, "stale": False,
                    "document_sha256": hashlib.sha256(serialize_brief(document).encode("utf-8")).hexdigest()}
        except local_model.ModelError as error:
            raise HTTPException(error.status, str(error)) from error
        finally:
            generation_slot.release()

    @app.get("/api/ai/glossary")
    def glossary():
        return learning.GLOSSARY

    def chat_answer(case_id, question):
        require_case(case_id)
        if not generation_slot.acquire(blocking=False):
            raise HTTPException(409, "A local AI answer is already being generated. Wait for it to finish.")
        try:
            sheet = fact_sheet(case_id) if question.mode == "case" else None
            assessment = None
            if question.brief_id:
                selected = brief_envelope(case_id, question.brief_id)
                if selected["stale"] or selected["document"]["input_digest"] != sheet["input_digest"]:
                    raise HTTPException(409, "Selected AI brief is stale. Regenerate or select a current version; clear the conversation. Viewing and downloading remain available.")
                assessment = selected["document"]
                sheet = assessment  # Pin F references and provenance to the selected historical snapshot.
            if sheet and question.facts_digest and question.facts_digest != sheet["facts_digest"]:
                raise HTTPException(409, "Case facts changed or belong to another case. Clear the conversation and try again.")
            result = learning.generate(settings, question, sheet, assessment)
            require_case(case_id)
            if sheet and fact_sheet(case_id)["input_digest"] != sheet["input_digest"]:
                raise HTTPException(409, "Case results changed during generation. Clear the conversation and try again.")
            # Chat text and references are never written to the audit log or Store.
            return result
        except local_model.ModelError as error:
            raise HTTPException(error.status, str(error)) from error
        finally:
            generation_slot.release()

    @app.post("/api/cases/{case_id}/chat")
    async def chat(case_id: str, request: Request):
        # Bound bytes before JSON parsing, including chunked requests without Content-Length.
        length = request.headers.get("content-length", "")
        if length and (not length.isascii() or not length.isdecimal()):
            raise HTTPException(422, "Content-Length must contain ASCII decimal digits.")
        if length and (len(length) > 8 or int(length) > learning.BODY_BYTES):
            raise HTTPException(422, "Chat request exceeds 16 KiB. Clear the conversation or shorten the question.")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > learning.BODY_BYTES:
                raise HTTPException(422, "Chat request exceeds 16 KiB. Clear the conversation or shorten the question.")
            body.extend(chunk)
        try:
            question = learning.ChatInput.model_validate_json(body)
        except ValidationError as error:
            # Do not echo user conversation in validation errors.
            raise HTTPException(422, "Use a nonblank question of at most 2000 UTF-8 bytes, concepts/case mode, "
                                     "and at most 8 history messages in user/assistant pairs. "
                                     "No extra fields are accepted. Clear the conversation if needed.") from error
        # The worker owns the slot until generation actually ends, even on disconnect.
        return await asyncio.to_thread(chat_answer, case_id, question)

    EXPORT_COLUMNS = [
        "evidence_id", "run_id", "run_number", "artifact_id", "job_status", "current_job_status",
        "coverage_status", "coverage_run_id",
        "source_name", "source_kind", "source_sha256", "imported_at",
        "artifact_path", "artifact_kind", "size", "deleted",
        "partition_offset", "metadata_address", "artifact_sha256", "parser",
    ]

    @app.get("/api/cases/{case_id}/export")
    def export_case(case_id: str, format: str = Query(default="csv", pattern="^(csv|json)$"),
                    run_id: str | None = Query(default=None, min_length=1, max_length=64)):
        """Download latest saved results, or one historical run belonging to this case."""
        require_case(case_id)
        data = store.export_rows(case_id, run_id)
        if data is None:
            raise HTTPException(404, "Case or analysis run not found")

        stem = f"cds-case-{case_id[:8]}" + (f"-run-{run_id}" if run_id else "")

        if format == "json":
            body = json.dumps(data, indent=2)
            media_type = "application/json"
            filename = f"{stem}.json"
        else:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data["items"])
            body = buffer.getvalue()
            media_type = "text/csv"
            filename = f"{stem}.csv"

        return Response(content=body, media_type=media_type,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    frontend = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if (frontend / "assets").exists():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        if not (frontend / "index.html").exists():
            return JSONResponse({"message": "Build the interface: cd frontend && npm install && npm run build", "api_docs": "/docs"})
        return FileResponse(frontend / "index.html")

    return app


app = create_app()
