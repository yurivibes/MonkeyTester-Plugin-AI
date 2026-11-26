"""Plugin-contract and config-surface tests: /health fields, /devices, /actions,
/jobs, /models, /config. Same fake-Ollama pattern as the other test modules."""
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.fake_ollama import FakeOllama, MODEL, MODEL2
from monkeytester_ai import config, gateway, runtime

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "monkeytester-ai"
FIXTURE = (Path(__file__).parent / "fixtures" / "target.png").read_bytes()
SMALL = base64.b64encode(FIXTURE).decode()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PluginCase(unittest.TestCase):
    token = ""

    def setUp(self):
        self.fake = FakeOllama()
        self.addCleanup(self.fake.close)
        tmp = tempfile.TemporaryDirectory()  # settings.json and copilot.json land here
        self.addCleanup(tmp.cleanup)
        self.runtime_dir = Path(tmp.name)
        self.cfg = replace(config.load({"MTAI_RUNTIME_DIR": tmp.name}), ollama_port=self.fake.port,
                           token=self.token)
        self.resized = []

        def resizer(data, fmt, width, height):
            self.resized.append((fmt, width, height))
            return b"resized:" + data[:4]

        self.app = gateway.Gateway(self.cfg, resizer=resizer)
        handler = gateway.make_handler(self.app)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, path, payload=None, headers=None, method=None, raw=None):
        data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
        req = urllib.request.Request(self.url + path, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    @staticmethod
    def chat(*images, model="anything"):
        parts = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{i}"}} for i in images]
        return {"model": model, "messages": [{"role": "user", "content": parts + [{"type": "text", "text": "q"}]}]}

    def point_at_dead_port(self):
        """Make the gateway see Ollama as unreachable without closing the fake."""
        self.app.cfg = replace(self.cfg, ollama_port=free_port())


class ContractTests(PluginCase):
    def test_health_reports_plugin_identity(self):
        _, health = self.call("/health")
        self.assertEqual(health["contract"], "monkeytester-plugin")
        self.assertEqual(health["contract_version"], 1)
        self.assertEqual(health["plugin"], {"name": "AI", "default_name": "AI",
                                            "renamable": True, "version": config.SERVICE_VERSION})
        self.assertEqual(health["service"], "monkeytester-ai")
        self.assertEqual(health["devices"], 1)
        self.assertEqual(health["capabilities"], [])
        self.assertIsNone(health["error"])

    def test_device_ready(self):
        status, body = self.call("/devices")
        self.assertEqual(status, 200)
        self.assertIsNone(body["error"])
        device = body["devices"][0]
        self.assertEqual(device["id"], "gateway")
        self.assertEqual(device["kind"], "visual-model")
        self.assertEqual(device["model"], MODEL)
        self.assertEqual(device["state"], "ready")
        self.assertTrue(device["details"]["model_loaded"])

    def test_device_offline_when_ollama_gone(self):
        self.point_at_dead_port()
        body = self.call("/devices")[1]
        self.assertEqual(body["devices"][0]["state"], "offline")
        self.assertFalse(body["devices"][0]["details"]["model_loaded"])
        self.assertFalse(self.call("/health")[1]["ok"])

    def test_actions_describe_set_model(self):
        actions = self.call("/actions")[1]["actions"]
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action["name"], "set-model")
        self.assertEqual(action["params"]["model"], {"type": "string", "required": True})
        self.assertEqual(action["default_timeout_ms"], 60000)
        self.assertIs(action["needs_device"], False)


