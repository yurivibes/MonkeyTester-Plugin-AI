"""Installing and running the private copy of Ollama under .runtime/."""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config as cfgmod

SERVICE_LABEL = "com.blackjackfruit.monkeytester-ai"


def service_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def service_status() -> str:
    """Whether the start-at-login service is installed. Works on any platform."""
    return "installed" if service_plist_path().is_file() else "not_installed"


class RuntimeError_(Exception):
    """A problem to show the user without a traceback."""


def check_platform():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError_(
            f"MonkeyTester-Plugin-AI needs macOS on Apple silicon (found {sys.platform} {platform.machine()})"
        )


def memory_bytes() -> int:
    out = subprocess.run(["sysctl", "-n", "hw.memsize"], check=True, capture_output=True, text=True)
    return int(out.stdout.strip())


def check_memory(model: str, total: int):
    info = cfgmod.MODELS.get(model)
    if info and total < info.min_memory_bytes:
        raise RuntimeError_(
            f"{model} needs {info.min_memory_bytes // cfgmod.GIB}GB of memory; this Mac has "
            f"{total // cfgmod.GIB}GB. Use the smaller model: MTAI_MODEL=qwen2.5vl:3b-q4_K_M"
        )


def check_disk(cfg, needed: int):
    probe = cfg.runtime_dir
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < needed:
        raise RuntimeError_(
            f"Need {needed // cfgmod.GIB}GB free disk, found {free // cfgmod.GIB}GB at {probe}"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract(archive: Path, dest: Path):
    """Extract a tarball, refusing entries that would land outside dest."""
    root = dest.resolve()

    def inside(path: Path) -> bool:
        return path == root or root in path.parents

    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        for member in members:
            target = (root / member.name).resolve()
            if not inside(target):
                raise RuntimeError_(f"Unsafe path in archive: {member.name}")
            if member.issym() and not inside((target.parent / member.linkname).resolve()):
                raise RuntimeError_(f"Unsafe link in archive: {member.name}")
            if member.islnk() and not inside((root / member.linkname).resolve()):
                raise RuntimeError_(f"Unsafe link in archive: {member.name}")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise RuntimeError_(f"Unsupported entry in archive: {member.name}")
        for member in members:
            tar.extract(member, root)


def find_binary(cfg) -> "Path | None":
    if not cfg.ollama_dir.is_dir():
        return None
    for path in sorted(cfg.ollama_dir.rglob("ollama"), key=lambda p: len(p.parts)):
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def installed_version(cfg) -> str:
    marker = cfg.ollama_dir / ".version"
    return marker.read_text().strip() if marker.is_file() and find_binary(cfg) else ""


def install_ollama(cfg, url=cfgmod.OLLAMA_URL, sha256=cfgmod.OLLAMA_SHA256,
                   version=cfgmod.OLLAMA_VERSION):
    """Download, verify and unpack Ollama. Nothing is replaced until all checks pass."""
    if installed_version(cfg) == version:
        return False
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cfg.runtime_dir, prefix="install-") as tmp:
        archive = Path(tmp) / "ollama.tgz"
        try:
            subprocess.run(["curl", "-fL", "--retry", "3", "-o", str(archive), "--", url],
                           check=True, env=cfgmod.child_env())
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError_(f"Could not download Ollama: {exc}") from exc
        actual = sha256_file(archive)
        if actual != sha256:
            raise RuntimeError_(f"Ollama checksum mismatch: expected {sha256}, got {actual}")
        staged = Path(tmp) / "ollama"
        staged.mkdir()
        safe_extract(archive, staged)
        (staged / ".version").write_text(version + "\n")
        if cfg.ollama_dir.exists():
            shutil.rmtree(cfg.ollama_dir)
        shutil.move(str(staged), str(cfg.ollama_dir))
    if not find_binary(cfg):
        raise RuntimeError_("The Ollama archive did not contain an ollama binary")
    return True


def ollama_env(cfg) -> dict:
    """Environment that keeps Ollama's keys, models and port inside this project."""
    # No OLLAMA_* from the caller (OLLAMA_HOST must stay loopback) and no MTAI_TOKEN.
    env = {k: v for k, v in cfgmod.child_env().items() if not k.startswith("OLLAMA_")}
    cfg.home_dir.mkdir(parents=True, exist_ok=True)
    cfg.models_dir.mkdir(parents=True, exist_ok=True)
    env.update(
        HOME=str(cfg.home_dir),
        OLLAMA_HOST=f"127.0.0.1:{cfg.ollama_port}",  # loopback only: Ollama has no auth
        OLLAMA_MODELS=str(cfg.models_dir),
        OLLAMA_NUM_PARALLEL="1",
        OLLAMA_CONTEXT_LENGTH="4096",
        OLLAMA_KEEP_ALIVE="30m",
    )
    return env


