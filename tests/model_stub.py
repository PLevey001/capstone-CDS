"""Scripted stand-in for the model server on this machine. Real HTTP, never a real model."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCAL_MODEL = {"name": "llama3.2:3b", "model": "llama3.2:3b", "size": 2019393189,
               "digest": "sha256:" + "a" * 64, "details": {"format": "gguf", "parameter_size": "3.2B"}}


def complete_reply(content, model="llama3.2:3b"):
    return {"model": model, "done": True, "done_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(content) if not isinstance(content, str) else content}}


def assessment(statement="The saved results describe evidence sources.", fact="F1"):
    return {"overall_summary": [{"statement": statement, "facts": [fact]}],
            "observations": [], "inferences": [], "review_next": []}


def cite_first_facts(payload):
    """Scripted categorized assessment or educational answer; never evaluates a real model."""
    if "answer" in payload["format"].get("properties", {}):
        concepts = "CONCEPT MODE" in payload["messages"][0]["content"]
        if "prior_UNVERIFIED_assessment_NOT_evidence" in payload["messages"][1]["content"]:
            return 200, complete_reply({"answer": [{"statement": "The prior suggestion is tentative and needs review.",
                "facts": ["A-overall_summary-1", "F1"]}], "cannot_answer": False})
        return 200, complete_reply({"answer": [{"statement": "Scripted general explanation." if concepts else "Scripted case observation.",
                                             "facts": ["G1" if concepts else "F1"]}], "cannot_answer": False})
    facts = dict(re.findall(r"^(F\d+): (.*)$", payload["messages"][-1]["content"], re.MULTILINE))
    result = assessment()
    inventory = next((ref for ref, text in facts.items() if text.startswith("The inventory of")), None)
    if inventory:
        result["observations"] = [{"statement": facts[inventory], "facts": [inventory]}]
        result["inferences"] = [{"statement": "The inventory may help focus a file review.", "facts": [inventory],
            "reasoning": "The saved inventory identifies file entries for further examination.",
            "alternatives": ["Relevant material may also exist outside the saved inventory."],
            "limitations": ["File entries do not establish an actor or intent."]}]
        result["review_next"] = [{"statement": "Review the saved inventory alongside its coverage limits.", "facts": [inventory]}]
    return 200, complete_reply(result)


class ModelStub:
    """Serves /api/tags and /api/chat on a loopback port chosen by the OS."""

    def __init__(self, reply=cite_first_facts, models=(LOCAL_MODEL,)):
        self.reply = reply
        self.models = list(models)
        self.requests = []
        self.shows = []
        self.show = {"details": {"format": "gguf"}, "capabilities": ["completion"]}
        self.gets = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, status, body, headers=()):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                stub.gets.append(self.path)
                if self.path == "/api/tags":
                    self.send_json(200, {"models": stub.models})
                elif self.path == "/api/version":
                    self.send_json(200, {"version": "synthetic-runtime"})
                else:
                    self.send_json(404, {"error": "not found"})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/show":
                    stub.shows.append(payload)
                    self.send_json(200, stub.show)
                    return
                stub.requests.append({"path": self.path, "payload": payload})
                result = stub.reply(payload)
                try:
                    self.send_json(*result)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The caller gave up waiting; that is the behavior under test.

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
