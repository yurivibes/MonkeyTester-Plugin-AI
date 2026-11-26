"""The "remote" provider: an OpenAI-compatible server at an address the user enters,
for example llama.cpp's llama-server on a GPU machine (http://host:8080).

Only two routes of that server are used, both part of the OpenAI API that llama.cpp,
vLLM and Ollama's /v1 implement: GET /v1/models (the model list, {"object": "list",
"data": [{"id": ...}]}) and POST /v1/chat/completions.

The address, the model and the resize switch are ordinary settings (settings.json, see
Gateway). The optional API key is a secret: it lives in <runtime>/remote.json
(MTAI_REMOTE_CONFIG overrides it), written atomically with mode 0600, is sent only as
"Authorization: Bearer <key>" to the remote server and is never returned by any route;
callers only see {configured, last4}.

Standard library only.
"""
from __future__ import annotations

import time
import urllib.parse
from pathlib import Path

from . import copilot as copilotmod

# Listing models runs inside GET /config and POST /config, so it must give up quickly
# when the remote machine is off or the address is wrong.
MODELS_TIMEOUT_SECS = 5
REJECTED_KEY = "the remote server rejected the API key"
_SUFFIXES = ("/v1/chat/completions", "/v1")


class RemoteError(Exception):
    """status is the remote HTTP status, or None when the network failed."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def normalize_url(value):
    """'http://h:8080/v1/' -> 'http://h:8080'. Raises ValueError for anything that is not
    http(s)://host[:port][/path]."""
    if not isinstance(value, str):
        raise ValueError("remote_url must be a string")
    url = value.strip().rstrip("/")
    for suffix in _SUFFIXES:
        if url.lower().endswith(suffix):
            url = url[:-len(suffix)].rstrip("/")
            break
    parts = urllib.parse.urlsplit(url)
    try:
        port_ok = parts.port is None or 0 < parts.port < 65536
    except ValueError:
        port_ok = False
    if (parts.scheme not in ("http", "https") or not parts.hostname or not port_ok
            or parts.query or parts.fragment or "@" in parts.netloc or " " in url):
        raise ValueError(f"remote_url must look like http://host:port (got {value!r})")
    return url


def auth_headers(key):
    return {"Authorization": f"Bearer {key}"} if key else {}


def list_models(url, key, timeout=None):
    """Model ids served at {url}/v1/models. Raises RemoteError with a message meant for
    people: the rejected-key message for 401/403, else 'could not reach ...: reason'."""
    try:
        reply = copilotmod.request_json(f"{url}/v1/models", headers=auth_headers(key),
                                        timeout=timeout or MODELS_TIMEOUT_SECS)
    except copilotmod.CopilotError as exc:
        if exc.status in (401, 403):
            hint = "" if key else " (no remote_api_key is set)"
            raise RemoteError(REJECTED_KEY + hint, status=exc.status)
        raise RemoteError(f"could not reach {url}/v1/models: {exc}", status=exc.status)
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, list):
        raise RemoteError(f"could not reach {url}/v1/models: the reply is not an "
                          "OpenAI model list (no \"data\" array)")
    ids = []
    for model in data:
        model_id = model.get("id") if isinstance(model, dict) else None
        if isinstance(model_id, str) and model_id and model_id not in ids:
            ids.append(model_id)
    return ids


class RemoteKey:
    """The optional API key of the remote server, stored by this plugin only."""

    def __init__(self, path):
        self.path = Path(path)

    def get(self):
        value = copilotmod.read_json(self.path).get("api_key")
        return value if isinstance(value, str) else ""

    def public(self):
        """{configured, last4}; never the key. last4 stays empty for keys shorter than 8
        characters, where four characters would give most of it away."""
        key = self.get()
        return {"configured": bool(key), "last4": key[-4:] if len(key) >= 8 else ""}

    def set(self, key):
        copilotmod.write_private_json(self.path, {
            "api_key": key, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")})

    def remove(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