def api(cfg, path, payload=None, timeout=5.0):
    """Call the private Ollama API and return decoded JSON."""
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(cfg.ollama_url + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - cfg.ollama_url is http://127.0.0.1:<port>
        return json.loads(resp.read())


def port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def command_of(pid: int) -> str:
    """A running process's command line; empty once it has exited."""
    out = subprocess.run(["ps", "-o", "stat=,command=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return "" if not out or out.startswith("Z") else out


class OllamaServer:
    """The private `ollama serve` process."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None

    def start(self, timeout=30.0):
        binary = find_binary(self.cfg)
        if not binary:
            raise RuntimeError_("Ollama is not installed. Run: bin/monkeytester-ai setup")
        self._stop_leftover()
        if port_in_use(self.cfg.ollama_port):
            raise RuntimeError_(
                f"Port {self.cfg.ollama_port} is already in use. Stop that process or set MTAI_OLLAMA_PORT."
            )
        self.cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.logs_dir / "ollama.log", "ab") as log:
            self.proc = subprocess.Popen(
                [str(binary), "serve"], env=ollama_env(self.cfg),
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
        self._pid_file.write_text(str(self.proc.pid))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError_(
                    f"Ollama exited with code {self.proc.returncode}; see {self.cfg.logs_dir / 'ollama.log'}"
                )
            try:
                api(self.cfg, "/api/version", timeout=1.0)
                return
            except (OSError, ValueError):
                time.sleep(0.2)
        self.stop()
        raise RuntimeError_("Ollama did not become ready in time")

    @property
    def _pid_file(self) -> Path:
        return self.cfg.runtime_dir / "ollama.pid"

    def _stop_leftover(self):
        """Stop an Ollama left behind by a gateway that was killed.

        A killed gateway can't stop its child, which would keep the port and the model's
        memory, and block every restart. Only the process recorded here is touched, and
        only if it still looks like our Ollama, in case the pid has been reused.
        """
        try:
            pid = int(self._pid_file.read_text())
        except (OSError, ValueError):
            return
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True).stdout
        if "ollama" in command and "serve" in command:
            try:
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and command_of(pid):
                    time.sleep(0.1)
                if command_of(pid):
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.2)
            except ProcessLookupError:
                pass
        self._pid_file.unlink(missing_ok=True)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        self._pid_file.unlink(missing_ok=True)


def model_installed(cfg) -> bool:
    """Whether the model's manifest is on disk. Works with Ollama stopped."""
    name, _, tag = cfg.model.partition(":")
    manifest = cfg.models_dir / "manifests" / "registry.ollama.ai" / "library" / name / (tag or "latest")
    return manifest.is_file()


def list_models(cfg) -> list:
    """Names of the models installed in the private Ollama copy.

    Raises OSError/ValueError when Ollama is unreachable."""
    models = api(cfg, "/api/tags", timeout=5.0).get("models", [])
    names = set()
    for entry in models:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        if name:
            names.add(str(name))
    return sorted(names)


def persist_service_token(new_token: str) -> bool:
    """Store a rotated token where the current one is persisted.

    The only persistence for a token is the LaunchAgent plist written by `service install`
    (which holds it in mode 600). When the token came only from MTAI_TOKEN in a shell,
    there is no file to write back to and this returns False; the gateway keeps the new
    value for its own lifetime either way."""
    path = service_plist_path()
    if not path.is_file():
        return False
    with open(path, "rb") as handle:
        plist = plistlib.load(handle)
    env = plist.get("EnvironmentVariables")
    if not isinstance(env, dict) or "MTAI_TOKEN" not in env:
        return False
    env["MTAI_TOKEN"] = new_token
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        plistlib.dump(plist, handle)
    return True


def pull_model(cfg):
    binary = find_binary(cfg)
    result = subprocess.run([str(binary), "pull", cfg.model], env=ollama_env(cfg))
    if result.returncode != 0:
        raise RuntimeError_(f"Could not pull {cfg.model}")


def verify_model(cfg):
    """Check the model is present and, for pinned models, has the expected digest."""
    models = api(cfg, "/api/tags").get("models", [])
    match = next((m for m in models if m.get("name") == cfg.model), None)
    if match is None:
        raise RuntimeError_(f"{cfg.model} is not installed. Run: bin/monkeytester-ai setup")
    info = cfgmod.MODELS.get(cfg.model)
    digest = match.get("digest", "")
    if info and not digest.startswith(info.digest):
        raise RuntimeError_(
            f"{cfg.model} has digest {digest[:12]}, expected {info.digest}. If Ollama republished "
            "this tag and you trust it, update the pin in monkeytester_ai/config.py."
        )


def preload(cfg):
    """Load the model into memory; an empty generate request does only that."""
    api(cfg, "/api/generate", {"model": cfg.model, "keep_alive": "30m"}, timeout=300.0)


def model_loaded(cfg) -> bool:
    models = api(cfg, "/api/ps", timeout=2.0).get("models", [])
    return any(m.get("name") == cfg.model for m in models)
