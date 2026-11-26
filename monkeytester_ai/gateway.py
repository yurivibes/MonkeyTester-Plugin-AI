"""The HTTP API MonkeyTester talks to. See API.md for the contract.

The service also implements the MonkeyTester device plugin contract
(PLUGIN_API.md in the MonkeyTester repo, contract_version 1): it publishes
/health, /devices, /actions and /jobs, and the plugin half of the API owns the
model choice. Plugin routes answer errors in the contract's flat shape
{"error": message, "code": code}; the MonkeyTester-Plugin-AI routes keep its own
nested shape from API.md.

Three providers answer chat requests: "local" (the private Ollama, with the Qwen
resize/rescale pipeline), "remote" (an OpenAI-compatible server at a user-entered
address, see remote.py; the Qwen pipeline is on unless remote_resize is false) and
"copilot" (GitHub Copilot, see copilot.py, passed through unchanged except for the
model). The choice of provider, each provider's model, the remote address and its
resize switch are persisted in <runtime>/settings.json.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import json
import os
import platform
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config as cfgmod
from . import copilot as copilotmod
from . import imaging, runtime
from . import remote as remotemod
from .docs_page import docs_html

MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 16                         # image parts per chat request
MAX_IMAGE_BYTES = 15 * 1024 * 1024      # one decoded image
UPSTREAM_TIMEOUT_SECS = 300
CLIENT_TIMEOUT_SECS = 60                # socket timeout per read/write with the HTTP client
LOOPBACK_NAMES = ("localhost", "localhost.", "ip6-localhost")
DATA_URL = re.compile(r"^data:image/[a-z+.-]+;base64,(.*)$", re.DOTALL | re.IGNORECASE)

DEVICE_ID = "gateway"
PLUGIN_CONTRACT = "monkeytester-plugin"
PLUGIN_CONTRACT_VERSION = 1
JOBS_KEPT_MS = 10 * 60 * 1000
JOB_TERMINAL = ("passed", "failed", "timeout", "error", "cancelled")
PROVIDERS = ("local", "remote", "copilot")
NOT_SIGNED_IN = "sign in to GitHub Copilot first"
REMOTE_NOT_SET = "set remote_url and remote_model first"
CONFIG_KEYS = {"model", "token", "port", "provider", "copilot_model",
               "remote_url", "remote_model", "remote_resize", "remote_api_key"}

ACTIONS = [
    {
        "name": "set-model",
        "description": "Choose the model of the active provider that answers future chat "
                       "requests: an installed Ollama model (local; the next request may "
                       "reload it), a model served by the remote server, or a vision "
                       "model of the signed-in Copilot account.",
        "params": {"model": {"type": "string", "required": True}},
        "default_timeout_ms": 60000,
        "needs_device": False,
    },
]
ACTION_NAMES = {action["name"] for action in ACTIONS}

# GET /docs renders the repo's README.md, read at request time and resolved from this
# file (not the CWD). The service runs from the checkout (flake.nix insists on it), so the
# README is normally there; when it isn't, /health omits "docs" and /docs answers 404.
README_PATH = Path(__file__).resolve().parent.parent / "README.md"
DOCS_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                               "form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


def readme_available():
    """Cheap check behind /health's "docs" field: README.md is a readable file."""
    return README_PATH.is_file() and os.access(README_PATH, os.R_OK)


def docs_page():
    """The rendered README.md for GET /docs, or None when it can't be read."""
    try:
        text = README_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return docs_html(cfgmod.SERVICE_NAME, text, cfgmod.SERVICE_VERSION)


