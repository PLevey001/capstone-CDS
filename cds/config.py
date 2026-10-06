import ipaddress
import os
import urllib.parse
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


def loopback_http_url(value):
    """True only for http://<this machine>[:port] with no credentials, path, query or fragment."""
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "http" or parts.username or parts.password or parts.query or parts.fragment:
        return False
    if parts.path not in ("", "/") or (port is not None and port == 0):
        return False
    host = parts.hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    workers: int = 2
    max_upload_bytes: int = 4 * 1024**3
    max_artifacts: int = 20_000
    tool_timeout: int = 90
    # Optional case-brief wording. Facts are sent only to a model server on this machine.
    ai_url: str = "http://127.0.0.1:11434"
    ai_model: str = "llama3.2:3b"
    ai_timeout: int = 180

    def __post_init__(self):
        if not 1 <= self.workers <= 8:
            raise ValueError("CDS_WORKERS must be between 1 and 8")
        if min(self.max_upload_bytes, self.max_artifacts, self.tool_timeout, self.ai_timeout) <= 0:
            raise ValueError("Limits must be positive")
        if not loopback_http_url(self.ai_url):
            raise ValueError("CDS_AI_URL must be a plain http:// address on this machine, "
                             "for example http://127.0.0.1:11434")
        if not self.ai_model.strip() or len(self.ai_model) > 200:
            raise ValueError("CDS_AI_MODEL must name one installed local model")

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.environ.get("CDS_DATA_DIR", "data")).resolve(),
            workers=int(os.environ.get("CDS_WORKERS", "2")),
            max_upload_bytes=int(os.environ.get("CDS_MAX_UPLOAD_BYTES", str(4 * 1024**3))),
            max_artifacts=int(os.environ.get("CDS_MAX_ARTIFACTS", "20000")),
            tool_timeout=int(os.environ.get("CDS_TOOL_TIMEOUT", "90")),
            ai_url=os.environ.get("CDS_AI_URL", "http://127.0.0.1:11434"),
            ai_model=os.environ.get("CDS_AI_MODEL", "llama3.2:3b"),
            ai_timeout=int(os.environ.get("CDS_AI_TIMEOUT", "180")),
        )
