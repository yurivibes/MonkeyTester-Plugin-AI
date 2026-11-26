"""GitHub Copilot as the second vision provider: sign-in, token exchange, models.

Sign-in is GitHub's OAuth device flow: the plugin asks GitHub for a short user
code, the user enters it at github.com/login/device, and the caller polls until
GitHub hands over an OAuth token. That token is long-lived and is what gets
stored. Each chat request needs a short-lived Copilot token (about 30 minutes)
that is exchanged from the OAuth token and cached in memory.

The sign-in lives in <runtime>/copilot.json (MTAI_COPILOT_CONFIG overrides it),
written atomically with mode 0600. The OAuth token is plaintext at rest: the
service reads it unattended, so a key kept on the same disk would add no real
protection. No HTTP route ever returns it.

The OAuth app is the one GitHub's own editor plugins use (there is no public
Copilot API app), and the request headers identify as an editor integration
because api.githubcopilot.com rejects requests without them. This is not an
official public API and may break.

Ported from MonkeyTester's former scripts/copilot_client.py. Standard library only.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CLIENT_ID = "Iv1.b507a08c87ecfe98"
# Overridable so tests (and a scratch instance) can point at a fake GitHub.
GITHUB_URL = (os.environ.get("MTAI_GITHUB_URL") or "https://github.com").rstrip("/")
GITHUB_API = (os.environ.get("MTAI_GITHUB_API") or "https://api.github.com").rstrip("/")
DEFAULT_API = (os.environ.get("MTAI_COPILOT_API") or "https://api.githubcopilot.com").rstrip("/")
DEFAULT_MODEL = "gpt-4o"

EDITOR_HEADERS = {
    "Editor-Version": "vscode/1.99.0",
    "Editor-Plugin-Version": "copilot-chat/0.26.0",
    "Copilot-Integration-Id": "vscode-chat",
    "User-Agent": "GitHubCopilotChat/0.26.0",
}

# A Copilot token is refreshed this many seconds before it expires.
REFRESH_MARGIN_SECS = 120
HTTP_TIMEOUT_SECS = 20
# GET /config lists models on every call; a short cache keeps that cheap.
MODELS_CACHE_SECS = 60


class CopilotError(Exception):
    """status is the upstream HTTP status, or None when the network failed."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _error_message(raw: bytes) -> str:
    try:
        parsed = json.loads(raw.decode() or "{}")
        err = parsed.get("error")
        return str(parsed.get("message") or parsed.get("error_description")
                   or (err.get("message") if isinstance(err, dict) else err)
                   or raw.decode()[:200])
    except (ValueError, AttributeError):
        return raw.decode(errors="replace")[:200]


def require_http(url):
    """`url` when it is http(s)://; urlopen must never open file:// or other schemes."""
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        raise CopilotError("only http:// and https:// URLs are allowed")
    return url


def request_json(url, data=None, headers=None, method=None, timeout=HTTP_TIMEOUT_SECS):
    """JSON request. Returns the parsed body; raises CopilotError with a short reason."""
    require_http(url)
    body = None
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers or {})
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - require_http above
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        with exc:
            raw = exc.read()
        raise CopilotError(f"HTTP {exc.code}: {_error_message(raw)}", status=exc.code)
    except urllib.error.URLError as exc:
        raise CopilotError(f"unreachable: {exc.reason}")
    except OSError as exc:  # includes timeouts
        raise CopilotError(f"request failed: {exc}")
    except ValueError as exc:
        raise CopilotError(f"unusable reply: {exc}")


