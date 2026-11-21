"""The remote provider: an OpenAI-compatible server at a user-entered address. Uses
fake_ollama, fake_github (for the shared CopilotCase harness) and fake_remote, a stdlib
stand-in for llama.cpp's llama-server serving only GET /v1/models and
POST /v1/chat/completions."""
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.fake_ollama import MODEL, MODEL2
from tests.fake_remote import FakeRemote, REMOTE_MODEL, REMOTE_MODEL2, REMOTE_REPLY
from tests.test_copilot import BIN, SMALL, CopilotCase, free_port
from monkeytester_ai import config, copilot, remote

KEY = "sk-remote-0123456789abcd"


class RemoteCase(CopilotCase):
    def setUp(self):
        self.remote = FakeRemote()
        self.addCleanup(self.remote.close)
        super().setUp()

    def assert_key_not_leaked(self, key=KEY):
        for text in self.responses:
            self.assertNotIn(key, text)

    def use_remote(self, resize=None, key=None):
        payload = {"remote_url": self.remote.url, "remote_model": REMOTE_MODEL,
                   "provider": "remote"}
        if resize is not None:
            payload["remote_resize"] = resize
        if key is not None:
            payload["remote_api_key"] = key
        status, body = self.post("/config", payload)
        self.assertEqual(status, 200, body)
        return body

    def settings(self):
        return json.loads(self.cfg.settings_path.read_text())


