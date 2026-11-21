"""GitHub Copilot as the second provider: device sign-in, provider and model choice,
chat forwarding, persistence. Uses fake_ollama plus fake_github (a stdlib stand-in for
github.com, api.github.com and the Copilot API); nothing here reaches the real GitHub."""
import argparse
import base64
import contextlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests.fake_github import FakeGitHub, LOGIN, OAUTH_TOKEN, REPLY, USER_CODE
from tests.fake_ollama import FakeOllama, MODEL, MODEL2
from monkeytester_ai import cli, config, copilot, gateway

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "monkeytester-ai"
FIXTURE = (Path(__file__).parent / "fixtures" / "target.png").read_bytes()
SMALL = base64.b64encode(FIXTURE).decode()
VISION_IDS = ["gpt-4o", "claude-sonnet-4"]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CopilotCase(unittest.TestCase):
    token = "feed0000beef4321"

    def setUp(self):
        self.ollama = FakeOllama()
        self.addCleanup(self.ollama.close)
        self.github = FakeGitHub()
        self.addCleanup(self.github.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime_dir = Path(tmp.name)
        self.cfg = replace(config.load({"VTAI_RUNTIME_DIR": tmp.name}),
                           ollama_port=self.ollama.port, token=self.token)
        self.resized = []
        self.start_gateway()

    def make_client(self):
        return copilot.CopilotClient(self.cfg.copilot_config, github_url=self.github.url,
                                     github_api=self.github.url)

    def resizer(self, data, fmt, width, height):
        self.resized.append((fmt, width, height))
        return b"resized:" + data[:4]

    def start_gateway(self):
        """(Re)start the gateway on the same runtime dir, like a service restart."""
        self.app = gateway.Gateway(self.cfg, resizer=self.resizer, copilot=self.make_client())
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.make_handler(self.app))
        self.url = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.responses = []

    def call(self, path, payload=None, method=None, auth=True, raw=None):
        data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
        headers = {"X-Plugin-Token": self.token} if auth else {}
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, text = resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            status, text = exc.code, exc.read().decode()
        self.responses.append(text)
        return status, json.loads(text)

    def post(self, path, payload=None):
        return self.call(path, payload if payload is not None else {}, method="POST")

    def sign_in(self):
        self.github.state.oauth_replies = [{"access_token": OAUTH_TOKEN}]
        self.assertEqual(self.post("/copilot/device/start")[0], 200)
        status, body = self.post("/copilot/device/poll")
        self.assertEqual((status, body["state"]), (200, "done"), body)
        return body

    def use_copilot(self):
        self.sign_in()
        status, body = self.post("/config", {"provider": "copilot"})
        self.assertEqual(status, 200, body)

    @staticmethod
    def chat(*images, model="anything"):
        parts = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{i}"}} for i in images]
        return {"model": model, "temperature": 0.0,
                "messages": [{"role": "user", "content": parts + [{"type": "text", "text": "q"}]}]}

    def assert_no_token_leaked(self):
        for text in self.responses:
            self.assertNotIn(OAUTH_TOKEN, text)
            for issued in self.github.state.issued:
                self.assertNotIn(issued, text)


