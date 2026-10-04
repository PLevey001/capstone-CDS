import os
from dataclasses import dataclass
from pathlib import Path

TOOL_OUTPUT_BYTES = 16 * 1024**2
TOOL_STDERR_BYTES = 64 * 1024

# Shared by the content parser, its process runner, and coverage reporting.
CONTENT_LIMITS = {
    "content_file_bytes": 64 * 1024**2,
    "parsed_records": 10_000,
    "record_payload_bytes": 16 * 1024**2,
    "record_text_chars": 4096,
    "content_timeout": 90,
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    workers: int = 2
    max_upload_bytes: int = 4 * 1024**3
    max_artifacts: int = 20_000
    tool_timeout: int = 90

    def __post_init__(self):
        if not 1 <= self.workers <= 8:
            raise ValueError("CDS_WORKERS must be between 1 and 8")
        if min(self.max_upload_bytes, self.max_artifacts, self.tool_timeout) <= 0:
            raise ValueError("Limits must be positive")

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.environ.get("CDS_DATA_DIR", "data")).resolve(),
            workers=int(os.environ.get("CDS_WORKERS", "2")),
            max_upload_bytes=int(os.environ.get("CDS_MAX_UPLOAD_BYTES", str(4 * 1024**3))),
            max_artifacts=int(os.environ.get("CDS_MAX_ARTIFACTS", "20000")),
            tool_timeout=int(os.environ.get("CDS_TOOL_TIMEOUT", "90")),
        )
