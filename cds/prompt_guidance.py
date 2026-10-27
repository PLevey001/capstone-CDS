"""Fixed, trusted runtime guidance. Read once at import; restart after edits."""
import hashlib
from dataclasses import dataclass
from pathlib import Path

GUIDANCE_VERSION = "0.2"
BRIEF_VERSION = "case-assessment.v5"
CHAT_VERSION = "learning.v4"
_DIRECTORY = Path(__file__).parent / "prompts"
_COMPOSITIONS = {
    "brief": ("common.txt", "case-brief.txt"),
    "concepts": ("common.txt", "chat-common.txt", "chat-concepts.txt"),
    "case": ("common.txt", "chat-common.txt", "chat-case.txt"),
    "prior_assessment": ("common.txt", "chat-common.txt", "chat-case.txt", "chat-prior-assessment.txt"),
}
_FILENAMES = frozenset(name for names in _COMPOSITIONS.values() for name in names)


def _read_file(name):
    if name not in _FILENAMES:
        raise ValueError("Unknown guidance filename.")
    path = _DIRECTORY / name
    if path.is_symlink():
        raise RuntimeError(f"CDS guidance file {name} must not be a symlink.")
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise RuntimeError(f"Cannot read CDS guidance file {name} as UTF-8. Restore it and restart CDS.") from error
    if not text:
        raise RuntimeError(f"CDS guidance file {name} is blank. Restore it and restart CDS.")
    return text


@dataclass(frozen=True)
class Prompt:
    text: str
    version: str

    def metadata(self):
        return {"prompt_version": self.version, "prompt_sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
                "guidance_version": GUIDANCE_VERSION}


_TEXTS = {name: _read_file(name) for name in sorted(_FILENAMES)}
_PROMPTS = {operation: Prompt("\n\n".join(_TEXTS[name] for name in names),
                             BRIEF_VERSION if operation == "brief" else CHAT_VERSION)
            for operation, names in _COMPOSITIONS.items()}


def get_prompt(operation):
    """Application operation only, never a filename or a request/environment path."""
    if operation not in _PROMPTS:
        raise ValueError("Unknown guidance operation.")
    return _PROMPTS[operation]