class DeviceFlowTests(CopilotCase):
    def test_start_poll_pending_slow_down_done(self):
        status, body = self.post("/copilot/device/start")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"user_code": USER_CODE,
                                "verification_uri": self.github.url + "/login/device",
                                "interval": 5, "expires_in": 900})
        self.assertEqual(self.post("/copilot/device/poll")[1], {"state": "pending", "interval": 5})
        self.assertEqual(self.post("/copilot/device/poll")[1], {"state": "slow_down", "interval": 10})
        self.assertFalse(self.cfg.copilot_config.exists())
        status, body = self.post("/copilot/device/poll")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "done")
        self.assertTrue(body["configured"])
        self.assertEqual(body["user"], LOGIN)
        self.assertEqual(body["last4"], OAUTH_TOKEN[-4:])
        self.assertTrue(body["saved_at"])
        self.assertEqual(body["model"], "gpt-4o")
        self.assertEqual([m["id"] for m in body["models"]], VISION_IDS)
        self.assertEqual(body["models"][1], {"id": "claude-sonnet-4", "name": "Claude Sonnet 4",
                                             "vendor": "Anthropic"})
        self.assertIsNone(body["error"])
        # Stored privately, next to the other runtime state.
        self.assertEqual(self.cfg.copilot_config, self.runtime_dir / "copilot.json")
        self.assertEqual(stat.S_IMODE(self.cfg.copilot_config.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.cfg.copilot_config.read_text())["oauth_token"], OAUTH_TOKEN)
        self.call("/config")
        self.call("/models")
        self.assert_no_token_leaked()

    def test_account_without_copilot_stores_nothing(self):
        self.github.state.has_copilot = False
        self.github.state.oauth_replies = [{"access_token": OAUTH_TOKEN}]
        self.post("/copilot/device/start")
        status, body = self.post("/copilot/device/poll")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "error")
        self.assertIn("Copilot is not available", body["error"])
        self.assertFalse(self.cfg.copilot_config.exists())
        self.assertFalse(self.call("/config")[1]["copilot"]["configured"])
        self.assert_no_token_leaked()

    def test_poll_without_start_and_denied(self):
        self.assertEqual(self.post("/copilot/device/poll")[1]["state"], "error")
        self.github.state.oauth_replies = [{"error": "access_denied"}]
        self.post("/copilot/device/start")
        self.assertEqual(self.post("/copilot/device/poll")[1]["state"], "denied")

    def test_start_upstream_failure_is_502(self):
        self.app.copilot.github_url = f"http://127.0.0.1:{free_port()}"
        status, body = self.post("/copilot/device/start")
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "upstream_error")

    def test_routes_need_the_token_and_take_no_body(self):
        for path in ("/copilot/device/start", "/copilot/device/poll", "/copilot/remove"):
            status, body = self.call(path, {}, method="POST", auth=False)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["error"]["code"], "unauthorized")
        # A POST without Content-Length (curl -X POST) is accepted.
        req = urllib.request.Request(self.url + "/copilot/remove", method="POST",
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)


class ProviderConfigTests(CopilotCase):
    def test_config_reports_providers(self):
        body = self.call("/config")[1]
        self.assertEqual(body["provider"], "local")
        self.assertEqual(body["model"]["selected"], MODEL)
        self.assertEqual(body["copilot"], {"configured": False, "user": "", "last4": "",
                                           "saved_at": "", "model": "gpt-4o", "models": [],
                                           "error": None})

    def test_switch_refused_when_signed_out(self):
        status, body = self.post("/config", {"provider": "copilot"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], {"code": "invalid_request",
                                         "message": "sign in to GitHub Copilot first"})
        status, body = self.post("/config", {"provider": "cloud"})
        self.assertEqual(status, 400)
        self.assertEqual(self.post("/config", {"bogus": 1})[0], 400)

    def test_copilot_model_validation(self):
        status, body = self.post("/config", {"copilot_model": "claude-sonnet-4"})
        self.assertEqual((status, body["error"]["message"]), (400, "sign in to GitHub Copilot first"))
        self.sign_in()
        for bad in ("o3-mini", "gpt-4.1-hidden", "text-embedding-3-small", "", 5):
            status, body = self.post("/config", {"copilot_model": bad})
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["error"]["code"], "invalid_request")
        status, body = self.post("/config", {"copilot_model": "claude-sonnet-4"})
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["copilot"]["model"], "claude-sonnet-4")
        self.assertEqual(body["config"]["provider"], "local")  # the model alone doesn't switch

    def test_failed_validation_changes_nothing(self):
        self.sign_in()
        status, _ = self.post("/config", {"provider": "copilot", "copilot_model": "o3-mini"})
        self.assertEqual(status, 400)
        self.assertEqual(self.call("/config")[1]["provider"], "local")

    def test_remove_switches_back_to_local(self):
        self.use_copilot()
        status, body = self.post("/copilot/remove")
        self.assertEqual(status, 200)
        self.assertFalse(body["configured"])
        self.assertEqual(body["models"], [])
        self.assertFalse(self.cfg.copilot_config.exists())
        self.assertEqual(self.call("/config")[1]["provider"], "local")
        self.assertEqual(json.loads(self.cfg.settings_path.read_text())["provider"], "local")
        self.assertEqual(self.call("/health")[1]["provider"], "local")

    def test_models_follow_the_active_provider(self):
        self.use_copilot()
        body = self.call("/models")[1]
        self.assertEqual(body["provider"], "copilot")
        self.assertEqual(body["selected"], "gpt-4o")
        self.assertEqual([m["id"] for m in body["data"]], VISION_IDS)
        self.assertEqual(sorted(body["installed"]), sorted([MODEL, MODEL2]))
        self.assertEqual(self.call("/v1/models")[1]["data"][0]["id"], "gpt-4o")
        self.post("/config", {"provider": "local"})
        body = self.call("/models")[1]
        self.assertEqual((body["provider"], body["selected"]), ("local", MODEL))


