"""Bounded Ollama transport. No evidence access, proxies, redirects, or cloud fallback."""
import http.client
import json
import re
import socket
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cds.config import loopback_http_url

RESPONSE_BYTES = 256 * 1024
REQUEST_BYTES = 128 * 1024
OUTPUT_BYTES = 16 * 1024
STATUS_TIMEOUT = 3
OUTPUT_TOKENS = 900
BRIEF_OUTPUT_TOKENS = 1800
TEMPLATE_RESERVE = 1024


class ModelError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def call(base_url, path, payload=None, timeout=STATUS_TIMEOUT, deadline=None):
    """One request with a wall-clock deadline, including a slow/dribbling reply."""
    if not loopback_http_url(base_url):
        raise ModelError(503, "The model server address is not on this machine.")
    parts = urlsplit(base_url)
    host = "127.0.0.1" if parts.hostname == "localhost" else parts.hostname
    body = None if payload is None else encoded(payload)
    if body is not None and len(body) > REQUEST_BYTES:
        raise ModelError(422, "The local model request is too large. Clear the conversation or shorten the question.")
    deadline = deadline if deadline is not None else time.monotonic() + timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelError(504, "The local AI operation exceeded its time limit. Try again.")
    connection = http.client.HTTPConnection(host, parts.port or 80, timeout=remaining)
    expired = threading.Event()
    active_socket = None

    def expire():
        expired.set()
        # Interrupt header/body reads even if the server continually sends bytes.
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    timer.start()
    response = None
    try:
        connection.connect()
        active_socket = connection.sock
        if expired.is_set() or time.monotonic() >= deadline:
            raise ModelError(504, "The local AI operation exceeded its time limit. Try again.")
        connection.request("GET" if body is None else "POST", path, body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(RESPONSE_BYTES + 1)
        if expired.is_set() or time.monotonic() >= deadline:
            raise ModelError(504, "The local AI operation exceeded its time limit. Try again.")
        if len(raw) > RESPONSE_BYTES:
            raise ModelError(502, "The local model server sent more data than CDS accepts.")
        if response.length not in (None, 0):
            raise ModelError(502, "The local model server closed the connection before finishing its reply.")
        if response.status != 200:
            # Do not echo arbitrary server output into an error or retry a 400.
            status = 503 if response.status in (404, 503) else 502
            raise ModelError(status, f"The local model server answered with an error ({response.status}).")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ModelError(502, "The local model server sent a reply that is not JSON.") from error
        if not isinstance(data, dict) or "error" in data:
            raise ModelError(502, "The local model server sent an unexpected reply or reported an error.")
        return data
    except (OSError, http.client.HTTPException) as error:
        if expired.is_set() or isinstance(error, TimeoutError) or time.monotonic() >= deadline:
            raise ModelError(504, "The local AI operation exceeded its time limit. Try again.") from error
        if response is not None or isinstance(error, http.client.HTTPException):
            raise ModelError(502, "The local model server closed the connection before finishing its reply.") from error
        raise ModelError(503, "No local model server is answering on this machine. Start Ollama, then try again.") from error
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def model_names(model):
    return {model} | ({model + ":latest"} if ":" not in model else set())


def remote_model(entry):
    names = (entry.get("name", ""), entry.get("model", ""), entry.get("remote_model", ""))
    return bool(entry.get("remote_host") or entry.get("remote_model") or
                any(re.search(r"(^|[-/:])cloud($|[-/:])", str(name), re.I) for name in names))


def discover(settings, deadline):
    wanted = model_names(settings.ai_model)
    remote_reason = (f"{settings.ai_model} runs on a remote service, not on this machine. "
                     "CDS only uses a model that runs locally.")
    if remote_model({"name": settings.ai_model}):
        raise ModelError(503, remote_reason)
    models = call(settings.ai_url, "/api/tags", deadline=deadline).get("models")
    entry = next((item for item in models if isinstance(item, dict) and
                  (item.get("name") in wanted or item.get("model") in wanted)), None) if isinstance(models, list) else None
    if entry is None:
        raise ModelError(503, f"The model {settings.ai_model} is not installed. Run: ollama pull {settings.ai_model}")
    if remote_model(entry):
        raise ModelError(503, remote_reason)
    details = call(settings.ai_url, "/api/show", {"model": settings.ai_model}, deadline=deadline)
    if remote_model(details):
        raise ModelError(503, remote_reason)
    if not isinstance(details.get("details"), dict) or details["details"].get("format") != "gguf":
        raise ModelError(503, "CDS requires an installed local GGUF model with Ollama chat support.")
    capabilities = details.get("capabilities", [])
    if not isinstance(capabilities, list) or "completion" not in capabilities:
        raise ModelError(503, "The configured local model does not support chat completion.")
    installed_digest = entry.get("digest")
    return {"model_digest": installed_digest if isinstance(installed_digest, str) and len(installed_digest) <= 256 else None}


def status(settings, deadline=None):
    result = {"available": False, "model": settings.ai_model, "endpoint": settings.ai_url, "reason": None}
    try:
        probe_deadline = time.monotonic() + STATUS_TIMEOUT
        discover(settings, min(probe_deadline, deadline) if deadline is not None else probe_deadline)
        result["available"] = True
    except ModelError as error:
        result["reason"] = str(error)
    return result


def input_limit(settings, output_tokens=OUTPUT_TOKENS):
    # Conservative byte budget: at most one UTF-8 byte per input token, with
    # additional reserve for the chat template, schema, and output. Not a tokenizer.
    return settings.ai_context - output_tokens - TEMPLATE_RESERVE


def context_budget_guidance(settings):
    text = ("CDS uses a conservative request byte budget, not the model's token capacity. "
            "Clear history or shorten the question. For case questions, deselect the brief "
            "or review the full Case brief fact sheet.")
    if settings.ai_context < 32768:
        text += (" CDS_AI_CONTEXT can be increased up to 32768 if the installed model supports it; "
                 "this does not guarantee the request will fit.")
    return text


def chat(settings, messages, schema, output_tokens=OUTPUT_TOKENS):
    started = time.monotonic()
    deadline = started + settings.ai_timeout
    input_bytes = len(encoded({"messages": messages, "format": schema}))
    if input_bytes > input_limit(settings, output_tokens):
        raise ModelError(422, "Conversation exceeds the configured AI context budget. "
                         + context_budget_guidance(settings))
    discovery = discover(settings, deadline)
    runtime_version = None
    try:
        version = call(settings.ai_url, "/api/version", deadline=min(deadline, time.monotonic() + STATUS_TIMEOUT)).get("version")
        if isinstance(version, str) and len(version) <= 100:
            runtime_version = version
    except ModelError:
        pass  # Older runtimes may not expose a version; never invent one.
    reply = call(settings.ai_url, "/api/chat", {
        "model": settings.ai_model, "stream": False, "format": schema, "messages": messages,
        "options": {"temperature": 0, "seed": 0, "num_ctx": settings.ai_context, "num_predict": output_tokens},
    }, deadline=deadline)
    if reply.get("done_reason") == "length":
        raise ModelError(502, f"The local model reached its {output_tokens}-token output limit before finishing. "
                         "No partial answer was accepted. This is a generation limit, not a connection failure.")
    if reply.get("done") is not True or reply.get("done_reason") != "stop":
        raise ModelError(502, "The local model reply was incomplete or had no normal completion marker. "
                         "No partial answer was accepted.")
    if reply.get("model") not in model_names(settings.ai_model):
        raise ModelError(502, "The local model reply did not match the configured model.")
    message = reply.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("tool_calls"):
        raise ModelError(502, "The local model returned an unexpected message.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ModelError(502, "The local model returned an empty reply. Try again.")
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ModelError(502, "The local model answer contained invalid Unicode.") from error
    if len(content_bytes) > OUTPUT_BYTES:
        raise ModelError(502, "The local model answer exceeded the output limit.")
    return content, {"model": settings.ai_model, **discovery, "runtime_version": runtime_version,
                     "generation_options": {"temperature": 0, "seed": 0, "num_ctx": settings.ai_context,
                                            "num_predict": output_tokens, "timeout_seconds": settings.ai_timeout},
                     "reproducibility": "Recorded configuration and discovery metadata do not guarantee identical output on another run.",
                     "generated_at": datetime.now(timezone.utc).isoformat(),
                     "duration_ms": int((time.monotonic() - started) * 1000),
                     "context": {"input_bytes": input_bytes, "input_limit_bytes": input_limit(settings, output_tokens),
                                 "context_tokens": settings.ai_context, "output_tokens": output_tokens,
                                 "history_omitted": 0}}
