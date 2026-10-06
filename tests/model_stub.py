"""Scripted stand-in for the model server on this machine. Real HTTP, never a real model."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCAL_MODEL = {"name": "llama3.2:3b", "model": "llama3.2:3b", "size": 2019393189,
               "details": {"format": "gguf", "parameter_size": "3.2B"}}


def cite_first_facts(payload):
    """Default reply: one overview statement per leading fact, then one review item."""
    ids = re.findall(r"^(F\d+): ", payload["messages"][-1]["content"], re.MULTILINE)
    overview = [{"statement": f"Scripted overview statement about {fact}.", "facts": [fact]} for fact in ids[:2]]
    review = [{"statement": "Scripted review item.", "facts": ids[-1:]}]
    return 200, {"message": {"role": "assistant", "content": json.dumps({"overview": overview, "review": review})}}


class ModelStub:
    """Serves /api/tags and /api/chat on a loopback port chosen by the OS."""

    def __init__(self, reply=cite_first_facts, models=(LOCAL_MODEL,)):
        self.reply = reply
        self.models = list(models)
        self.requests = []
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
                else:
                    self.send_json(404, {"error": "not found"})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
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
