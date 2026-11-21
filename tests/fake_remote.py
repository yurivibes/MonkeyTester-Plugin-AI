#!/usr/bin/env python3
"""Stands in for an OpenAI-compatible model server such as llama.cpp's llama-server.

Serves ONLY the two routes the real server has and the remote provider uses:
GET /v1/models ({"object": "list", "data": [{"id", ...}]}) and POST /v1/chat/completions.
Everything else is 404, so a test can't pass by relying on a route a real server lacks.
With `required_key` set it answers 401 with llama.cpp's error body, like a llama-server
started with --api-key. Every request is recorded with its headers.

Run standalone for a scratch instance: python3 tests/fake_remote.py <port> [key]
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REMOTE_MODEL = "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
REMOTE_MODEL2 = "gemma-3-12b-it-Q4_K_M.gguf"
REMOTE_REPLY = '{"found": true, "coordinates": [100, 200]}'


class RemoteState:
    def __init__(self, required_key=""):
        self.required_key = required_key
        self.models = [REMOTE_MODEL, REMOTE_MODEL2]
        self.reply = REMOTE_REPLY
        self.chat_status = 200
        self.requests = []   # {"method", "path", "headers", "body"}
        self.chats = []      # {"body", "headers"}

    def model_requests(self):
        return [r for r in self.requests if r["path"] == "/v1/models"]


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _record(self, body=None):
            state.requests.append({"method": self.command, "path": self.path,
                                   "headers": dict(self.headers.items()), "body": body})

        def _key_ok(self):
            if not state.required_key:
                return True
            if self.headers.get("Authorization", "") == f"Bearer {state.required_key}":
                return True
            # llama.cpp's reply to a missing or wrong --api-key.
            self._json(401, {"error": {"code": 401, "message": "Invalid API Key",
                                       "type": "authentication_error"}})
            return False

        def do_GET(self):
            self._record()
            if self.path != "/v1/models":
                self._json(404, {"error": {"code": 404, "message": "File Not Found",
                                           "type": "not_found_error"}})
                return
            if not self._key_ok():
                return
            created = int(time.time())
            self._json(200, {"object": "list", "data": [
                {"id": name, "object": "model", "created": created, "owned_by": "llamacpp",
                 "meta": {"n_ctx_train": 128000}} for name in state.models]})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw) if raw else None
            self._record(body)
            if self.path != "/v1/chat/completions":
                self._json(404, {"error": {"code": 404, "message": "File Not Found",
                                           "type": "not_found_error"}})
                return
            if not self._key_ok():
                return
            state.chats.append({"body": body, "headers": dict(self.headers.items())})
            if state.chat_status != 200:
                self._json(state.chat_status, {"error": {
                    "code": state.chat_status, "message": "remote says no",
                    "type": "server_error"}})
                return
            self._json(200, {"id": "chatcmpl-fake", "object": "chat.completion",
                             "created": int(time.time()), "model": body.get("model"),
                             "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant",
                                                      "content": state.reply}}]})

        def log_message(self, *_args):
            pass

    return Handler


class FakeRemote:
    def __init__(self, required_key="", port=0):
        self.state = RemoteState(required_key)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.state))
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1])
    key = sys.argv[2] if len(sys.argv) > 2 else ""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(RemoteState(key)))
    httpd.serve_forever()
