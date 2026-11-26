import base64
import json
import platform
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.fake_ollama import FakeOllama, MODEL
from monkeytester_ai import config, gateway, runtime

FIXTURE = (Path(__file__).parent / "fixtures" / "target.png").read_bytes()
SMALL = base64.b64encode(FIXTURE).decode()


class GatewayCase(unittest.TestCase):
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

        handler = gateway.make_handler(gateway.Gateway(self.cfg, resizer=resizer))
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


class ChatTests(GatewayCase):
    def test_coordinates_come_back_in_device_pixels(self):
        status, reply = self.call("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(status, 200)
        fmt, new_w, new_h = self.resized[0]
        self.assertEqual(fmt, "png")
        content = json.loads(reply["choices"][0]["message"]["content"])
        self.assertEqual(content["coordinates"], [round(100 * 1170 / new_w), round(200 * 2532 / new_h)])

    def test_model_name_is_replaced(self):
        self.call("/v1/chat/completions", self.chat(SMALL, model="Qwen_Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"))
        self.assertEqual(self.fake.state.chats[0]["model"], MODEL)

    def test_forwards_resized_image_and_tells_model_its_size(self):
        self.call("/v1/chat/completions", self.chat(SMALL))
        _, new_w, new_h = self.resized[0]
        sent = self.fake.state.chats[0]["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn(f"{new_w}x{new_h}", sent[0]["content"])
        url = sent[1]["content"][0]["image_url"]["url"]
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), b"resized:" + FIXTURE[:4])

    def test_two_images_are_resized_but_reply_is_not_rescaled(self):
        status, reply = self.call("/v1/chat/completions", self.chat(SMALL, SMALL))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.resized), 2)
        self.assertEqual(json.loads(reply["choices"][0]["message"]["content"])["coordinates"], [100, 200])
        self.assertEqual(self.fake.state.chats[0]["messages"][0]["role"], "user")

    def test_text_only_request_matches_settings_test_button(self):
        status, _ = self.call("/v1/chat/completions", {
            "model": "x", "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 5})
        self.assertEqual(status, 200)
        self.assertEqual(self.resized, [])

    def test_non_json_reply_passes_through(self):
        self.fake.state.reply = "The screen shows a login form."
        _, reply = self.call("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(reply["choices"][0]["message"]["content"], "The screen shows a login form.")

    def test_bad_requests(self):
        cases = [
            ({"messages": "nope"}, None),
            ({"messages": [], "stream": True}, None),
            (self.chat("!!!notbase64"), None),
            (self.chat(base64.b64encode(b"GIF89a").decode()), None),
            ({"messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "http://example.com/a.png"}}]}]}, None),
        ]
        for payload, _ in cases:
            status, reply = self.call("/v1/chat/completions", payload)
            self.assertEqual(status, 400, payload)
            self.assertEqual(reply["error"]["code"], "invalid_request")
            self.assertIsInstance(reply["error"]["message"], str)
        self.assertEqual(self.call("/v1/chat/completions", raw=b"{oops")[0], 400)
        self.assertEqual(self.fake.state.chats, [])

    def test_body_limit(self):
        original = gateway.MAX_BODY_BYTES
        gateway.MAX_BODY_BYTES = 10
        self.addCleanup(setattr, gateway, "MAX_BODY_BYTES", original)
        status, _ = self.call("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual(status, 413)

    def test_upstream_error_is_normalised(self):
        self.fake.state.chat_status = 500
        status, reply = self.call("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual((status, reply), (500, {"error": {"code": "upstream_error", "message": "boom"}}))


class RoutingTests(GatewayCase):
    def test_admin_paths_are_blocked(self):
        for path in ("/api/delete", "/api/pull", "/api/generate", "/api/tags"):
            self.assertEqual(self.call(path, {"model": "x"})[0], 404, path)
            self.assertEqual(self.call(path, {"model": "x"}, method="DELETE")[0], 404, path)
        self.assertEqual(self.fake.state.other_paths, [])

    def test_health(self):
        status, health = self.call("/health")
        self.assertEqual(status, 200)
        self.assertEqual(health, {"ok": True, "service": "monkeytester-ai", "api_version": config.API_VERSION, "model": MODEL,
                                  "provider": "local", "model_loaded": True, "ollama": "running",
                                  "contract": "monkeytester-plugin", "contract_version": 1,
                                  "plugin": {"name": "monkeytester-ai",
                                             "default_name": "monkeytester-ai",
                                             "renamable": True,
                                             "version": config.SERVICE_VERSION},
                                  "host": {"os": sys.platform, "hostname": platform.node()},
                                  "devices": 1, "capabilities": [], "error": None, "docs": "/docs"})

    def test_models(self):
        self.assertEqual(self.call("/v1/models")[1]["data"][0]["id"], MODEL)


class AuthTests(GatewayCase):
    token = "s3cret"

    def test_token_required(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "s3cret"}):
            self.assertEqual(self.call("/v1/chat/completions", self.chat(SMALL), headers)[0], 401)
            self.assertEqual(self.call("/v1/models", headers=headers)[0], 401)
        self.assertEqual(self.fake.state.chats, [])

    def test_correct_token(self):
        status, _ = self.call("/v1/chat/completions", self.chat(SMALL), {"Authorization": "Bearer s3cret"})
        self.assertEqual(status, 200)

    def test_health_is_open(self):
        self.assertEqual(self.call("/health")[0], 200)


class BackendDownTests(unittest.TestCase):
    def test_unreachable_backend(self):
        fake = FakeOllama()
        port = fake.port
        fake.close()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = replace(config.load({"MTAI_RUNTIME_DIR": tmp.name}), ollama_port=port)
        gw = gateway.Gateway(cfg, resizer=lambda d, f, w, h: d)
        self.assertEqual(gw.health()["ollama"], "unreachable")
        self.assertFalse(gw.health()["ok"])
        status, reply = gw.chat(json.dumps({"messages": []}).encode())
        self.assertEqual(status, 503)
        self.assertEqual(reply["error"]["code"], "model_unavailable")


class FailureTests(GatewayCase):
    def test_unexpected_failure_is_a_structured_500(self):
        with mock.patch.object(gateway.Gateway, "chat", side_effect=ValueError("surprise")):
            status, reply = self.call("/v1/chat/completions", self.chat(SMALL))
        self.assertEqual((status, reply),
                         (500, {"error": {"code": "internal_error", "message": "internal error: ValueError"}}))

    def test_health_ignores_a_query_string(self):
        self.assertEqual(self.call("/health?probe=1")[0], 200)


class SupervisorTests(unittest.TestCase):
    def test_a_failed_restart_does_not_end_supervision(self):
        app = mock.Mock()  # the running Gateway: Supervisor follows model changes through it
        ollama = mock.Mock()
        ollama.alive.return_value = False
        supervisor = gateway.Supervisor(app, ollama)
        calls = []

        def start():
            calls.append(1)
            if len(calls) >= 2:
                supervisor.stopping.set()
            raise OSError("cannot exec")

        ollama.start.side_effect = start
        waits = [False, False, True]
        with mock.patch.object(supervisor, "_preload"), \
                mock.patch.object(supervisor.stopping, "wait", side_effect=lambda _t: waits.pop(0) if waits else True):
            supervisor.run()
        self.assertGreaterEqual(len(calls), 1)


class VerifyModelTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.addCleanup(self.fake.close)
        self.cfg = replace(config.load({}), ollama_port=self.fake.port)

    def test_matching_digest(self):
        runtime.verify_model(self.cfg)

    def test_wrong_digest(self):
        self.fake.state.digest = "deadbeef"
        with self.assertRaisesRegex(runtime.RuntimeError_, "digest"):
            runtime.verify_model(self.cfg)

    def test_missing_model(self):
        self.fake.state.models = [m for m in self.fake.state.models
                                  if m["name"] != "qwen2.5vl:3b-q4_K_M"]
        cfg = replace(self.cfg, model="qwen2.5vl:3b-q4_K_M")
        with self.assertRaisesRegex(runtime.RuntimeError_, "not installed"):
            runtime.verify_model(cfg)


if __name__ == "__main__":
    unittest.main()


# --- security hardening (SECURITY.md) ---

class BindTests(unittest.TestCase):
    def test_is_loopback(self):
        for host in ("127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost"):
            self.assertTrue(gateway.is_loopback(host), host)
        for host in ("0.0.0.0", "::", "", "192.168.125.71", "example.com"):
            self.assertFalse(gateway.is_loopback(host), host)

    def test_non_loopback_bind_needs_a_token(self):
        self.assertIsNone(gateway.bind_error("127.0.0.1", ""))
        self.assertIsNone(gateway.bind_error("0.0.0.0", "tok"))
        for host in ("0.0.0.0", "::", "192.168.125.71"):
            message = gateway.bind_error(host, "  ")
            self.assertIn("MTAI_TOKEN", message)
            self.assertIn("EnvironmentVariables", message)

    def test_serve_lan_without_token_refuses_before_starting_ollama(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = replace(config.load({"MTAI_RUNTIME_DIR": tmp}), token="")
            with mock.patch.object(runtime, "OllamaServer") as server:
                with self.assertRaisesRegex(runtime.RuntimeError_, "MTAI_TOKEN"):
                    gateway.serve(cfg, lan=True)
            server.assert_not_called()


class RequestHardeningTests(GatewayCase):
    token = "s3cret-token-abcd1234"

    def auth(self, extra=None):
        return {"Authorization": f"Bearer {self.token}", **(extra or {})}

    def raw(self, method, path, body=b"", headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        conn.putrequest(method, path)
        for key, value in self.auth(headers).items():
            conn.putheader(key, value)
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")

    def test_bad_json_is_400_everywhere(self):
        for body in (b"{oops", b"\xff\xfe", b"[" * 100000):
            for path in ("/v1/chat/completions", "/config"):
                status, reply = self.call(path, raw=body, headers=self.auth())
                self.assertEqual((status, reply["error"]["code"]), (400, "invalid_request"), (path, body[:5]))
            status, reply = self.call("/jobs", raw=body, headers=self.auth())
            self.assertEqual((status, reply["code"]), (400, "bad_request"))
        self.assertEqual(self.fake.state.chats, [])

    def test_missing_content_length_is_411(self):
        status, reply = self.raw("POST", "/v1/chat/completions")
        self.assertEqual((status, reply["error"]["code"]), (411, "invalid_request"))

    def test_declared_oversize_body_is_413_without_reading_it(self):
        status, reply = self.raw("POST", "/v1/chat/completions",
                                 headers={"Content-Length": str(gateway.MAX_BODY_BYTES + 1)})
        self.assertEqual((status, reply["error"]["code"]), (413, "body_too_large"))

    def test_too_many_images_is_413(self):
        with mock.patch.object(gateway, "MAX_IMAGES", 1):
            status, reply = self.call("/v1/chat/completions", self.chat(SMALL, SMALL), self.auth())
        self.assertEqual((status, reply["error"]["code"]), (413, "body_too_large"))
        self.assertEqual(self.fake.state.chats, [])

    def test_oversize_image_is_413(self):
        with mock.patch.object(gateway, "MAX_IMAGE_BYTES", 16):
            status, reply = self.call("/v1/chat/completions", self.chat(SMALL), self.auth())
        self.assertEqual((status, reply["error"]["code"]), (413, "body_too_large"))
        self.assertEqual(self.resized, [])

    def test_job_failure_is_a_contract_500_without_details(self):
        with mock.patch.object(gateway.Gateway, "start_job", side_effect=OSError("/secret/path")):
            status, reply = self.call("/jobs", {"action": "set-model"}, self.auth())
        self.assertEqual((status, reply), (500, {"error": "internal error: OSError", "code": "internal_error"}))

    def test_health_carries_no_secret(self):
        gw_secrets = [self.token, "sk-remote-key-9876", "gho_oauth_token_5555"]
        config_dir = self.runtime_dir
        (config_dir / "remote.json").write_text(json.dumps({"api_key": gw_secrets[1]}))
        (config_dir / "copilot.json").write_text(json.dumps({"oauth_token": gw_secrets[2], "user": "me"}))
        req = urllib.request.Request(self.url + "/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode()
        for secret in gw_secrets:
            self.assertNotIn(secret, text)
        self.assertNotIn("token", json.loads(text))

    def test_get_config_shows_last4_only(self):
        status, body = self.call("/config", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(body["token"], {"configured": True, "last4": "1234"})
        self.assertNotIn(self.token, json.dumps(body))


class ShortTokenConfigTests(GatewayCase):
    token = "abc123"

    def test_short_token_has_no_last4(self):
        status, body = self.call("/config", headers={"Authorization": "Bearer abc123"})
        self.assertEqual((status, body["token"]), (200, {"configured": True, "last4": None}))