class JobTests(PluginCase):
    def test_set_model_job_passes_and_switches(self):
        status, body = self.call("/jobs", {"action": "set-model", "params": {"model": MODEL2}})
        self.assertEqual(status, 202)
        job = body["job"]
        self.assertEqual(job["state"], "passed")
        self.assertEqual(job["result"], {"previous": MODEL, "current": MODEL2})
        self.assertIsNone(job["error"])
        self.assertEqual(job["device"], "gateway")
        self.call("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(self.fake.state.chats[-1]["model"], MODEL2)
        self.assertEqual(self.call("/health")[1]["model"], MODEL2)

    def test_get_job_with_wait_is_terminal(self):
        job_id = self.call("/jobs", {"action": "set-model", "params": {"model": MODEL}})[1]["job"]["id"]
        status, job = self.call(f"/jobs/{job_id}?wait=1")
        self.assertEqual(status, 200)
        self.assertEqual(job["state"], "passed")

    def test_cancel_and_unknown_job(self):
        job_id = self.call("/jobs", {"action": "set-model", "params": {"model": MODEL}})[1]["job"]["id"]
        status, body = self.call(f"/jobs/{job_id}/cancel", {}, method="POST")
        self.assertEqual(status, 200)
        self.assertEqual(body["job"]["state"], "passed")  # already terminal when it started
        status, body = self.call("/jobs/j-9999")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")

    def test_job_errors_use_contract_shape(self):
        cases = [
            ({"action": "flash", "params": {}}, 404, "unknown_action"),
            ({"action": "set-model"}, 400, "bad_request"),
            ({"action": "set-model", "device": "board-9", "params": {"model": MODEL}},
             404, "unknown_device"),
        ]
        for payload, status, code in cases:
            got, body = self.call("/jobs", payload)
            self.assertEqual(got, status, payload)
            self.assertEqual(body["code"], code)
            self.assertIsInstance(body["error"], str)

    def test_uninstalled_model_fails_the_job(self):
        _, body = self.call("/jobs", {"action": "set-model", "params": {"model": "missing:9"}})
        job = body["job"]
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "model_unavailable")
        self.assertEqual(self.call("/health")[1]["model"], MODEL)  # selection unchanged

    def test_selection_survives_between_requests(self):
        self.call("/jobs", {"action": "set-model", "params": {"model": MODEL2}})
        self.call("/jobs", {"action": "set-model", "params": {"model": MODEL2}})
        self.call("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(self.fake.state.chats[-1]["model"], MODEL2)


class ModelsTests(PluginCase):
    def test_models_reports_installed_and_selected(self):
        body = self.call("/models")[1]
        self.assertEqual(body["object"], "list")
        self.assertEqual(body["selected"], MODEL)
        self.assertEqual(sorted(body["installed"]), sorted([MODEL, MODEL2]))
        self.assertEqual({entry["id"] for entry in body["data"]}, {MODEL, MODEL2})

    def test_v1_models_follows_the_selection(self):
        self.call("/config", {"model": MODEL2})
        self.assertEqual(self.call("/v1/models")[1]["data"][0]["id"], MODEL2)


class ConfigGetTests(PluginCase):
    token = "cafe0000dead1234"

    def test_config_never_shows_the_token_value(self):
        status, body = self.call("/config", headers={"X-Plugin-Token": self.token})
        self.assertEqual(status, 200)
        self.assertEqual(body["token"], {"configured": True, "last4": "1234"})
        self.assertNotIn(self.token, json.dumps(body))
        self.assertEqual(body["model"]["selected"], MODEL)
        self.assertEqual(body["service"], "not_installed")
        self.assertEqual(body["api_version"], config.API_VERSION)

    def test_config_offline_lists_no_models(self):
        self.point_at_dead_port()
        body = self.call("/config", headers={"X-Plugin-Token": self.token})[1]
        self.assertEqual(body["model"]["installed"], [])


class ConfigSetTests(PluginCase):
    token = "cafe0000dead1234"

    def test_config_model_switch(self):
        status, body = self.call("/config", {"model": MODEL2}, {"X-Plugin-Token": self.token})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["config"]["model"]["selected"], MODEL2)
        self.call("/v1/chat/completions", self.chat(SMALL), {"X-Plugin-Token": self.token})
        self.assertEqual(self.fake.state.chats[-1]["model"], MODEL2)

    def test_config_rejects_unknown_model(self):
        status, body = self.call("/config", {"model": "missing:9"}, {"X-Plugin-Token": self.token})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertIn("missing:9", body["error"]["message"])

    def test_config_rejects_port_change(self):
        status, body = self.call("/config", {"port": 9999}, {"X-Plugin-Token": self.token})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertIn("MTAI_PORT", body["error"]["message"])

    def test_token_rotation_returns_the_new_value_exactly_once(self):
        status, body = self.call("/config", {"token": {"generate": True}},
                                 {"Authorization": f"Bearer {self.token}"})
        self.assertEqual(status, 200)
        new = body["new_token"]
        self.assertNotEqual(new, self.token)
        self.assertEqual(len(new), 64)
        self.assertEqual(new[-4:], body["config"]["token"]["last4"])
        # The old token is dead; the new one works under both spellings.
        self.assertEqual(self.call("/config", headers={"X-Plugin-Token": self.token})[0], 401)
        self.assertEqual(self.call("/config", headers={"X-Plugin-Token": new})[0], 200)
        self.assertEqual(self.call("/models", headers={"Authorization": f"Bearer {new}"})[0], 200)


