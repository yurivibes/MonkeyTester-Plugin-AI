"""Pinned versions and environment-driven settings."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

API_VERSION = 3
SERVICE_NAME = "monkeytester-ai"
SERVICE_VERSION = "1.0.0"

OLLAMA_VERSION = "v0.40.13"
OLLAMA_URL = (
    f"https://github.com/ollama/ollama/releases/download/{OLLAMA_VERSION}/ollama-darwin.tgz"
)
OLLAMA_SHA256 = "bae29252232b9e1358cf5801a7d65a07442af18fb5467cb68c7fc3711f59c511"

DEFAULT_MODEL = "qwen2.5vl:7b-q4_K_M"

GIB = 1024 ** 3


@dataclass(frozen=True)
class ModelInfo:
    digest: str  # leading characters of the digest Ollama reports
    size_bytes: int
    min_memory_bytes: int


MODELS = {
    "qwen2.5vl:7b-q4_K_M": ModelInfo("5ced39dfa4ba", 6 * GIB, 16 * GIB),
    "qwen2.5vl:3b-q4_K_M": ModelInfo("fb90415cde1e", int(3.2 * GIB), 8 * GIB),
}

REPO_ROOT = Path(__file__).resolve().parent.parent

# The service's own secrets: never handed to a child process (Ollama, sips, curl).
SECRET_ENV = ("VTAI_TOKEN",)


def child_env(base=None) -> dict:
    """`base` (default os.environ) without SECRET_ENV."""
    base = os.environ if base is None else base
    return {k: v for k, v in base.items() if k not in SECRET_ENV}


@dataclass(frozen=True)
class Config:
    runtime_dir: Path
    model: str
    gateway_port: int
    ollama_port: int
    token: str
    max_pixels: int
    copilot_config: Path  # the GitHub Copilot sign-in, see copilot.py
    remote_config: Path   # the remote server's API key, see remote.py

    @property
    def settings_path(self) -> Path:
        """The persisted provider and model choice, see Gateway."""
        return self.runtime_dir / "settings.json"

    @property
    def ollama_dir(self) -> Path:
        return self.runtime_dir / "ollama"

    @property
    def models_dir(self) -> Path:
        return self.runtime_dir / "models"

    @property
    def home_dir(self) -> Path:
        return self.runtime_dir / "home"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def ollama_url(self) -> str:
        return f"http://127.0.0.1:{self.ollama_port}"

    @property
    def gateway_url(self) -> str:
        return f"http://127.0.0.1:{self.gateway_port}"


def load(env=None) -> Config:
    env = os.environ if env is None else env
    runtime_dir = Path(env.get("VTAI_RUNTIME_DIR") or REPO_ROOT / ".runtime").resolve()
    copilot_config = env.get("VTAI_COPILOT_CONFIG")
    remote_config = env.get("VTAI_REMOTE_CONFIG")
    return Config(
        runtime_dir=runtime_dir,
        model=env.get("VTAI_MODEL") or DEFAULT_MODEL,
        gateway_port=int(env.get("VTAI_PORT") or 8091),
        ollama_port=int(env.get("VTAI_OLLAMA_PORT") or 11435),
        token=(env.get("VTAI_TOKEN") or "").strip(),
        max_pixels=int(env.get("VTAI_MAX_PIXELS") or 1_000_000),
        copilot_config=(Path(copilot_config).expanduser().resolve() if copilot_config
                        else runtime_dir / "copilot.json"),
        remote_config=(Path(remote_config).expanduser().resolve() if remote_config
                       else runtime_dir / "remote.json"),
    )
