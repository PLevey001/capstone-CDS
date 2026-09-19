import os
from dataclasses import dataclass
from pathlib import Path


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
