#!/usr/bin/env python3
"""Stands in for Ollama: in-process for gateway tests, or as the `ollama` binary."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "qwen2.5vl:7b-q4_K_M"
MODEL2 = "qwen2.5vl:3b-q4_K_M"
DIGEST = "5ced39dfa4ba0000"
DIGEST2 = "fb90415cde1e0000"
DEFAULT_REPLY = '{"found": true, "coordinates": [100, 200]}'


class FakeState:
    def __init__(self):
        self.chats = []
        self.other_paths = []
        self.reply = DEFAULT_REPLY
        self.chat_status = 200
        self.digest = DIGEST
        self.models = [{"name": MODEL, "digest": DIGEST}, {"name": MODEL2, "digest": DIGEST2}]
        self.loaded = [MODEL]


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/version":
                self._json(200, {"version": "fake"})
            elif self.path == "/api/tags":
                # Real Ollama lists installed models only here; there is no /api/models.
                # The pinned model reports state.digest so tests can break the digest check.
                self._json(200, {"models": [
                    dict(m, digest=state.digest) if m["name"] == MODEL else m
                    for m in state.models]})
            elif self.path == "/api/ps":
                self._json(200, {"models": [{"name": name} for name in state.loaded]})
            else:
                state.other_paths.append(self.path)
                self._json(404, {"error": "not found"})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/api/generate":
                self._json(200, {"done": True})
            elif self.path == "/v1/chat/completions":
                state.chats.append(json.loads(body))
                if state.chat_status != 200:
                    self._json(state.chat_status, {"error": {"message": "boom"}})
                else:
                    self._json(200, {"choices": [{"message": {"role": "assistant", "content": state.reply}}]})
            else:
                state.other_paths.append(self.path)
                self._json(404, {"error": "not found"})

        def log_message(self, *_args):
            pass

    return Handler


class FakeOllama:
    def __init__(self):
        self.state = FakeState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "pull":
        sys.exit(0)
    if command == "serve":
        host, port = os.environ["OLLAMA_HOST"].split(":")
        ThreadingHTTPServer((host, int(port)), make_handler(FakeState())).serve_forever()
    sys.exit(2)