def write_private_json(path, data):
    """Write JSON readable only by this user (0600), atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def read_json(path) -> dict:
    try:
        with open(str(path)) as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def load_signin(path) -> dict:
    """The stored sign-in; blank fields when there is none. Works with the service stopped."""
    cfg = {"oauth_token": "", "user": "", "saved_at": ""}
    stored = read_json(path)
    cfg.update({k: str(stored[k]) for k in cfg if isinstance(stored.get(k), str)})
    return cfg


class CopilotClient:
    """One sign-in, its Copilot token cache and the device flow in progress."""

    def __init__(self, config_path, github_url=None, github_api=None, default_api=None):
        self.config_path = Path(config_path)
        self.github_url = (github_url or GITHUB_URL).rstrip("/")
        self.github_api = (github_api or GITHUB_API).rstrip("/")
        self.default_api = (default_api or DEFAULT_API).rstrip("/")
        self._lock = threading.RLock()
        self._token_cache = {}   # oauth token -> {"token", "expires_at", "api"}
        self._models_cache = {}  # oauth token -> (fetched_at, models)
        self._pending_flow = {}  # device_code, interval, expires_at

    # --- stored sign-in ---

    def load(self) -> dict:
        return load_signin(self.config_path)

    def configured(self) -> bool:
        """Config-only check: no network, so /health can use it."""
        return bool(self.load()["oauth_token"])

    def public(self) -> dict:
        """The sign-in fields callers may see. Never includes a token."""
        cfg = self.load()
        token = cfg["oauth_token"]
        return {"configured": bool(token), "user": cfg["user"],
                "last4": token[-4:] if len(token) >= 8 else "",
                "saved_at": cfg["saved_at"]}

    def remove(self):
        with self._lock:
            self._token_cache.clear()
            self._models_cache.clear()
            self._pending_flow.clear()
            try:
                self.config_path.unlink()
            except FileNotFoundError:
                pass

    # --- device flow ---

    def start_device_flow(self) -> dict:
        """Ask GitHub for a user code. The device code stays server-side."""
        reply = request_json(f"{self.github_url}/login/device/code",
                             data={"client_id": CLIENT_ID, "scope": "read:user"})
        if not reply.get("device_code") or not reply.get("user_code"):
            raise CopilotError(f"GitHub did not return a device code: {reply}")
        interval = int(reply.get("interval") or 5)
        expires_in = int(reply.get("expires_in") or 900)
        with self._lock:
            self._pending_flow.clear()
            self._pending_flow.update(device_code=reply["device_code"], interval=interval,
                                      expires_at=time.time() + expires_in)
        return {"user_code": reply["user_code"],
                "verification_uri": reply.get("verification_uri") or f"{self.github_url}/login/device",
                "interval": interval, "expires_in": expires_in}

    def poll_device_flow(self) -> dict:
        """One poll: {"state": "pending"|"slow_down"|"done"|"expired"|"denied"|"error", ...}.

        On "done" the OAuth token is checked against Copilot first and stored only if
        the account really has Copilot."""
        with self._lock:
            flow = dict(self._pending_flow)
        if not flow:
            return {"state": "error", "error": "No sign-in in progress; start again"}
        if time.time() > flow["expires_at"]:
            with self._lock:
                self._pending_flow.clear()
            return {"state": "expired", "error": "The code expired; start again"}
        try:
            reply = request_json(f"{self.github_url}/login/oauth/access_token",
                                 data={"client_id": CLIENT_ID, "device_code": flow["device_code"],
                                       "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        except CopilotError as exc:
            return {"state": "error", "error": str(exc)}
        error = reply.get("error")
        if error == "authorization_pending":
            return {"state": "pending", "interval": flow["interval"]}
        if error == "slow_down":
            interval = int(reply.get("interval") or flow["interval"] + 5)
            with self._lock:
                if self._pending_flow:
                    self._pending_flow["interval"] = interval
            return {"state": "slow_down", "interval": interval}
        if error in ("expired_token", "access_denied"):
            with self._lock:
                self._pending_flow.clear()
            return {"state": "expired" if error == "expired_token" else "denied",
                    "error": reply.get("error_description") or error}
        if error or not reply.get("access_token"):
            return {"state": "error", "error": reply.get("error_description") or error or "no token"}

        oauth = reply["access_token"]
        with self._lock:
            self._pending_flow.clear()
        try:
            user = request_json(f"{self.github_api}/user",
                                headers={"Authorization": f"token {oauth}"})
            self.copilot_token(oauth)  # fails when the account has no Copilot access
        except CopilotError as exc:
            return {"state": "error", "error": f"Signed in, but Copilot is not available: {exc}"}
        with self._lock:
            self._models_cache.clear()
            write_private_json(self.config_path, {
                "oauth_token": oauth, "user": user.get("login") or "",
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return {"state": "done"}

    # --- Copilot API ---

    def copilot_token(self, oauth=None, force=False):
        """(token, api base URL) for Copilot requests, refreshed shortly before expiry."""
        oauth = oauth or self.load()["oauth_token"]
        if not oauth:
            raise CopilotError("GitHub Copilot is not signed in")
        with self._lock:
            cached = self._token_cache.get(oauth)
            if cached and not force and cached["expires_at"] - REFRESH_MARGIN_SECS > time.time():
                return cached["token"], cached["api"]
        reply = request_json(f"{self.github_api}/copilot_internal/v2/token",
                             headers={"Authorization": f"token {oauth}", **EDITOR_HEADERS})
        token = reply.get("token")
        if not token:
            raise CopilotError(f"no Copilot token in reply: {reply.get('message') or reply}")
        api = self._trusted_api((reply.get("endpoints") or {}).get("api"))
        expires_at = float(reply.get("expires_at") or time.time() + 1500)
        with self._lock:
            self._token_cache[oauth] = {"token": token, "expires_at": expires_at, "api": api}
        return token, api

    def _trusted_api(self, api):
        """The Copilot API base from the token exchange, where the short-lived token
        is sent: only https://, or the configured default/GitHub API host (tests'
        fakes); anything else falls back to the default API."""
        if not isinstance(api, str) or not api:
            return self.default_api
        api = api.rstrip("/")
        parts = urllib.parse.urlsplit(api)
        if parts.scheme == "https" and parts.hostname and "@" not in parts.netloc:
            return api
        trusted = {urllib.parse.urlsplit(u).netloc for u in (self.default_api, self.github_api)}
        if parts.scheme == "http" and parts.netloc in trusted:
            return api
        return self.default_api

    @staticmethod
    def api_headers(token, vision=True) -> dict:
        headers = {"Authorization": f"Bearer {token}", **EDITOR_HEADERS,
                   "Openai-Intent": "conversation-panel"}
        if vision:
            headers["Copilot-Vision-Request"] = "true"
        return headers

    def list_models(self, vision_only=True, use_cache=True) -> list:
        """Chat models the account can use, vision-capable ones only by default:
        [{"id", "name", "vendor"}]. Raises CopilotError."""
        oauth = self.load()["oauth_token"]
        if not oauth:
            raise CopilotError("GitHub Copilot is not signed in")
        with self._lock:
            cached = self._models_cache.get(oauth)
            if (use_cache and vision_only and cached
                    and time.monotonic() - cached[0] < MODELS_CACHE_SECS):
                return [dict(m) for m in cached[1]]
        token, api = self.copilot_token(oauth)
        try:
            reply = request_json(f"{api}/models", headers=self.api_headers(token, vision=False))
        except CopilotError as exc:
            if exc.status != 401:
                raise
            token, api = self.copilot_token(oauth, force=True)
            reply = request_json(f"{api}/models", headers=self.api_headers(token, vision=False))
        seen, out = set(), []
        for model in reply.get("data") or []:
            if not isinstance(model, dict):
                continue
            caps = model.get("capabilities") or {}
            if caps.get("type") not in (None, "chat"):
                continue
            if vision_only and not (caps.get("supports") or {}).get("vision"):
                continue
            if model.get("model_picker_enabled") is False:
                continue
            model_id = model.get("id")
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            out.append({"id": model_id, "name": model.get("name") or model_id,
                        "vendor": model.get("vendor") or ""})
        if vision_only:
            with self._lock:
                self._models_cache[oauth] = (time.monotonic(), out)
        return [dict(m) for m in out]

    def chat(self, request: dict, timeout):
        """POST one chat completion to Copilot. Returns (status, parsed JSON body or raw
        error message). A 401 forces a token refresh and is retried once.

        Raises CopilotError for sign-in, token-exchange and network failures."""
        body = json.dumps(request).encode()
        for attempt in (0, 1):
            token, api = self.copilot_token(force=attempt == 1)
            req = urllib.request.Request(
                require_http(f"{api}/chat/completions"), data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         **self.api_headers(token)})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - require_http above
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                with exc:
                    raw = exc.read()
                if exc.code == 401 and attempt == 0:
                    continue
                return exc.code, _error_message(raw)
            except urllib.error.URLError as exc:
                raise CopilotError(f"unreachable: {exc.reason}")
            except OSError as exc:
                raise CopilotError(f"request failed: {exc}")
            except ValueError as exc:
                raise CopilotError(f"unusable reply: {exc}")
        raise CopilotError("unreachable")  # pragma: no cover - the loop always returns