class PluginAuthTests(PluginCase):
    token = "abcd0000efef9999"

    def test_x_plugin_token_accepted_on_plugin_routes(self):
        for path in ("/devices", "/actions", "/models", "/config"):
            self.assertEqual(self.call(path, headers={"X-Plugin-Token": self.token})[0], 200, path)
        status, _ = self.call("/jobs", {"action": "set-model", "params": {"model": MODEL}},
                              {"X-Plugin-Token": self.token})
        self.assertEqual(status, 202)
        status, body = self.call("/devices")
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "unauthorized")


class RenameTests(PluginCase):
    """The renamable plugin name: POST /config {"name"}, /health plugin.name, settings.json."""
    token = "beef0000cafe5678"

    def auth(self):
        return {"X-Plugin-Token": self.token}

    def rename(self, name):
        return self.call("/config", {"name": name}, self.auth())

    def restart(self):
        """A new Gateway on the same runtime dir, as after a service restart."""
        self.app = gateway.Gateway(self.cfg)
        self.httpd.RequestHandlerClass = gateway.make_handler(self.app)

    def health_plugin(self):
        return self.call("/health")[1]["plugin"]

    def test_default_name(self):
        self.assertEqual(self.health_plugin()["name"], "AI")
        body = self.call("/config", headers=self.auth())[1]
        self.assertEqual(body["name"], "AI")
        self.assertEqual(body["default_name"], "AI")
        self.assertFalse(self.cfg.settings_path.exists())

    def test_rename_is_reported_and_persists_across_a_restart(self):
        status, body = self.rename("VISION")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["config"]["name"], "VISION")
        self.assertEqual(body["config"]["default_name"], "AI")
        _, health = self.call("/health")
        self.assertEqual(health["plugin"]["name"], "VISION")
        self.assertEqual(health["plugin"]["default_name"], "AI")
        self.assertIs(health["plugin"]["renamable"], True)
        self.assertEqual(health["service"], "monkeytester-ai")  # the service id never changes
        self.assertEqual(json.loads(self.cfg.settings_path.read_text())["name"], "VISION")
        self.restart()
        self.assertEqual(self.health_plugin()["name"], "VISION")
        self.assertEqual(self.call("/config", headers=self.auth())[1]["name"], "VISION")

    def test_posting_the_default_name_restores_it(self):
        self.rename("VISION")
        status, body = self.rename("AI")
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["name"], "AI")
        self.assertEqual(self.health_plugin()["name"], "AI")
        self.assertNotIn("name", json.loads(self.cfg.settings_path.read_text()))
        self.restart()
        self.assertEqual(self.health_plugin()["name"], "AI")

    def test_bad_names_are_400_and_change_nothing(self):
        self.rename("VISION")
        for bad in ("", " VISION", "VISION ", "-VISION", "_V", "VI SION", "VISION!", "A/B",
                    "X" * 65, "VISION\n", "VISIÓN", "vision", "Vision", "a", "monkeytester-ai",
                    7, None, True, ["VISION"], {"n": 1}):
            status, body = self.rename(bad)
            self.assertEqual(status, 400, repr(bad))
            self.assertEqual(body["error"]["code"], "bad_request", repr(bad))
        self.assertEqual(self.health_plugin()["name"], "VISION")
        self.assertEqual(json.loads(self.cfg.settings_path.read_text())["name"], "VISION")

    def test_bad_name_with_other_keys_changes_nothing(self):
        status, _ = self.call("/config", {"name": "BAD NAME", "model": MODEL2}, self.auth())
        self.assertEqual(status, 400)
        self.assertEqual(self.call("/health")[1]["model"], MODEL)
        self.assertFalse(self.cfg.settings_path.exists())

    def test_edge_names_accepted(self):
        for good in ("A", "9", "A_B-C", "X" * 64):
            status, _ = self.rename(good)
            self.assertEqual(status, 200, good)
            self.assertEqual(self.health_plugin()["name"], good)

    def test_name_alongside_model_change(self):
        status, body = self.call("/config", {"name": "VISION", "model": MODEL2}, self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["name"], "VISION")
        self.assertEqual(body["config"]["model"]["selected"], MODEL2)

    def test_invalid_stored_name_means_default(self):
        self.cfg.settings_path.write_text(json.dumps({"name": "not valid!"}))
        self.restart()
        self.assertEqual(self.health_plugin()["name"], "AI")

    def test_rename_requires_auth(self):
        for headers in ({}, {"X-Plugin-Token": "wrong0000token00"}):
            status, body = self.call("/config", {"name": "VISION"}, headers)
            self.assertEqual(status, 401)
            self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertEqual(self.call("/config")[0], 401)
        self.assertEqual(self.health_plugin()["name"], "AI")
        status, _ = self.call("/config", {"name": "VISION"},
                              {"Authorization": f"Bearer {self.token}"})
        self.assertEqual(status, 200)
        self.assertEqual(self.health_plugin()["name"], "VISION")

    def test_lowercase_name_is_rejected_with_an_uppercase_message(self):
        status, body = self.rename("vision")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")
        self.assertIn("uppercase", body["error"]["message"])
        self.assertEqual(self.health_plugin()["name"], "AI")
        self.assertFalse(self.cfg.settings_path.exists())

    def test_stored_lowercase_name_is_uppercased_on_load(self):
        self.cfg.settings_path.write_text(json.dumps({"name": "vision"}))
        self.restart()
        self.assertEqual(self.health_plugin()["name"], "VISION")
        self.rename("VISION2")
        self.rename("VISION")
        self.assertEqual(json.loads(self.cfg.settings_path.read_text())["name"], "VISION")

    def test_stored_old_default_name_becomes_the_new_default(self):
        for old in ("monkeytester-ai", "MONKEYTESTER-AI", "MonkeyTester-AI"):
            self.cfg.settings_path.write_text(json.dumps({"name": old}))
            self.restart()
            plugin = self.health_plugin()
            self.assertEqual(plugin["name"], "AI", old)
            self.assertEqual(plugin["default_name"], "AI", old)
        self.rename("VISION")
        self.rename("AI")
        self.assertNotIn("name", json.loads(self.cfg.settings_path.read_text()))

    def test_stored_lowercase_ai_is_the_default(self):
        self.cfg.settings_path.write_text(json.dumps({"name": "ai"}))
        self.restart()
        self.assertEqual(self.health_plugin()["name"], "AI")


