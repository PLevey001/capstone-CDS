import asyncio
import csv
import io
import json
import os
import shutil
import threading
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cds.config import Settings
from cds.coordinator import Coordinator
from cds.instance_lock import InstanceLock
from cds.store import CaseBusyError, Store


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
    def detail(evidence_id: str):
        item = store.detail(evidence_id)
        if item is None:
            raise HTTPException(404, "Evidence not found")
        return item

    @app.get("/api/evidence/{evidence_id}/artifacts")
    def artifacts(evidence_id: str, q: str = Query(default="", max_length=200),
                  offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200)):
        if store.detail(evidence_id) is None:
            raise HTTPException(404, "Evidence not found")
        return store.artifacts(evidence_id, q, offset, limit)

    @app.post("/api/evidence/{evidence_id}/retry", status_code=202)
    def retry(evidence_id: str):
        if store.detail(evidence_id) is None:
            raise HTTPException(404, "Evidence not found")
        if not store.retry(evidence_id):
            raise HTTPException(409, "Only failed jobs can be retried")
        return {"status": "queued"}

    @app.get("/api/cases/{case_id}/audit")
    def audit(case_id: str):
        require_case(case_id)
        return store.audit(case_id)

    EXPORT_COLUMNS = ["source_name", "source_kind", "source_sha256", "imported_at",
                      "artifact_path", "artifact_kind", "size", "deleted",
                      "partition_offset", "metadata_address", "artifact_sha256", "parser"]

    @app.get("/api/cases/{case_id}/export")
    def export_case(case_id: str, format: str = Query(default="csv", pattern="^(csv|json)$")):
        """Download every artifact in a case as CSV or JSON, with source hashes."""
        require_case(case_id)
        data = store.export_rows(case_id)
        if data is None:
            raise HTTPException(404, "Case not found")

        stem = f"cds-case-{case_id[:8]}"

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
