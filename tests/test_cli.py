import hashlib
import io
import json
import os
import plistlib
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from monkeytester_ai import cli, config, runtime

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "monkeytester-ai"
FAKE = Path(__file__).parent / "fake_ollama.py"
FIXTURE = Path(__file__).parent / "fixtures" / "target.png"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TempCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = {
            "VTAI_RUNTIME_DIR": str(self.tmp / "runtime"),
            "VTAI_PORT": str(free_port()),
            "VTAI_OLLAMA_PORT": str(free_port()),
        }
        self.cfg = config.load(self.env)


class StatusJsonTests(TempCase):
    def run_status(self):
        out = subprocess.run([sys.executable, str(BIN), "status", "--json"], capture_output=True, text=True,
                             env={**os.environ, **self.env})
        return out.returncode, json.loads(out.stdout)

    def test_not_set_up(self):
        code, report = self.run_status()
        self.assertEqual(code, 1)
        self.assertFalse(report["ready_to_serve"])
        self.assertFalse(report["gateway_running"])
        self.assertEqual(report["service"], "monkeytester-ai")

    def test_set_up_without_starting_ollama(self):
        ollama = self.cfg.ollama_dir
        ollama.mkdir(parents=True)
        (ollama / ".version").write_text(config.OLLAMA_VERSION + "\n")
        binary = ollama / "ollama"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        name, tag = self.cfg.model.split(":")
        manifest = self.cfg.models_dir / "manifests" / "registry.ollama.ai" / "library" / name / tag
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}")
        code, report = self.run_status()
        self.assertEqual(code, 0)
        self.assertTrue(report["ready_to_serve"])


class InstallTests(TempCase):
    def make_archive(self, members):
        path = self.tmp / "ollama.tgz"
        with tarfile.open(path, "w:gz") as tar:
            for name, body, mode in members:
                info = tarfile.TarInfo(name)
                info.size = len(body)
                info.mode = mode
                tar.addfile(info, io.BytesIO(body))
        return path

    def install(self, archive, sha=None):
        sha = sha or hashlib.sha256(archive.read_bytes()).hexdigest()
        return runtime.install_ollama(self.cfg, url=archive.as_uri(), sha256=sha, version="vX")

    def test_installs_and_is_idempotent(self):
        archive = self.make_archive([("ollama", b"#!/bin/sh\n", 0o755), ("lib/a.so", b"x", 0o644)])
        self.assertTrue(self.install(archive))
        self.assertEqual(runtime.find_binary(self.cfg), self.cfg.ollama_dir / "ollama")
        self.assertEqual(runtime.installed_version(self.cfg), "vX")
        self.assertFalse(self.install(archive))

    def test_checksum_failure_installs_nothing(self):
        archive = self.make_archive([("ollama", b"x", 0o755)])
        with self.assertRaisesRegex(runtime.RuntimeError_, "checksum"):
            self.install(archive, sha="0" * 64)
        self.assertFalse(self.cfg.ollama_dir.exists())

    def test_path_traversal_rejected(self):
        archive = self.make_archive([("../../evil", b"x", 0o755)])
        with self.assertRaisesRegex(runtime.RuntimeError_, "Unsafe"):
            self.install(archive)
        self.assertFalse((self.tmp / "evil").exists())

    def test_escaping_symlink_rejected(self):
        path = self.tmp / "ollama.tgz"
        with tarfile.open(path, "w:gz") as tar:
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        with self.assertRaisesRegex(runtime.RuntimeError_, "Unsafe"):
            self.install(path)


class ChecksTests(TempCase):
    def test_memory_gate(self):
        with self.assertRaisesRegex(runtime.RuntimeError_, "3b-q4_K_M"):
            runtime.check_memory("qwen2.5vl:7b-q4_K_M", 8 * config.GIB)
        runtime.check_memory("qwen2.5vl:7b-q4_K_M", 16 * config.GIB)
        runtime.check_memory("qwen2.5vl:3b-q4_K_M", 8 * config.GIB)

    def test_platform_gate(self):
        with mock.patch.object(sys, "platform", "linux"):
            with self.assertRaisesRegex(runtime.RuntimeError_, "macOS"):
                runtime.check_platform()

    def test_child_environment_stays_in_runtime(self):
        with mock.patch.dict(os.environ, {"OLLAMA_DEBUG": "1", "OLLAMA_MODELS": "/elsewhere"}):
            env = runtime.ollama_env(self.cfg)
        self.assertEqual(env["HOME"], str(self.cfg.home_dir))
        self.assertEqual(env["OLLAMA_MODELS"], str(self.cfg.models_dir))
        self.assertEqual(env["OLLAMA_HOST"], f"127.0.0.1:{self.cfg.ollama_port}")
        self.assertNotIn("OLLAMA_DEBUG", env)

    def test_child_environment_never_gets_the_service_token(self):
        with mock.patch.dict(os.environ, {"VTAI_TOKEN": "s3cret-gateway-token", "OLLAMA_HOST": "0.0.0.0:1"}):
            env = runtime.ollama_env(self.cfg)
            self.assertNotIn("VTAI_TOKEN", env)
            self.assertEqual(env["OLLAMA_HOST"], f"127.0.0.1:{self.cfg.ollama_port}")
            self.assertNotIn("VTAI_TOKEN", config.child_env())
            from monkeytester_ai import imaging
            with mock.patch.object(imaging.subprocess, "run", side_effect=OSError("no sips")) as run:
                with self.assertRaises(imaging.ImageError):
                    imaging.resize_with_sips(b"x", "png", 28, 28)
            self.assertNotIn("VTAI_TOKEN", run.call_args.kwargs["env"])
            self.assertEqual(run.call_args.args[0][:4], ["sips", "-z", "28", "28"])

    def test_service_install_lan_without_token_refuses(self):
        with mock.patch.dict(os.environ, {**self.env}, clear=False), \
                mock.patch.dict(os.environ, {"VTAI_TOKEN": ""}), \
                mock.patch.object(runtime, "check_platform"), \
                mock.patch.object(cli, "_launchctl") as launchctl, \
                mock.patch.object(cli, "_plist_path", return_value=self.tmp / "agent.plist"):
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli.main(["service", "install", "--lan"])
        self.assertEqual(code, 1)
        self.assertIn("VTAI_TOKEN", err.getvalue())
        launchctl.assert_not_called()
        self.assertFalse((self.tmp / "agent.plist").exists())