class StatusJsonTests(unittest.TestCase):
    """Runs the real entry point with fake ollama and asks `status --json`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = {
            "MTAI_RUNTIME_DIR": str(self.tmp / "runtime"),
            "MTAI_PORT": str(free_port()),
            "MTAI_OLLAMA_PORT": str(free_port()),
        }
        cfg = config.load(self.env)
        ollama_dir = cfg.ollama_dir
        ollama_dir.mkdir(parents=True)
        binary = ollama_dir / "ollama"
        fake = Path(__file__).parent / "fake_ollama.py"
        binary.write_text(f"#!/bin/sh\nexec {sys.executable} {fake} \"$@\"\n")
        binary.chmod(0o755)
        (ollama_dir / ".version").write_text(config.OLLAMA_VERSION + "\n")
        self.env = {**os.environ, **self.env}
        self.proc = None
        self.addCleanup(self.stop)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)

    def test_status_json_includes_selected_and_installed(self):
        self.proc = subprocess.Popen([sys.executable, str(BIN), "serve"], env=self.env,
                                     stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.env['MTAI_PORT']}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("gateway did not start")
        out = subprocess.run([sys.executable, str(BIN), "status", "--json"], env=self.env,
                             capture_output=True, text=True, timeout=30)
        report = json.loads(out.stdout)
        self.assertTrue(report["gateway_running"])
        self.assertEqual(report["model"], MODEL)
        self.assertEqual(report["selected"], MODEL)
        self.assertEqual(sorted(report["installed"]), sorted([MODEL, MODEL2]))


if __name__ == "__main__":
    unittest.main()
