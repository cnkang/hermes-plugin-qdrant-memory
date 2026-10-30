"""Deterministic loopback embedding and extraction providers for soak runs.

The soak measures the Runtime, the Gateway, the ledger and a real Qdrant. Model
quality is out of scope, so both providers are deterministic local stand-ins:

* the embedding provider maps tokens onto a fixed hashed vector space, which
  keeps exact-content checks meaningful and makes retrieval reproducible;
* the extraction provider returns the user text as one durable fact and answers
  every relation question without contacting a model.

Both speak the OpenAI-compatible HTTP surface that the Runtime already uses, so
no product code path is bypassed or stubbed inside the Owner.
"""

import hashlib
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIMENSIONS = 96
TOKEN = re.compile(r"[a-z0-9]+")


def embed(text, dimensions=DIMENSIONS):
    """Map one text onto a deterministic, L2-normalised hashed vector."""
    vector = [0.0] * dimensions
    for token in TOKEN.findall((text or "").lower()):
        digest = hashlib.sha256(token.encode()).hexdigest()
        index = int(digest[:8], 16) % dimensions
        vector[index] += 1.0 if int(digest[8:10], 16) % 2 else -1.0
    if not any(vector):
        vector[0] = 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _relation(payload):
    """Answer one relation question without inferring anything."""
    old = payload.get("old")
    new = payload.get("new")
    texts = [item for item in (old, new) if isinstance(item, str)]
    same = len(texts) == 2 and " ".join(texts[0].split()) == " ".join(texts[1].split())
    return {"relation": "SAME" if same else "UNRELATED"}


def _memories(payload):
    """Return the user statement as one durable fact, or nothing."""
    user = payload.get("user") if isinstance(payload.get("user"), str) else ""
    user = " ".join(user.split())
    memories = []
    if user:
        memories.append(
            {
                "text": user,
                "category": ["fact"],
                "importance": 0.6,
                "replacement_intent": False,
            }
        )
    return {"memories": memories}


def _structured(schema, payload):
    """Answer the structured request the Owner asked for."""
    properties = (schema or {}).get("properties", {})
    return _relation(payload) if "relation" in properties else _memories(payload)


class _Server(ThreadingHTTPServer):
    """Carry the provider request counter with the server instance."""

    daemon_threads = True

    def __init__(self, address, handler):
        """Create the counter before the server starts accepting connections."""
        self.counter = 0
        self.counter_lock = threading.Lock()
        super().__init__(address, handler)


class _Handler(BaseHTTPRequestHandler):
    """Serve the two OpenAI-compatible routes the Runtime uses."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        """Keep provider chatter out of the runner log."""

    def do_GET(self):
        """Report readiness for the environment probe."""
        if self.path != "/health":
            self.send_error(404)
            return
        self._reply(200, {"ok": True, "requests": self.server.counter})

    def do_POST(self):
        """Answer one embeddings or chat-completion request."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if not 0 < length <= 4 * 1024 * 1024:
            self.send_error(400)
            return
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            self.send_error(400)
            return
        with self.server.counter_lock:
            self.server.counter += 1
        if self.path.endswith("/embeddings"):
            self._reply(200, self._embeddings(body))
        elif self.path.endswith("/chat/completions"):
            self._reply(200, self._completion(body))
        else:
            self.send_error(404)

    @staticmethod
    def _embeddings(body):
        """Return one vector per input string, preserving input order."""
        inputs = body.get("input") or []
        if isinstance(inputs, str):
            inputs = [inputs]
        return {
            "object": "list",
            "model": body.get("model", "soak-embedding"),
            "data": [
                {"object": "embedding", "index": index, "embedding": embed(text)}
                for index, text in enumerate(inputs)
            ],
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    @staticmethod
    def _completion(body):
        """Return one structured completion for the requested schema."""
        messages = body.get("messages") or []
        content = messages[-1].get("content", "") if messages else ""
        try:
            value = json.loads(content)
        except ValueError:
            value = {}
        if isinstance(value, list) and value and isinstance(value[0], dict):
            try:
                value = json.loads(value[0].get("text", "{}"))
            except ValueError:
                value = {}
        schema = ((body.get("response_format") or {}).get("json_schema") or {}).get("schema")
        if schema is None:
            tools = body.get("tools") or []
            if tools:
                schema = tools[0].get("function", {}).get("parameters")
        answer = _structured(schema, value if isinstance(value, dict) else {})
        return {
            "id": "soak-completion",
            "object": "chat.completion",
            "model": body.get("model", "soak-extractor"),
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(answer)},
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def _reply(self, status, value):
        """Send a bounded JSON response and close the connection."""
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)


class ProviderStub:
    """Run both providers on one loopback port until the run ends."""

    def __init__(self, host="127.0.0.1", port=0):
        """Bind the stub immediately so the Owner config can name the port."""
        self.server = _Server((host, port), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="soak-provider")
        self.thread.daemon = True

    @property
    def port(self):
        """Return the bound loopback port."""
        return self.server.server_address[1]

    @property
    def base_url(self):
        """Return the HTTP base URL the Runtime config needs."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def requests(self):
        """Return how many provider requests the run issued."""
        return self.server.counter

    def start(self):
        """Start serving in a background thread."""
        self.thread.start()
        return self

    def close(self):
        """Stop serving and release the port."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)

    def __enter__(self):
        """Allow ``with ProviderStub() as stub``."""
        return self.start()

    def __exit__(self, *exc_info):
        """Stop the provider on the way out."""
        self.close()
