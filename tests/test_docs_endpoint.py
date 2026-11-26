"""GET /docs (README.md rendered as HTML) and the "docs" field of /health."""
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from monkeytester_ai import config, gateway
from tests.test_gateway import GatewayCase

CSP = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


class DocsCase(GatewayCase):
    def get(self, path, headers=None):
        req = urllib.request.Request(self.url + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()


class DocsTests(DocsCase):
    def test_readme_path_is_repo_root(self):
        self.assertEqual(gateway.README_PATH, Path(__file__).resolve().parent.parent / "README.md")

    def test_docs_page(self):
        status, headers, body = self.get("/docs")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["Content-Security-Policy"], CSP)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        html = body.decode("utf-8")
        self.assertIn('<h1 id="monkeytester-plugin-ai">MonkeyTester-Plugin-AI</h1>', html)
        self.assertIn(config.SERVICE_VERSION, html)

    def test_health_advertises_docs(self):
        _, _, body = self.get("/health")
        self.assertEqual(json.loads(body)["docs"], "/docs")

    def test_missing_readme(self):
        missing = Path(self.runtime_dir) / "no-such-README.md"
        with mock.patch.object(gateway, "README_PATH", missing):
            _, _, body = self.get("/health")
            self.assertNotIn("docs", json.loads(body))
            status, headers, body = self.get("/docs")
        self.assertEqual(status, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")


class DocsAuthTests(DocsCase):
    token = "s3cret-docs"

    def test_token_required(self):
        for headers in ({}, {"X-Plugin-Token": "wrong"}, {"Authorization": "Bearer wrong"}):
            status, _, body = self.get("/docs", headers)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["error"]["code"], "unauthorized")

    def test_correct_token(self):
        for headers in ({"X-Plugin-Token": self.token}, {"Authorization": f"Bearer {self.token}"}):
            self.assertEqual(self.get("/docs", headers)[0], 200)

    def test_health_stays_open_and_advertises_docs(self):
        status, _, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["docs"], "/docs")