class CopilotChatTests(CopilotCase):
    def test_chat_goes_to_copilot_unchanged_except_model(self):
        self.use_copilot()
        sent = self.chat(SMALL)
        status, reply = self.post("/v1/chat/completions", sent)
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["choices"][0]["message"]["content"], REPLY)  # not rescaled
        forwarded = self.github.state.chats[-1]
        self.assertEqual(forwarded["body"]["model"], "gpt-4o")
        self.assertEqual(forwarded["body"]["messages"], sent["messages"])  # no system hint
        self.assertEqual(forwarded["body"]["temperature"], 0.0)
        self.assertEqual(forwarded["headers"]["Copilot-Vision-Request"], "true")
        self.assertEqual(forwarded["headers"]["Editor-Version"], copilot.EDITOR_HEADERS["Editor-Version"])
        self.assertEqual(forwarded["headers"]["Copilot-Integration-Id"], "vscode-chat")
        self.assertEqual(self.resized, [])
        self.assertEqual(self.ollama.state.chats, [])

    def test_chat_uses_the_selected_copilot_model(self):
        self.use_copilot()
        self.post("/config", {"copilot_model": "claude-sonnet-4"})
        self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(self.github.state.chats[-1]["body"]["model"], "claude-sonnet-4")

    def test_401_refreshes_the_token_once(self):
        self.use_copilot()
        self.assertEqual(self.post("/v1/chat/completions", self.chat(SMALL))[0], 200)
        exchanges = len(self.github.state.token_requests)
        self.github.state.revoked.add(self.github.state.issued[-1])
        status, _ = self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.github.state.token_requests), exchanges + 1)
        self.assertEqual(len(self.github.state.chats), 2)

    def test_persistent_401_is_returned_after_one_retry(self):
        self.use_copilot()
        self.post("/v1/chat/completions", self.chat(SMALL))
        exchanges = len(self.github.state.token_requests)
        with mock.patch.object(self.github.state, "revoked",
                               new=type("All", (), {"__contains__": lambda s, x: True})()):
            status, body = self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "upstream_error")
        self.assertEqual(len(self.github.state.token_requests), exchanges + 1)

    def test_copilot_errors(self):
        self.use_copilot()
        self.github.state.chat_status = 429
        status, body = self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual((status, body["error"]["code"]), (429, "upstream_error"))
        self.assertIn("copilot says no", body["error"]["message"])
        self.assertEqual(self.post("/v1/chat/completions", {**self.chat(SMALL), "stream": True})[0], 400)
        self.github.close()  # the cached token still points at the dead API
        status, body = self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual((status, body["error"]["code"]), (503, "model_unavailable"))

    def test_not_signed_in_is_503(self):
        self.use_copilot()
        self.cfg.copilot_config.unlink()  # signed out behind the service's back
        status, body = self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], {"code": "model_unavailable",
                                         "message": "GitHub Copilot is not signed in"})

    def test_local_still_uses_the_qwen_pipeline(self):
        self.use_copilot()
        self.post("/config", {"provider": "local"})
        self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(self.ollama.state.chats[-1]["model"], MODEL)
        self.assertEqual(self.ollama.state.chats[-1]["messages"][0]["role"], "system")