class NormalizeUrlTests(unittest.TestCase):
    def test_suffixes_and_slashes_are_stripped(self):
        for given in ("http://h:8080", "http://h:8080/", "http://h:8080/v1", "http://h:8080/v1/",
                      "http://h:8080/v1/chat/completions", "http://h:8080/v1/chat/completions/",
                      "  http://h:8080/V1  "):
            self.assertEqual(remote.normalize_url(given), "http://h:8080", given)
        self.assertEqual(remote.normalize_url("https://gpu.lan/llm/v1"), "https://gpu.lan/llm")

    def test_bad_addresses(self):
        for bad in ("", "h:8080", "ftp://h", "http://", "http://h:99999", "http://h:8080?x=1",
                    "http://user:pw@h:8080", "http://h#frag", "http://h 1", 5, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                remote.normalize_url(bad)


class RemoteConfigTests(RemoteCase):
    def test_config_reports_an_unset_remote(self):
        body = self.call("/config")[1]
        self.assertEqual(body["remote"], {"url": "", "model": "", "models": [], "resize": True,
                                          "key": {"configured": False, "last4": ""},
                                          "error": None})
        self.assertEqual(body["provider"], "local")

    def test_url_is_normalised_validated_and_listed(self):
        status, body = self.post("/config", {"remote_url": self.remote.url + "/v1/chat/completions"})
        self.assertEqual(status, 200, body)
        block = body["config"]["remote"]
        self.assertEqual(block["url"], self.remote.url)
        self.assertEqual(block["models"], [{"id": REMOTE_MODEL}, {"id": REMOTE_MODEL2}])
        self.assertEqual((block["model"], block["error"]), ("", None))
        self.assertEqual(body["config"]["provider"], "local")  # the address alone doesn't switch
        self.assertEqual(self.remote.state.model_requests()[0]["path"], "/v1/models")
        self.assertEqual(self.settings()["remote_url"], self.remote.url)

    def test_unreachable_url_is_400_and_stores_nothing(self):
        dead = f"http://127.0.0.1:{free_port()}"
        status, body = self.post("/config", {"remote_url": dead, "remote_resize": False})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertTrue(body["error"]["message"].startswith(f"could not reach {dead}/v1/models: "),
                        body)
        self.assertFalse(self.cfg.settings_path.exists())
        self.assertEqual(self.call("/config")[1]["remote"]["resize"], True)

    def test_not_an_openai_server_is_400(self):
        # The fake Ollama has no /v1/models, like any server that isn't OpenAI-compatible.
        status, body = self.post("/config", {"remote_url": f"http://127.0.0.1:{self.ollama.port}"})
        self.assertEqual(status, 400)
        self.assertIn("/v1/models: HTTP 404", body["error"]["message"])
        status, body = self.post("/config", {"remote_url": "not a url"})
        self.assertEqual(status, 400)
        self.assertEqual(self.post("/config", {"remote_url": 5})[0], 400)

    def test_remote_model_validation(self):
        status, body = self.post("/config", {"remote_model": REMOTE_MODEL})
        self.assertEqual((status, body["error"]["message"]), (400, "set remote_url first"))
        self.post("/config", {"remote_url": self.remote.url})
        for bad in ("nope.gguf", "", 5, None):
            status, body = self.post("/config", {"remote_model": bad})
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["error"]["code"], "invalid_request")
        status, body = self.post("/config", {"remote_model": REMOTE_MODEL2})
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["remote"]["model"], REMOTE_MODEL2)
        self.assertEqual(body["config"]["provider"], "local")
        self.assertEqual(self.post("/config", {"remote_resize": "no"})[0], 400)
        status, body = self.post("/config", {"remote_resize": False})
        self.assertEqual((status, body["config"]["remote"]["resize"]), (200, False))

    def test_provider_remote_refused_until_url_and_model_are_set(self):
        status, body = self.post("/config", {"provider": "remote"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["message"], "set remote_url and remote_model first")
        self.post("/config", {"remote_url": self.remote.url})
        self.assertEqual(self.post("/config", {"provider": "remote"})[0], 400)
        status, body = self.post("/config", {"remote_model": REMOTE_MODEL, "provider": "remote"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["config"]["provider"], "remote")

    def test_failed_validation_changes_nothing(self):
        status, _ = self.post("/config", {"remote_url": self.remote.url, "remote_model": "nope",
                                          "provider": "remote", "remote_api_key": KEY})
        self.assertEqual(status, 400)
        self.assertFalse(self.cfg.settings_path.exists())
        self.assertFalse(self.cfg.remote_config.exists())
        body = self.call("/config")[1]
        self.assertEqual((body["provider"], body["remote"]["url"]), ("local", ""))

    def test_key_is_write_only_private_and_sent_as_bearer(self):
        self.use_remote(key=KEY)
        self.assertEqual(self.cfg.remote_config, self.runtime_dir / "remote.json")
        self.assertEqual(stat.S_IMODE(self.cfg.remote_config.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.cfg.remote_config.read_text())["api_key"], KEY)
        self.assertNotIn(KEY, self.cfg.settings_path.read_text())
        body = self.call("/config")[1]
        self.assertEqual(body["remote"]["key"], {"configured": True, "last4": KEY[-4:]})
        self.assertEqual(self.remote.state.model_requests()[-1]["headers"]["Authorization"],
                         f"Bearer {KEY}")
        self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(self.remote.state.chats[-1]["headers"]["Authorization"], f"Bearer {KEY}")
        self.call("/models")
        self.call("/health", auth=False)
        self.call("/devices")
        self.assert_key_not_leaked()
        for removal in (None, ""):
            self.post("/config", {"remote_api_key": KEY})
            status, body = self.post("/config", {"remote_api_key": removal})
            self.assertEqual(status, 200)
            self.assertEqual(body["config"]["remote"]["key"], {"configured": False, "last4": ""})
            self.assertFalse(self.cfg.remote_config.exists())
        self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertNotIn("Authorization", self.remote.state.chats[-1]["headers"])
        self.assertEqual(self.post("/config", {"remote_api_key": 5})[0], 400)
        self.assert_key_not_leaked()

    def test_short_key_shows_no_last4(self):
        self.post("/config", {"remote_api_key": "abc"})
        self.assertEqual(self.call("/config")[1]["remote"]["key"], {"configured": True, "last4": ""})

    def test_key_protected_server(self):
        self.remote.state.required_key = KEY
        status, body = self.post("/config", {"remote_url": self.remote.url})
        self.assertEqual(status, 400)
        self.assertTrue(body["error"]["message"].startswith("the remote server rejected the API key"))
        status, body = self.post("/config", {"remote_url": self.remote.url,
                                             "remote_api_key": "sk-wrong-key-0000"})
        self.assertEqual((status, body["error"]["message"]),
                         (400, "the remote server rejected the API key"))
        self.assertFalse(self.cfg.remote_config.exists())
        # The key from the same request is used for the check.
        status, body = self.post("/config", {"remote_url": self.remote.url, "remote_api_key": KEY,
                                             "remote_model": REMOTE_MODEL, "provider": "remote"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.post("/v1/chat/completions", self.chat(SMALL))[0], 200)
        # A key removed behind a protected server: GET /config says why the list is empty.
        self.post("/config", {"remote_api_key": None})
        block = self.call("/config")[1]["remote"]
        self.assertEqual(block["models"], [])
        self.assertTrue(block["error"].startswith("the remote server rejected the API key"))
        self.assert_key_not_leaked()

    def test_clearing_the_url_switches_to_local(self):
        self.use_remote()
        status, body = self.post("/config", {"remote_url": ""})
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["provider"], "local")
        self.assertEqual((body["config"]["remote"]["url"], body["config"]["remote"]["model"]), ("", ""))
        self.assertEqual((self.settings()["provider"], self.settings()["remote_url"]), ("local", ""))
        self.assertEqual(self.call("/health", auth=False)[1]["provider"], "local")
        self.assertEqual(self.post("/config", {"remote_url": "", "provider": "remote"})[0], 400)

    def test_moving_to_a_server_without_the_model(self):
        self.use_remote()
        other = FakeRemote()
        self.addCleanup(other.close)
        other.state.models = ["llava-1.6.gguf"]
        status, body = self.post("/config", {"remote_url": other.url})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["config"]["remote"]["model"], "")
        self.assertEqual(body["config"]["provider"], "local")
        status, body = self.post("/config", {"remote_url": self.remote.url,
                                             "remote_model": REMOTE_MODEL2, "provider": "remote"})
        self.assertEqual((status, body["config"]["remote"]["model"]), (200, REMOTE_MODEL2))
        # Re-entering the same address keeps the model.
        status, body = self.post("/config", {"remote_url": self.remote.url + "/"})
        self.assertEqual((body["config"]["provider"], body["config"]["remote"]["model"]),
                         ("remote", REMOTE_MODEL2))

    def test_get_config_with_the_remote_down_is_quick(self):
        self.use_remote()
        self.remote.close()
        started = time.monotonic()
        status, body = self.call("/config")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(status, 200)
        self.assertEqual(body["remote"]["models"], [])
        self.assertIn("could not reach", body["remote"]["error"])
        self.assertEqual(body["remote"]["model"], REMOTE_MODEL)
        self.assertEqual(body["provider"], "remote")

    def test_get_config_gives_up_on_a_silent_server(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)  # accepts connections, never answers
        self.addCleanup(listener.close)
        self.app.remote_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        with mock.patch.object(remote, "MODELS_TIMEOUT_SECS", 0.5):
            started = time.monotonic()
            status, body = self.call("/config")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(status, 200)
        self.assertIn("could not reach", body["remote"]["error"])


class RemoteChatTests(RemoteCase):
    def test_resize_on_runs_the_qwen_pipeline(self):
        self.use_remote(key=KEY)
        status, reply = self.post("/v1/chat/completions", self.chat(SMALL, model="whatever"))
        self.assertEqual(status, 200, reply)
        forwarded = self.remote.state.chats[-1]
        self.assertEqual(forwarded["body"]["model"], REMOTE_MODEL)
        self.assertEqual(forwarded["headers"]["Authorization"], f"Bearer {KEY}")
        _, new_w, new_h = self.resized[0]
        messages = forwarded["body"]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn(f"{new_w}x{new_h}", messages[0]["content"])
        content = json.loads(reply["choices"][0]["message"]["content"])
        self.assertEqual(content["coordinates"], [round(100 * 1170 / new_w), round(200 * 2532 / new_h)])
        self.assertEqual(self.ollama.state.chats, [])

    def test_resize_off_passes_through_except_model(self):
        self.use_remote(resize=False)
        sent = self.chat(SMALL)
        status, reply = self.post("/v1/chat/completions", sent)
        self.assertEqual(status, 200, reply)
        forwarded = self.remote.state.chats[-1]
        self.assertEqual(forwarded["body"]["model"], REMOTE_MODEL)
        self.assertEqual(forwarded["body"]["messages"], sent["messages"])  # no hint, no resize
        self.assertEqual(forwarded["body"]["temperature"], 0.0)
        self.assertNotIn("Authorization", forwarded["headers"])
        self.assertEqual(self.resized, [])
        self.assertEqual(reply["choices"][0]["message"]["content"], REMOTE_REPLY)  # not rescaled

    def test_remote_errors(self):
        self.use_remote()
        self.remote.state.chat_status = 500
        status, body = self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual((status, body["error"]["code"]), (500, "upstream_error"))
        self.assertIn("remote says no", body["error"]["message"])
        self.assertEqual(self.post("/v1/chat/completions", {**self.chat(SMALL), "stream": True})[0], 400)
        self.remote.close()
        status, body = self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual((status, body["error"]["code"]), (503, "model_unavailable"))
        self.assertIn(self.remote.url, body["error"]["message"])

    def test_wrong_key_on_chat_is_upstream_401(self):
        self.use_remote(key=KEY)
        self.remote.state.required_key = "sk-rotated-on-the-server"
        status, body = self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual((status, body["error"]["code"]), (401, "upstream_error"))
        self.assertIn("Invalid API Key", body["error"]["message"])

    def test_local_is_unchanged_after_using_remote(self):
        self.use_remote(resize=False)
        self.post("/config", {"provider": "local"})
        self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(self.ollama.state.chats[-1]["model"], MODEL)
        self.assertEqual(self.ollama.state.chats[-1]["messages"][0]["role"], "system")
        self.assertEqual(len(self.remote.state.chats), 0)


class RemoteStatusTests(RemoteCase):
    def test_health_devices_and_models_reflect_remote(self):
        self.use_remote()
        self.app.cfg = replace(self.app.cfg, ollama_port=free_port())  # Ollama gone: no matter
        self.remote.close()  # /health and /devices never touch the network for remote
        health = self.call("/health", auth=False)[1]
        self.assertEqual((health["ok"], health["provider"], health["model"], health["model_loaded"]),
                         (True, "remote", REMOTE_MODEL, True))
        self.assertEqual(health["ollama"], "unreachable")
        device = self.call("/devices")[1]["devices"][0]
        self.assertEqual((device["state"], device["model"], device["details"]["provider"]),
                         ("ready", REMOTE_MODEL, "remote"))
        self.assertEqual(self.call("/v1/models")[1]["data"][0]["id"], REMOTE_MODEL)

    def test_models_list_the_remote_server(self):
        self.use_remote()
        body = self.call("/models")[1]
        self.assertEqual((body["provider"], body["selected"], body["error"]), ("remote", REMOTE_MODEL, None))
        self.assertEqual([m["id"] for m in body["data"]], [REMOTE_MODEL, REMOTE_MODEL2])
        self.assertEqual(sorted(body["installed"]), sorted([MODEL, MODEL2]))
        self.remote.close()
        body = self.call("/models")[1]
        self.assertEqual(body["data"], [])
        self.assertIn("could not reach", body["error"])

    def test_set_model_job_uses_the_remote_list(self):
        self.use_remote()
        _, body = self.post("/jobs", {"action": "set-model", "params": {"model": REMOTE_MODEL2}})
        self.assertEqual(body["job"]["state"], "passed", body)
        self.assertEqual(body["job"]["result"], {"previous": REMOTE_MODEL, "current": REMOTE_MODEL2})
        self.assertEqual(self.settings()["remote_model"], REMOTE_MODEL2)
        _, body = self.post("/jobs", {"action": "set-model", "params": {"model": MODEL2}})
        self.assertEqual(body["job"]["state"], "failed")
        self.assertEqual(body["job"]["error"]["code"], "model_unavailable")
        self.assertEqual(self.call("/config")[1]["model"]["selected"], MODEL)


class RemotePersistenceTests(RemoteCase):
    def test_settings_survive_a_restart(self):
        self.use_remote(resize=False, key=KEY)
        self.assertEqual(stat.S_IMODE(self.cfg.settings_path.stat().st_mode), 0o600)
        self.assertEqual(self.settings(), {"provider": "remote", "model": MODEL,
                                           "copilot_model": "gpt-4o",
                                           "remote_url": self.remote.url,
                                           "remote_model": REMOTE_MODEL, "remote_resize": False})
        self.start_gateway()
        body = self.call("/config")[1]
        self.assertEqual(body["provider"], "remote")
        self.assertEqual((body["remote"]["url"], body["remote"]["model"], body["remote"]["resize"]),
                         (self.remote.url, REMOTE_MODEL, False))
        self.assertEqual(body["remote"]["key"], {"configured": True, "last4": KEY[-4:]})
        self.post("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(self.remote.state.chats[-1]["headers"]["Authorization"], f"Bearer {KEY}")
        self.assert_key_not_leaked()

    def test_persisted_remote_without_address_starts_local(self):
        for stored in ({"provider": "remote", "model": MODEL, "remote_url": "", "remote_model": "x"},
                       {"provider": "remote", "model": MODEL, "remote_url": self.remote.url,
                        "remote_model": ""}):
            copilot.write_private_json(self.cfg.settings_path, stored)
            with mock.patch("sys.stderr"):
                self.start_gateway()
            self.assertEqual(self.call("/health", auth=False)[1]["provider"], "local", stored)

    def test_remote_config_override(self):
        cfg = config.load({"VTAI_RUNTIME_DIR": str(self.runtime_dir),
                           "VTAI_REMOTE_CONFIG": str(self.runtime_dir / "elsewhere" / "r.json")})
        self.assertEqual(cfg.remote_config, (self.runtime_dir / "elsewhere" / "r.json").resolve())
        self.assertEqual(config.load({"VTAI_RUNTIME_DIR": str(self.runtime_dir)}).remote_config,
                         self.runtime_dir.resolve() / "remote.json")


class StatusJsonRemoteTests(unittest.TestCase):
    def test_status_json_reports_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "VTAI_RUNTIME_DIR": tmp, "VTAI_PORT": str(free_port()),
                   "VTAI_OLLAMA_PORT": str(free_port())}
            env.pop("VTAI_COPILOT_CONFIG", None)
            env.pop("VTAI_REMOTE_CONFIG", None)
            run = lambda: json.loads(subprocess.run(
                [sys.executable, str(BIN), "status", "--json"], env=env,
                capture_output=True, text=True, timeout=30).stdout)
            report = run()
            self.assertEqual(report["remote"], {"url": "", "model": ""})
            copilot.write_private_json(Path(tmp) / "settings.json", {
                "provider": "remote", "model": MODEL2, "copilot_model": "gpt-4o",
                "remote_url": "http://gpu.lan:8080", "remote_model": REMOTE_MODEL,
                "remote_resize": True})
            copilot.write_private_json(Path(tmp) / "remote.json", {"api_key": KEY})
            report = run()
            self.assertEqual(report["provider"], "remote")
            self.assertEqual(report["remote"], {"url": "http://gpu.lan:8080", "model": REMOTE_MODEL})
            self.assertEqual(report["selected"], MODEL2)  # still the local model
            self.assertNotIn(KEY, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
