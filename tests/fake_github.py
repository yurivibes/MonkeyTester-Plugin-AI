#!/usr/bin/env python3
"""Stands in for github.com, api.github.com and the Copilot API in one server.

In-process for tests (FakeGitHub), or standalone for a scratch instance:
    python3 tests/fake_github.py <port>
then serve with VTAI_GITHUB_URL=VTAI_GITHUB_API=http://127.0.0.1:<port>. The token
exchange points the Copilot API back at this same server.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OAUTH_TOKEN = "gho_fakeOAuthToken0000abcd"
LOGIN = "octocat"
USER_CODE = "ABCD-1234"
REPLY = '{"found": true, "coordinates": [100, 200]}'
MODELS = [
    {"id": "gpt-4o", "name": "GPT-4o", "vendor": "Azure OpenAI",
     "capabilities": {"type": "chat", "supports": {"vision": True}}},
    {"id": "claude-sonnet-4", "name": "Claude Sonnet 4", "vendor": "Anthropic",
     "capabilities": {"type": "chat", "supports": {"vision": True}}},
    {"id": "o3-mini", "name": "o3-mini", "vendor": "Azure OpenAI",
     "capabilities": {"type": "chat", "supports": {}}},
    {"id": "text-embedding-3-small", "name": "Embedding", "vendor": "Azure OpenAI",
     "capabilities": {"type": "embeddings", "supports": {"vision": True}}},
    {"id": "gpt-4.1-hidden", "name": "Hidden", "vendor": "Azure OpenAI",
     "model_picker_enabled": False,
     "capabilities": {"type": "chat", "supports": {"vision": True}}},
]


class FakeState:
    def __init__(self):
        # Replies to successive POST /login/oauth/access_token calls; the last repeats.
        self.oauth_replies = [{"error": "authorization_pending"},
                              {"error": "slow_down", "interval": 10},
                              {"access_token": OAUTH_TOKEN, "token_type": "bearer"}]
        self.has_copilot = True
        self.issued = []          # Copilot tokens handed out, newest last
        self.revoked = set()      # Copilot tokens the chat endpoint answers 401 to
        self.token_requests = []  # header dicts of each token exchange
        self.chats = []           # {"headers", "body"} of each chat request
        self.chat_status = 200
        self.base_url = ""


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _copilot_ok(self):
            auth = self.headers.get("Authorization", "")
            token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
            if token not in state.issued or token in state.revoked:
                self._json(401, {"error": {"message": "unauthorized: token expired"}})
                return False
            return True

        def do_GET(self):
            if self.path == "/user":
                if self.headers.get("Authorization") != f"token {OAUTH_TOKEN}":
                    self._json(401, {"message": "Bad credentials"})
                else:
                    self._json(200, {"login": LOGIN, "id": 1})
            elif self.path == "/copilot_internal/v2/token":
                state.token_requests.append(dict(self.headers))
                if self.headers.get("Authorization") != f"token {OAUTH_TOKEN}":
                    self._json(401, {"message": "Bad credentials"})
                elif not state.has_copilot:
                    self._json(403, {"message": "Resource not accessible by integration"})
                else:
                    token = f"tid=fake;exp={len(state.issued) + 1}"
                    state.issued.append(token)
                    self._json(200, {"token": token, "expires_at": int(time.time()) + 1800,
                                     "endpoints": {"api": state.base_url}})
            elif self.path == "/models":
                if self._copilot_ok():
                    self._json(200, {"data": MODELS})
            else:
                self._json(404, {"message": "Not Found"})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/login/device/code":
                self._json(200, {"device_code": "dev-123", "user_code": USER_CODE,
                                 "verification_uri": f"{state.base_url}/login/device",
                                 "interval": 5, "expires_in": 900})
            elif self.path == "/login/oauth/access_token":
                reply = state.oauth_replies.pop(0) if len(state.oauth_replies) > 1 \
                    else state.oauth_replies[0]
                self._json(200, reply)
            elif self.path == "/chat/completions":
                if not self._copilot_ok():
                    return
                body = json.loads(raw)
                state.chats.append({"headers": dict(self.headers), "body": body})
                if state.chat_status != 200:
                    self._json(state.chat_status, {"error": {"message": "copilot says no"}})
                else:
                    self._json(200, {"model": body.get("model"), "choices": [
                        {"message": {"role": "assistant", "content": REPLY}}]})
            else:
                self._json(404, {"message": "Not Found"})

        def log_message(self, *_args):
            pass

    return Handler


class FakeGitHub:
    def __init__(self, port=0):
        self.state = FakeState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.state))
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.state.base_url = self.url
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    fake = FakeGitHub(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    print(f"fake GitHub/Copilot on {fake.url}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