class ProviderStatusTests(CopilotCase):
    def test_health_and_devices_reflect_provider(self):
        self.use_copilot()
        self.app.cfg = replace(self.app.cfg, ollama_port=free_port())  # Ollama gone: no matter
        health = self.call("/health", auth=False)[1]
        self.assertEqual((health["ok"], health["provider"], health["model"]), (True, "copilot", "gpt-4o"))
        self.assertEqual(health["ollama"], "unreachable")
        device = self.call("/devices")[1]["devices"][0]
        self.assertEqual((device["state"], device["model"]), ("ready", "gpt-4o"))
        self.assertEqual(device["details"]["provider"], "copilot")
        self.cfg.copilot_config.unlink()
        self.assertFalse(self.call("/health", auth=False)[1]["ok"])
        self.assertEqual(self.call("/devices")[1]["devices"][0]["state"], "offline")

    def test_health_never_calls_github_for_copilot(self):
        self.use_copilot()
        self.github.close()
        self.assertTrue(self.call("/health", auth=False)[1]["ok"])

    def test_set_model_job_uses_the_active_provider(self):
        self.use_copilot()
        _, body = self.post("/jobs", {"action": "set-model", "params": {"model": "claude-sonnet-4"}})
        self.assertEqual(body["job"]["state"], "passed")
        self.assertEqual(body["job"]["result"], {"previous": "gpt-4o", "current": "claude-sonnet-4"})
        _, body = self.post("/jobs", {"action": "set-model", "params": {"model": MODEL2}})
        self.assertEqual(body["job"]["state"], "failed")
        self.assertEqual(body["job"]["error"]["code"], "model_unavailable")
        self.assertEqual(self.call("/config")[1]["model"]["selected"], MODEL)

    def test_supervisor_preloads_only_for_local(self):
        self.use_copilot()
        supervisor = gateway.Supervisor(self.app, mock.Mock())
        with mock.patch.object(gateway.runtime, "preload") as preload:
            supervisor._preload()
            preload.assert_not_called()
            self.post("/config", {"provider": "local"})
            supervisor._preload()
            preload.assert_called_once()

    def test_ask_works_with_copilot(self):
        self.use_copilot()
        image = self.runtime_dir / "shot.png"
        image.write_bytes(FIXTURE)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_ask(replace(self.cfg, gateway_port=self.port),
                        argparse.Namespace(image=str(image), prompt="find it"))
        self.assertEqual(out.getvalue().strip(), REPLY)
        self.assertEqual(self.github.state.chats[-1]["body"]["model"], "gpt-4o")