class BadRequest(Exception):
    def __init__(self, message, status=400, code="invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


def is_loopback(host):
    """True when a bind address only listens on this machine."""
    host = (host or "").strip().strip("[]").lower()
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bind_error(host, token):
    """Why serving on `host` is refused, or None: any non-loopback bind needs MTAI_TOKEN
    (the gateway fronts Ollama, which has no auth of its own, and holds the Copilot
    sign-in and the remote API key)."""
    if (token or "").strip() or is_loopback(host):
        return None
    return (f"--lan (listening on {host}) requires MTAI_TOKEN, because Ollama has no auth "
            "of its own. Generate one with `python3 -c 'import secrets; "
            "print(secrets.token_hex(32))'`, then either export MTAI_TOKEN=<token> and run "
            "`bin/monkeytester-ai service install --lan` (writes it into the LaunchAgent "
            "plist's EnvironmentVariables), or add <key>MTAI_TOKEN</key> to that plist "
            "yourself. Enter the same token for the plugin in the MonkeyTester viewer's "
            "Settings. Without --lan the service listens on 127.0.0.1 only. See SECURITY.md.")


def parse_json(body):
    """The decoded JSON body, or BadRequest (also for invalid UTF-8 and nesting too
    deep for the parser)."""
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise BadRequest("body is not valid JSON") from None


def error_body(code, message):
    """The one MonkeyTester-Plugin-AI error shape, shared with the Mobile Controller. See API.md."""
    return {"error": {"code": code, "message": message}}


def contract_error(status, code, message):
    """The plugin contract's error shape. See PLUGIN_API.md in the MonkeyTester repo."""
    return status, {"error": message, "code": code}


class Gateway:
    def __init__(self, cfg, resizer=imaging.resize_with_sips, copilot=None):
        self.cfg = cfg
        self.default_model = cfg.model  # MTAI_MODEL: the fallback for a stale persisted model
        self.resizer = resizer
        self.copilot = copilot or copilotmod.CopilotClient(cfg.copilot_config)
        self.remote_key = remotemod.RemoteKey(cfg.remote_config)
        self.provider = "local"
        self.copilot_model = copilotmod.DEFAULT_MODEL
        self.remote_url = ""      # normalised, without /v1
        self.remote_model = ""
        self.remote_resize = True
        self.jobs = {}
        self._job_seq = 0
        self._jobs_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self._load_settings()

    # --- persisted selection ---

    def _load_settings(self):
        """settings.json wins over MTAI_MODEL; without a file nothing changes."""
        stored = copilotmod.read_json(self.cfg.settings_path)
        model = stored.get("model")
        if isinstance(model, str) and model:
            self.cfg = replace(self.cfg, model=model)
        copilot_model = stored.get("copilot_model")
        if isinstance(copilot_model, str) and copilot_model:
            self.copilot_model = copilot_model
        try:
            self.remote_url = remotemod.normalize_url(stored.get("remote_url") or "")
        except ValueError:
            self.remote_url = ""
        remote_model = stored.get("remote_model")
        if self.remote_url and isinstance(remote_model, str):
            self.remote_model = remote_model
        if isinstance(stored.get("remote_resize"), bool):
            self.remote_resize = stored["remote_resize"]
        provider = stored.get("provider")
        if provider == "copilot" and not self.copilot.configured():
            _log("provider copilot was selected but GitHub Copilot is not signed in; using local")
        elif provider == "remote" and not self.remote_ready():
            _log("provider remote was selected but no remote server address and model are "
                 "set; using local")
        elif provider in PROVIDERS:
            self.provider = provider

    def save_settings(self):
        with self._settings_lock:
            copilotmod.write_private_json(self.cfg.settings_path, {
                "provider": self.provider, "model": self.cfg.model,
                "copilot_model": self.copilot_model, "remote_url": self.remote_url,
                "remote_model": self.remote_model, "remote_resize": self.remote_resize})

    def reconcile_local_model(self):
        """After Ollama starts: a persisted local model that is gone falls back to MTAI_MODEL."""
        installed = self.installed_models()
        if installed and self.cfg.model not in installed:
            _log(f"selected model {self.cfg.model} is no longer installed; "
                 f"falling back to {self.default_model}")
            self.cfg = replace(self.cfg, model=self.default_model)

    @property
    def active_model(self):
        if self.provider == "copilot":
            return self.copilot_model
        if self.provider == "remote":
            return self.remote_model
        return self.cfg.model

    def remote_ready(self):
        """Config-only: an address and a model are set (no network, /health uses it)."""
        return bool(self.remote_url and self.remote_model)

    def _local_state(self):
        """(ollama reachable, local model loaded)."""
        try:
            return True, runtime.model_loaded(self.cfg)
        except (OSError, ValueError):
            return False, False

    def _usable(self):
        """(active provider usable, model_loaded, Ollama reachable). Copilot's and the
        remote server's parts are config-only checks: no network, so /health never
        blocks on them."""
        reachable, loaded = self._local_state()
        if self.provider == "copilot":
            signed_in = self.copilot.configured()
            return signed_in, signed_in, reachable
        if self.provider == "remote":
            ready = self.remote_ready()
            return ready, ready, reachable
        return reachable, loaded, reachable

    def health(self):
        usable, loaded, reachable = self._usable()
        payload = {
            "ok": usable,
            "service": cfgmod.SERVICE_NAME,
            "api_version": cfgmod.API_VERSION,
            "provider": self.provider,
            "model": self.active_model,
            "model_loaded": loaded,
            "ollama": "running" if reachable else "unreachable",
            "contract": PLUGIN_CONTRACT,
            "contract_version": PLUGIN_CONTRACT_VERSION,
            "plugin": {"name": cfgmod.SERVICE_NAME, "version": cfgmod.SERVICE_VERSION},
            "host": {"os": sys.platform, "hostname": platform.node()},
            "devices": 1,
            "capabilities": [],
            "error": None,
        }
        if readme_available():
            payload["docs"] = "/docs"
        return payload

    def models(self):
        return {"object": "list", "data": [{"id": self.active_model, "object": "model"}]}

    def models_list(self):
        installed = self.installed_models()
        error = None
        if self.provider == "copilot":
            listed, error = self.copilot_models()
            data = [{"id": m["id"], "object": "model", "name": m["name"], "vendor": m["vendor"]}
                    for m in listed]
        elif self.provider == "remote":
            listed, error = self.remote_models()
            data = [{"id": model_id, "object": "model"} for model_id in listed]
        else:
            data = [{"id": name, "object": "model"} for name in installed]
        return {"object": "list", "data": data, "provider": self.provider,
                "selected": self.active_model, "installed": installed, "error": error}

    def installed_models(self):
        """Models in the private Ollama copy; empty when Ollama is unreachable."""
        try:
            return runtime.list_models(self.cfg)
        except (OSError, ValueError):
            return []

    def copilot_models(self):
        """(vision models, error or None); ([], None) when not signed in."""
        if not self.copilot.configured():
            return [], None
        try:
            return self.copilot.list_models(), None
        except copilotmod.CopilotError as exc:
            return [], f"could not list Copilot models: {exc}"

    def remote_models(self, url=None, key=None):
        """(model ids of the remote server, error or None); ([], None) when no address is
        set. Gives up after remote.MODELS_TIMEOUT_SECS, so GET /config never hangs."""
        url = self.remote_url if url is None else url
        if not url:
            return [], None
        try:
            return remotemod.list_models(url, self.remote_key.get() if key is None else key), None
        except remotemod.RemoteError as exc:
            return [], str(exc)

    def devices(self):
        usable, loaded, _ = self._usable()
        return {"devices": [{
            "id": DEVICE_ID,
            "kind": "visual-model",
            "model": self.active_model,
            "state": "ready" if usable else "offline",
            "details": {"provider": self.provider,
                        "port": self.cfg.gateway_port,
                        "ollama_port": self.cfg.ollama_port,
                        "token_configured": bool(self.cfg.token),
                        "model_loaded": loaded},
        }], "error": None}

    def actions(self):
        return {"actions": ACTIONS}

    def copilot_public(self):
        """The copilot block of GET /config. Never includes a token."""
        info = self.copilot.public()
        models, error = self.copilot_models() if info["configured"] else ([], None)
        info.update(model=self.copilot_model, models=models, error=error)
        return info

    def remote_public(self):
        """The remote block of GET /config. Never includes the API key."""
        models, error = self.remote_models()
        return {"url": self.remote_url, "model": self.remote_model,
                "models": [{"id": model_id} for model_id in models],
                "resize": self.remote_resize, "key": self.remote_key.public(),
                "error": error}

    def config_get(self):
        token = self.cfg.token
        return {
            "provider": self.provider,
            "model": {"selected": self.cfg.model, "installed": self.installed_models()},
            "copilot": self.copilot_public(),
            "remote": self.remote_public(),
            # last4 only for tokens long enough that four characters don't give most away.
            "token": {"configured": bool(token), "last4": token[-4:] if len(token) >= 8 else None},
            "port": self.cfg.gateway_port,
            "ollama_port": self.cfg.ollama_port,
            "max_pixels": self.cfg.max_pixels,
            "api_version": cfgmod.API_VERSION,
            "service": runtime.service_status(),
        }

    def _validated_copilot_model(self, model):
        if not isinstance(model, str) or not model:
            raise BadRequest("copilot_model must be a non-empty string")
        if not self.copilot.configured():
            raise BadRequest(NOT_SIGNED_IN)
        try:
            available = [m["id"] for m in self.copilot.list_models()]
        except copilotmod.CopilotError as exc:
            raise BadRequest(f"could not list Copilot models: {exc}", 502, "upstream_error")
        if model not in available:
            raise BadRequest(f"{model} is not a Copilot vision model of this account "
                             f"(available: {', '.join(available) or 'none'})")
        return model

    def _validated_remote(self, request):
        """(url, model, resize, key) after the remote_* keys of a POST /config, or
        BadRequest. Nothing is stored here; the server is asked within 5 seconds.
        key is None when unchanged, "" to remove, else the new key."""
        key = None
        if "remote_api_key" in request:
            value = request["remote_api_key"]
            if value is not None and not isinstance(value, str):
                raise BadRequest("remote_api_key must be a string, or null to remove it")
            key = (value or "").strip()
        effective_key = self.remote_key.get() if key is None else key
        url, model = self.remote_url, self.remote_model
        listed = None  # model ids of the server at `url`, once fetched
        if "remote_url" in request:
            value = request["remote_url"]
            if not isinstance(value, str):
                raise BadRequest("remote_url must be a string")
            if not value.strip():
                url, model = "", ""
            else:
                try:
                    url = remotemod.normalize_url(value)
                except ValueError as exc:
                    raise BadRequest(str(exc))
                try:
                    listed = remotemod.list_models(url, effective_key)
                except remotemod.RemoteError as exc:
                    raise BadRequest(str(exc))
                if url != self.remote_url and model not in listed:
                    model = ""  # the old server's model; pick one of the new server's
        if "remote_model" in request:
            value = request["remote_model"]
            if not isinstance(value, str) or not value:
                raise BadRequest("remote_model must be a non-empty string")
            if not url:
                raise BadRequest("set remote_url first")
            if listed is None:
                try:
                    listed = remotemod.list_models(url, effective_key)
                except remotemod.RemoteError as exc:
                    raise BadRequest(str(exc))
            if value not in listed:
                raise BadRequest(f"{value} is not served by {url} "
                                 f"(available: {', '.join(listed) or 'none'})")
            model = value
        resize = self.remote_resize
        if "remote_resize" in request:
            if not isinstance(request["remote_resize"], bool):
                raise BadRequest("remote_resize must be true or false")
            resize = request["remote_resize"]
        return url, model, resize, key

    def config_set(self, body: bytes):
        """POST /config. Returns (status, payload). A rotated token's value appears
        exactly once, in the response to the request that rotated it."""
        request = parse_json(body)
        if not isinstance(request, dict) or not request:
            raise BadRequest("body must be a non-empty object")
        unknown = sorted(set(request) - CONFIG_KEYS)
        if unknown:
            raise BadRequest(f"unknown setting: {unknown[0]}")
        if "port" in request:
            raise BadRequest("a running server cannot rebind; stop the service and restart it "
                             "with MTAI_PORT (reinstall the login service with the new value)")
        # Validate everything that can fail before changing anything.
        provider = request.get("provider", self.provider)
        if provider not in PROVIDERS:
            raise BadRequest('provider must be "local", "remote" or "copilot"')
        if "provider" in request and provider == "copilot" and not self.copilot.configured():
            raise BadRequest(NOT_SIGNED_IN)
        copilot_model = self.copilot_model
        if "copilot_model" in request:
            copilot_model = self._validated_copilot_model(request["copilot_model"])
        remote_url, remote_model, remote_resize, remote_key = self._validated_remote(request)
        if provider == "remote" and not (remote_url and remote_model):
            if "provider" in request:
                raise BadRequest(REMOTE_NOT_SET)
            # The address was cleared or moved to a server without the selected model.
            _log("the remote server address or model is no longer set; switching to local")
            provider = "local"
        model = self.cfg.model
        if "model" in request:
            model = request["model"]
            if not isinstance(model, str) or not model:
                raise BadRequest("model must be a non-empty string")
            installed = self.installed_models()
            if not installed:
                raise BadRequest("cannot validate the model while Ollama is offline")
            if model not in installed:
                raise BadRequest(f"{model} is not installed (installed: {', '.join(installed)})")
        new_token = None
        if "token" in request:
            value = request["token"]
            if isinstance(value, dict):
                if set(value) != {"generate"} or value["generate"] is not True:
                    raise BadRequest('token object must be {"generate": true}')
                value = secrets.token_hex(32)
            if not isinstance(value, str) or not value.strip():
                raise BadRequest("token must be a non-empty string")
            new_token = value.strip()
        selection = (provider, copilot_model, model, remote_url, remote_model, remote_resize)
        changed = selection != (self.provider, self.copilot_model, self.cfg.model,
                                self.remote_url, self.remote_model, self.remote_resize)
        if remote_key is not None:
            if remote_key:
                self.remote_key.set(remote_key)
            else:
                self.remote_key.remove()
        self.provider, self.copilot_model = provider, copilot_model
        self.remote_url, self.remote_model, self.remote_resize = (remote_url, remote_model,
                                                                  remote_resize)
        if model != self.cfg.model:
            self.cfg = replace(self.cfg, model=model)
        if changed:
            self.save_settings()
        if new_token:
            self.cfg = replace(self.cfg, token=new_token)
            runtime.persist_service_token(new_token)
        payload = {"ok": True, "config": self.config_get()}
        if new_token:
            payload["new_token"] = new_token
        return 200, payload

    # --- GitHub Copilot sign-in ---

    def copilot_device_start(self):
        try:
            return 200, self.copilot.start_device_flow()
        except copilotmod.CopilotError as exc:
            return 502, error_body("upstream_error", f"GitHub device sign-in failed: {exc}")

    def copilot_device_poll(self):
        reply = self.copilot.poll_device_flow()
        if reply.get("state") == "done":
            reply.update(self.copilot_public())
        return 200, reply

    def copilot_remove(self):
        self.copilot.remove()
        if self.provider == "copilot":
            self.provider = "local"
            self.save_settings()
        return 200, self.copilot_public()

    # --- chat ---

    def chat(self, body: bytes):
        """Return (status, response dict) for a chat completion request."""
        request = parse_json(body)
        if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
            raise BadRequest("messages must be a list")
        if request.get("stream"):
            raise BadRequest("streaming is not supported")
        self._check_image_count(request)
        if self.provider == "copilot":
            return self._chat_copilot(request)
        if self.provider == "remote":
            return self._chat_remote(request)
        request["model"] = self.cfg.model
        geometry = self._prepare_images(request)
        status, reply = self._forward(request)
        if status == 200 and geometry:
            self._restore_coordinates(reply, geometry)
        return status, reply

    def _chat_remote(self, request):
        """The remote server gets the Local pipeline (resize, system hint, rescale) when
        remote_resize is on, which suits a Qwen2.5-VL there; off, the request goes as sent
        with only the model replaced, as for Copilot."""
        if not self.remote_ready():
            return 503, error_body("model_unavailable", "the remote server is not configured")
        url, resize = self.remote_url, self.remote_resize
        request["model"] = self.remote_model
        geometry = self._prepare_images(request) if resize else None
        status, reply = self._forward(request, base_url=url,
                                      headers=remotemod.auth_headers(self.remote_key.get()),
                                      backend=f"remote server {url}")
        if status == 200 and geometry:
            self._restore_coordinates(reply, geometry)
        return status, reply

    def _chat_copilot(self, request):
        """Copilot gets the request as sent, with only the model replaced: GPT-class
        models answer in pixels of the image they were given, so there is nothing to
        resize or rescale."""
        if not self.copilot.configured():
            return 503, error_body("model_unavailable", "GitHub Copilot is not signed in")
        request["model"] = self.copilot_model
        try:
            status, reply = self.copilot.chat(request, timeout=UPSTREAM_TIMEOUT_SECS)
        except copilotmod.CopilotError as exc:
            if exc.status is not None:
                return exc.status, error_body("upstream_error", str(exc))
            return 503, error_body("model_unavailable", f"GitHub Copilot unavailable: {exc}")
        if status != 200:
            return status, error_body("upstream_error", str(reply))
        return status, reply

    @staticmethod
    def _check_image_count(request):
        """At most MAX_IMAGES image parts, whichever provider answers."""
        count = 0
        for message in request["messages"]:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                count += sum(1 for part in content
                             if isinstance(part, dict) and part.get("type") == "image_url")
        if count > MAX_IMAGES:
            raise BadRequest(f"at most {MAX_IMAGES} images per request", 413, "body_too_large")

    def _prepare_images(self, request):
        """Resize every image; return geometry only when there is exactly one.

        With several images a coordinate doesn't say which image it refers to,
        so replies to those requests are not rescaled.
        """
        sizes = []
        for message in request["messages"]:
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    sizes.append(self._resize_part(part))
        if len(sizes) != 1:
            return None
        src_w, src_h, new_w, new_h = sizes[0]
        request["messages"].insert(0, {
            "role": "system",
            "content": f"The image is {new_w}x{new_h} pixels. Report any coordinates "
                       "as absolute pixel values in that image. A single [x, y] point "
                       "for an element must be the center of that element.",
        })
        return src_w, src_h, new_w, new_h

    def _resize_part(self, part):
        image_url = part.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else None
        match = DATA_URL.match(url) if isinstance(url, str) else None
        if not match:
            raise BadRequest("image_url must be a base64 data URL")
        if len(match.group(1)) * 3 // 4 > MAX_IMAGE_BYTES + 3:
            raise BadRequest(f"an image exceeds {MAX_IMAGE_BYTES} bytes", 413, "body_too_large")
        try:
            data = base64.b64decode(match.group(1), validate=True)
        except (binascii.Error, ValueError):
            raise BadRequest("image_url is not valid base64")
        try:
            fmt, src_w, src_h = imaging.image_size(data)
            new_w, new_h = imaging.smart_resize(src_w, src_h, self.cfg.max_pixels)
            if (new_w, new_h) != (src_w, src_h):
                data = self.resizer(data, fmt, new_w, new_h)
        except imaging.ImageError as exc:
            raise BadRequest(str(exc))
        encoded = base64.b64encode(data).decode()
        image_url["url"] = f"data:image/{fmt};base64,{encoded}"
        return src_w, src_h, new_w, new_h

    def _forward(self, request, base_url=None, headers=None, backend="model backend"):
        """POST to {base_url}/v1/chat/completions (default: the private Ollama)."""
        req = urllib.request.Request(
            copilotmod.require_http((base_url or self.cfg.ollama_url) + "/v1/chat/completions"),
            data=json.dumps(request).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_SECS) as resp:  # nosec B310 - require_http above
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            with exc:
                raw = exc.read()
            message = _upstream_error(raw)
            if base_url:
                message = f"{backend} answered HTTP {exc.code}: {message}"
            return exc.code, error_body("upstream_error", message)
        except (OSError, ValueError) as exc:
            return 503, error_body("model_unavailable", f"{backend} unavailable: {exc}")

    @staticmethod
    def _restore_coordinates(reply, geometry):
        src_w, src_h, new_w, new_h = geometry
        for choice in reply.get("choices") or []:
            message = choice.get("message") or {}
            if isinstance(message.get("content"), str):
                message["content"] = imaging.rescale_reply(
                    message["content"], src_w / new_w, src_h / new_h, src_w, src_h
                )

    # --- plugin jobs (see PLUGIN_API.md in the MonkeyTester repo) ---

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def start_job(self, body: bytes):
        """POST /jobs. Runs the action and answers 202 with the finished job; set-model
        is instant, so a caller's first GET /jobs/<id> already sees a terminal state."""
        try:
            request = parse_json(body)
        except BadRequest:
            return contract_error(400, "bad_request", "body is not valid JSON")
        if not isinstance(request, dict):
            return contract_error(400, "bad_request", "job body must be an object")
        action = request.get("action")
        params = request.get("params") or {}
        timeout_ms = request.get("timeout_ms")
        device = request.get("device")
        if not isinstance(action, str) or not action:
            return contract_error(400, "bad_request", "action must be a non-empty string")
        if device is not None and device != DEVICE_ID:
            return contract_error(404, "unknown_device", f"unknown device: {device}")
        if action not in ACTION_NAMES:
            return contract_error(404, "unknown_action", f"unknown action: {action}")
        if not isinstance(timeout_ms, int) or timeout_ms <= 0:
            timeout_ms = None
        model = params.get("model") if isinstance(params, dict) else None
        if not isinstance(model, str) or not model:
            return contract_error(400, "bad_request", "params.model must be a non-empty string")
        started = self._now_ms()
        with self._jobs_lock:
            self._prune_jobs_locked()
            if any(job["state"] not in JOB_TERMINAL for job in self.jobs.values()):
                return contract_error(409, "device_busy", "a job is already running")
            self._job_seq += 1
            job = {"id": f"j-{self._job_seq:04x}", "device": DEVICE_ID, "action": action,
                   "params": dict(params), "state": "running", "started_ms": started,
                   "finished_ms": None, "result": None, "error": None,
                   "log": [f"job started: {action} on {DEVICE_ID}"]}
            self.jobs[job["id"]] = job
        previous = self.active_model
        problem = self._set_model_problem(model)
        if problem is None:
            state, error = "passed", None
            result = {"previous": previous, "current": model}
            log = job["log"] + [f"provider {self.provider}; previous model {previous}; "
                                f"selected model {model}"]
            if model != previous:
                if self.provider == "copilot":
                    self.copilot_model = model
                elif self.provider == "remote":
                    self.remote_model = model
                else:
                    self.cfg = replace(self.cfg, model=model)
                self.save_settings()
        else:
            state = "failed"
            error = {"code": "model_unavailable", "message": problem}
            result = None
            log = job["log"] + [problem]
        finished = self._now_ms()
        if timeout_ms is not None and finished - started > timeout_ms:
            state = "timeout"
            error = {"code": "timeout", "message": f"job exceeded {timeout_ms} ms"}
        with self._jobs_lock:
            job.update(state=state, finished_ms=finished, result=result, error=error, log=log)
            done = dict(job)
        return 202, {"job": done}

    def _set_model_problem(self, model):
        """Why `model` can't be selected for the active provider, or None."""
        if self.provider == "copilot":
            if not self.copilot.configured():
                return "GitHub Copilot is not signed in"
            try:
                available = [m["id"] for m in self.copilot.list_models()]
            except copilotmod.CopilotError as exc:
                return f"could not list Copilot models: {exc}"
            if model not in available:
                return (f"{model} is not a Copilot vision model of this account "
                        f"(available: {', '.join(available) or 'none'})")
            return None
        if self.provider == "remote":
            if not self.remote_url:
                return "the remote server is not configured"
            try:
                available = remotemod.list_models(self.remote_url, self.remote_key.get())
            except remotemod.RemoteError as exc:
                return str(exc)
            if model not in available:
                return (f"{model} is not served by {self.remote_url} "
                        f"(available: {', '.join(available) or 'none'})")
            return None
        installed = self.installed_models()
        if model not in installed:
            return (f"{model} is not installed in the private Ollama "
                    f"(installed: {', '.join(installed) or 'none'})")
        return None

    def get_job(self, job_id: str, wait: float):
        """GET /jobs/<id>?wait=<seconds>. Blocks up to `wait` (0-60) until terminal."""
        deadline = time.monotonic() + max(0.0, min(wait, 60.0))
        while True:
            with self._jobs_lock:
                job = self.jobs.get(job_id)
                snapshot = dict(job) if job else None
            if snapshot is None:
                return contract_error(404, "not_found", f"unknown job: {job_id}")
            if snapshot["state"] in JOB_TERMINAL or time.monotonic() >= deadline:
                return 200, snapshot
            time.sleep(0.05)

    def cancel_job(self, job_id: str):
        """POST /jobs/<id>/cancel. A job that already finished stays in its state."""
        with self._jobs_lock:
            job = self.jobs.get(job_id)
            if job is None:
                return contract_error(404, "not_found", f"unknown job: {job_id}")
            if job["state"] not in JOB_TERMINAL:
                job.update(state="cancelled", finished_ms=self._now_ms(),
                           error={"code": "cancelled", "message": "cancelled on request"})
            return 200, {"job": dict(job)}

    def _prune_jobs_locked(self):
        """Finished jobs stay queryable for at least 10 minutes (the contract's floor)."""
        now = self._now_ms()
        for job_id, job in list(self.jobs.items()):
            if job["state"] in JOB_TERMINAL and job["finished_ms"] and now - job["finished_ms"] > JOBS_KEPT_MS:
                del self.jobs[job_id]


def _upstream_error(raw: bytes) -> str:
    try:
        error = json.loads(raw).get("error")
    except (ValueError, AttributeError):
        return raw.decode(errors="replace")[:500]
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


COPILOT_ROUTES = {
    "/copilot/device/start": "copilot_device_start",
    "/copilot/device/poll": "copilot_device_poll",
    "/copilot/remove": "copilot_remove",
}


def make_handler(gateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MonkeyTesterAI"
        timeout = CLIENT_TIMEOUT_SECS  # a stalled client shouldn't hold a thread for long

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_docs(self):
            body = docs_page()
            if body is None:
                self._send_error(404, "not_found", "documentation unavailable: README.md not found")
                return
            self.send_response(200)
            for name, value in DOCS_HEADERS.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status, code, message, contract=False):
            if contract:
                self._send(status, {"error": message, "code": code})
            else:
                self._send(status, error_body(code, message))

        def _authorized(self, contract=False):
            token = gateway.cfg.token
            if not token:
                return True
            ok = hmac.compare_digest(self.headers.get("Authorization", "").encode(),
                                     f"Bearer {token}".encode())
            if not ok:
                ok = hmac.compare_digest(self.headers.get("X-Plugin-Token", "").encode(),
                                         token.encode())
            if ok:
                return True
            self._send_error(401, "unauthorized", "invalid or missing token", contract)
            return False

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.close_connection = True
                self._send_error(411, "invalid_request", "Content-Length required")
                return None
            if length < 0 or length > MAX_BODY_BYTES:
                self.close_connection = True
                self._send_error(413, "body_too_large", f"body exceeds {MAX_BODY_BYTES} bytes")
                return None
            return self.rfile.read(length)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/health":
                self._send(200, gateway.health())
            elif path == "/v1/models":
                if self._authorized():
                    self._send(200, gateway.models())
            elif path == "/models":
                if self._authorized():
                    self._send(200, gateway.models_list())
            elif path == "/config":
                if self._authorized():
                    self._send(200, gateway.config_get())
            elif path == "/docs":
                if self._authorized():
                    self._send_docs()
            elif path in ("/devices", "/actions"):
                if not self._authorized(contract=True):
                    return
                payload = gateway.devices() if path == "/devices" else gateway.actions()
                self._send(200, payload)
            elif path.startswith("/jobs/"):
                if not self._authorized(contract=True):
                    return
                parts = path.split("/")
                if len(parts) != 3:
                    self._send_error(404, "not_found", "not found", contract=True)
                    return
                try:
                    wait = float(urllib.parse.parse_qs(query).get("wait", ["0"])[0])
                except ValueError:
                    self._send_error(400, "bad_request", "wait must be a number of seconds",
                                     contract=True)
                    return
                status, payload = gateway.get_job(parts[2], wait)
                self._send(status, payload)
            else:
                self._send_error(404, "not_found", "not found")

        def do_POST(self):
            path = self.path.split("?")[0]
            if path in COPILOT_ROUTES:
                if not self._authorized():
                    return
                # These routes take no body; a POST without Content-Length is fine.
                if "Content-Length" in self.headers and self._read_body() is None:
                    return
                try:
                    status, reply = getattr(gateway, COPILOT_ROUTES[path])()
                except Exception as exc:  # the client always gets an answer it can parse
                    status, reply = 500, _internal_error(exc)
                self._send(status, reply)
            elif path in ("/v1/chat/completions", "/jobs", "/config"):
                contract = path == "/jobs"
                if not self._authorized(contract=contract):
                    return
                body = self._read_body()
                if body is None:
                    return
                try:
                    if path == "/jobs":
                        status, reply = gateway.start_job(body)
                    elif path == "/config":
                        status, reply = gateway.config_set(body)
                    else:
                        status, reply = gateway.chat(body)
                except BadRequest as exc:
                    status, reply = exc.status, error_body(exc.code, str(exc))
                except (BrokenPipeError, ConnectionResetError):
                    raise
                except Exception as exc:  # the client always gets an answer it can parse
                    status, reply = 500, _internal_error(exc)
                    if contract:
                        status, reply = contract_error(500, "internal_error", reply["error"]["message"])
                self._send(status, reply)
            elif path.startswith("/jobs/") and path.endswith("/cancel"):
                if not self._authorized(contract=True):
                    return
                status, payload = gateway.cancel_job(path.split("/")[2])
                self._send(status, payload)
            else:
                self._send_error(404, "not_found", "not found")

        def _not_allowed(self):
            self._send_error(404, "not_found", "not found")

        do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _not_allowed

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} {fmt % args}", file=sys.stderr, flush=True)

    return Handler