class PlistTests(TempCase):
    def test_contents(self):
        plist = cli.build_plist(self.cfg, lan=True)
        plistlib.dumps(plist)
        self.assertEqual(plist["ProgramArguments"][1:], [str(BIN), "serve", "--lan"])
        self.assertEqual(plist["EnvironmentVariables"]["VTAI_RUNTIME_DIR"], str(self.cfg.runtime_dir))
        self.assertNotIn("VTAI_TOKEN", plist["EnvironmentVariables"])


class EndToEndTests(TempCase):
    """Runs the real entry point with fake ollama and sips executables."""

    def setUp(self):
        super().setUp()
        ollama_dir = self.cfg.ollama_dir
        ollama_dir.mkdir(parents=True)
        binary = ollama_dir / "ollama"
        binary.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        binary.chmod(0o755)
        (ollama_dir / ".version").write_text(config.OLLAMA_VERSION + "\n")
        fake_bin = self.tmp / "fakebin"
        fake_bin.mkdir()
        sips = fake_bin / "sips"
        sips.write_text('#!/bin/sh\ncp "$4" "$6"\n')
        sips.chmod(0o755)
        self.env = {**os.environ, **self.env, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
        self.env.pop("VTAI_TOKEN", None)

    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(BIN), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=30)

    def start(self, *args, env=None):
        proc = subprocess.Popen([sys.executable, str(BIN), "serve", *args], env=env or self.env,
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, proc)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.env['VTAI_PORT']}/health", timeout=1)
                return proc
            except OSError:
                time.sleep(0.1)
        self.fail("gateway did not start")

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=15)
        proc.stderr.close()

    def test_serve_ask_status_and_clean_shutdown(self):
        proc = self.start()
        status = self.run_cli("status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("backend running", status.stdout)
        ask = self.run_cli("ask", str(FIXTURE), "where is the red box?")
        self.assertEqual(ask.returncode, 0, ask.stderr)
        self.assertTrue(json.loads(ask.stdout)["found"])
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(runtime.port_in_use(int(self.env["VTAI_OLLAMA_PORT"])), "Ollama left running")

    def test_restart_recovers_from_an_ollama_left_by_a_killed_gateway(self):
        proc = self.start()
        pid = int((self.cfg.runtime_dir / "ollama.pid").read_text())
        proc.kill()  # SIGKILL: the gateway can't clean up, so Ollama outlives it
        proc.wait(timeout=15)
        self.assertTrue(runtime.port_in_use(int(self.env["VTAI_OLLAMA_PORT"])), "expected a leftover Ollama")
        self.start()
        self.assertNotEqual(int((self.cfg.runtime_dir / "ollama.pid").read_text()), pid)
        self.assertEqual(runtime.command_of(pid), "")

    def test_unrelated_process_with_a_reused_pid_is_left_alone(self):
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.runtime_dir / "ollama.pid").write_text(str(other.pid))
        runtime.OllamaServer(self.cfg)._stop_leftover()
        self.assertIsNone(other.poll())

    def test_lan_requires_token(self):
        result = self.run_cli("serve", "--lan")
        self.assertEqual(result.returncode, 1)
        self.assertIn("VTAI_TOKEN", result.stderr)

    def test_lan_with_token_listens_and_enforces_it(self):
        env = {**self.env, "VTAI_TOKEN": "s3cret"}
        self.start("--lan", env=env)
        self.assertEqual(self.run_cli("ask", str(FIXTURE), "q", env=env).returncode, 0)
        denied = self.run_cli("ask", str(FIXTURE), "q")
        self.assertEqual(denied.returncode, 1)
        self.assertIn("401", denied.stderr)

    def test_ollama_is_restarted_if_it_dies(self):
        proc = self.start()
        subprocess.run(["pkill", "-f", f"{FAKE} serve"], check=False)
        deadline = time.monotonic() + 20
        time.sleep(1)
        while time.monotonic() < deadline:
            if runtime.port_in_use(int(self.env["VTAI_OLLAMA_PORT"])):
                return
            time.sleep(0.2)
        self.fail("Ollama was not restarted")

    def test_serve_without_install_explains(self):
        shutil.rmtree(self.cfg.ollama_dir)
        result = self.run_cli("serve")
        self.assertEqual(result.returncode, 1)
        self.assertIn("setup", result.stderr)

    def test_clean_removes_runtime(self):
        self.assertEqual(self.run_cli("clean", "--yes").returncode, 0)
        self.assertFalse(self.cfg.runtime_dir.exists())

    def test_ask_rejects_non_image(self):
        bad = self.tmp / "bad.txt"
        bad.write_text("hello")
        result = self.run_cli("ask", str(bad), "q")
        self.assertEqual(result.returncode, 1)
        self.assertIn("PNG or JPEG", result.stderr)


if __name__ == "__main__":
    unittest.main()