class PersistenceTests(CopilotCase):
    def test_selection_survives_a_restart(self):
        self.sign_in()
        self.post("/config", {"model": MODEL2, "copilot_model": "claude-sonnet-4",
                              "provider": "copilot"})
        self.assertEqual(stat.S_IMODE(self.cfg.settings_path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.cfg.settings_path.read_text()),
                         {"provider": "copilot", "model": MODEL2, "copilot_model": "claude-sonnet-4",
                          "remote_url": "", "remote_model": "", "remote_resize": True})
        self.start_gateway()  # same runtime dir, VTAI_MODEL still MODEL
        body = self.call("/config")[1]
        self.assertEqual(body["provider"], "copilot")
        self.assertEqual(body["model"]["selected"], MODEL2)
        self.assertEqual(body["copilot"]["model"], "claude-sonnet-4")
        self.assertEqual(self.call("/health", auth=False)[1]["model"], "claude-sonnet-4")

    def test_job_selection_is_persisted(self):
        self.post("/jobs", {"action": "set-model", "params": {"model": MODEL2}})
        self.start_gateway()
        self.assertEqual(self.call("/health", auth=False)[1]["model"], MODEL2)

    def test_no_file_means_vtai_model(self):
        self.assertFalse(self.cfg.settings_path.exists())
        self.assertEqual(self.app.cfg.model, MODEL)
        self.assertEqual(self.app.provider, "local")

    def test_uninstalled_persisted_model_falls_back(self):
        copilot.write_private_json(self.cfg.settings_path,
                                   {"provider": "local", "model": "gone:1", "copilot_model": "gpt-4o"})
        self.start_gateway()
        self.assertEqual(self.app.cfg.model, "gone:1")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.app.reconcile_local_model()
        self.assertEqual(self.app.cfg.model, MODEL)
        self.assertIn("no longer installed", err.getvalue())

    def test_persisted_copilot_without_sign_in_starts_local(self):
        copilot.write_private_json(self.cfg.settings_path,
                                   {"provider": "copilot", "model": MODEL, "copilot_model": "gpt-4o"})
        with contextlib.redirect_stderr(io.StringIO()):
            self.start_gateway()
        self.assertEqual(self.call("/health", auth=False)[1]["provider"], "local")

    def test_copilot_config_override(self):
        cfg = config.load({"VTAI_RUNTIME_DIR": str(self.runtime_dir),
                           "VTAI_COPILOT_CONFIG": str(self.runtime_dir / "elsewhere" / "c.json")})
        self.assertEqual(cfg.copilot_config, (self.runtime_dir / "elsewhere" / "c.json").resolve())
        self.assertEqual(cfg.settings_path, self.runtime_dir.resolve() / "settings.json")


class StatusJsonCopilotTests(unittest.TestCase):
    def test_status_json_reports_provider_and_copilot(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "VTAI_RUNTIME_DIR": tmp, "VTAI_PORT": str(free_port()),
                   "VTAI_OLLAMA_PORT": str(free_port())}
            env.pop("VTAI_COPILOT_CONFIG", None)
            run = lambda: json.loads(subprocess.run(
                [sys.executable, str(BIN), "status", "--json"], env=env,
                capture_output=True, text=True, timeout=30).stdout)
            report = run()
            self.assertEqual(report["provider"], "local")
            self.assertEqual(report["copilot"], {"configured": False, "user": ""})
            copilot.write_private_json(Path(tmp) / "copilot.json",
                                       {"oauth_token": OAUTH_TOKEN, "user": LOGIN, "saved_at": "x"})
            copilot.write_private_json(Path(tmp) / "settings.json",
                                       {"provider": "copilot", "model": MODEL2,
                                        "copilot_model": "gpt-4o"})
            report = run()
            self.assertEqual(report["provider"], "copilot")
            self.assertEqual(report["copilot"], {"configured": True, "user": LOGIN})
            self.assertEqual(report["selected"], MODEL2)  # still the local model
            self.assertNotIn(OAUTH_TOKEN, json.dumps(report))


if __name__ == "__main__":
    unittest.main()


class UrlHardeningTests(unittest.TestCase):
    def test_request_json_refuses_non_http(self):
        for url in ("file:///etc/passwd", "ftp://h/x", "gopher://h"):
            with self.assertRaises(copilot.CopilotError):
                copilot.request_json(url)

    def test_token_exchange_endpoint_must_be_trusted(self):
        client = copilot.CopilotClient("/nonexistent/copilot.json", github_url="https://github.com",
                                       github_api="https://api.github.com",
                                       default_api="https://api.githubcopilot.com")
        self.assertEqual(client._trusted_api("https://api.individual.githubcopilot.com/"),
                         "https://api.individual.githubcopilot.com")
        for bad in ("http://evil.example", "file:///tmp/x", "https://user@evil.example", None, ""):
            self.assertEqual(client._trusted_api(bad), "https://api.githubcopilot.com", bad)
        local = copilot.CopilotClient("/x.json", github_api="http://127.0.0.1:9",
                                      default_api="http://127.0.0.1:9")
        self.assertEqual(local._trusted_api("http://127.0.0.1:9"), "http://127.0.0.1:9")