def _log(message):
    print(f"[monkeytester-ai] {message}", file=sys.stderr, flush=True)


def _internal_error(exc):
    """The 500 body: the exception type only. The details (which could name paths
    or upstream replies) go to the service log, never to the client."""
    _log(f"request failed: {exc!r}")
    return error_body("internal_error", f"internal error: {type(exc).__name__}")


class Supervisor:
    """Keeps Ollama running and, with the local provider, the selected model loaded.

    Takes the Gateway, not a Config, because POST /config can swap the model (and
    token) while the service runs: preloading the old model would pin it in memory."""

    def __init__(self, app, ollama):
        self.app = app
        self.ollama = ollama
        self.stopping = threading.Event()

    def run(self):
        self._preload()
        while not self.stopping.wait(2.0):
            if self.ollama.alive():
                continue
            _log("Ollama exited; restarting")
            try:
                self.ollama.start()
                self._preload()
            except (runtime.RuntimeError_, OSError) as exc:
                # Keep trying: a dead supervisor thread would leave the model down for good.
                _log(str(exc))
                self.stopping.wait(5.0)

    def _preload(self):
        """Only the local provider keeps a model in memory; switching back to local
        lets the next chat request load it."""
        if self.app.provider != "local":
            return
        cfg = self.app.cfg
        started = time.monotonic()
        try:
            runtime.preload(cfg)
            _log(f"{cfg.model} loaded in {time.monotonic() - started:.1f}s")
        except (OSError, ValueError) as exc:
            _log(f"could not preload {cfg.model}: {exc}")


def serve(cfg, lan=False):
    """Run Ollama and the gateway until interrupted."""
    bind = "0.0.0.0" if lan else "127.0.0.1"  # nosec B104 - --lan is opt-in and needs MTAI_TOKEN
    problem = bind_error(bind, cfg.token)
    if problem:
        raise runtime.RuntimeError_(problem)
    app = Gateway(cfg)
    ollama = runtime.OllamaServer(cfg)
    ollama.start()
    supervisor = Supervisor(app, ollama)
    try:
        runtime.verify_model(cfg)
        app.reconcile_local_model()
        httpd = ThreadingHTTPServer((bind, cfg.gateway_port), make_handler(app))
    except (OSError, runtime.RuntimeError_):
        ollama.stop()
        raise

    import signal

    def shutdown(_signum, _frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=supervisor.run, daemon=True).start()
    _log(f"listening on {bind}:{cfg.gateway_port}, provider {app.provider}, "
         f"model {app.active_model}")
    try:
        httpd.serve_forever()
    finally:
        supervisor.stopping.set()
        httpd.server_close()
        ollama.stop()
