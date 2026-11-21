"""Command line interface: setup, serve, status, ask, service, clean."""
from __future__ import annotations

import argparse
import base64
import json
import os
import plistlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import config as cfgmod
from . import copilot, gateway, imaging, runtime

SERVICE_LABEL = runtime.SERVICE_LABEL
ENTRY_POINT = cfgmod.REPO_ROOT / "bin" / "monkeytester-ai"


def cmd_setup(cfg, _args):
    runtime.check_platform()
    runtime.check_memory(cfg.model, runtime.memory_bytes())
    info = cfgmod.MODELS.get(cfg.model)
    if info is None:
        print(f"Warning: {cfg.model} has no pinned digest, so it won't be verified.")
    else:
        runtime.check_disk(cfg, info.size_bytes + 2 * cfgmod.GIB)
    if runtime.install_ollama(cfg):
        print(f"Installed Ollama {cfgmod.OLLAMA_VERSION}")
    else:
        print(f"Ollama {cfgmod.OLLAMA_VERSION} already installed")
    server = runtime.OllamaServer(cfg)
    server.start()
    try:
        runtime.pull_model(cfg)
        runtime.verify_model(cfg)
    finally:
        server.stop()
    print(f"Ready: {cfg.model}. Start it with: bin/monkeytester-ai serve")
    return 0


def cmd_serve(cfg, args):
    gateway.serve(cfg, lan=args.lan)
    return 0


def _gateway_health(cfg):
    try:
        with urllib.request.urlopen(cfg.gateway_url + "/health", timeout=5) as resp:  # nosec B310 - cfg.gateway_url is http://127.0.0.1:<port>
            return json.loads(resp.read())
    except (OSError, ValueError):
        return None


def _gateway_models(cfg):
    """GET /models from a running gateway: `{object, data, selected, installed}`."""
    try:
        headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
        req = urllib.request.Request(cfg.gateway_url + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 - cfg.gateway_url is http://127.0.0.1:<port>
            return json.loads(resp.read())
    except (OSError, ValueError):
        return None


def status_report(cfg) -> dict:
    """What `status --json` prints, for programs such as the MonkeyTester-Mac launcher."""
    health = _gateway_health(cfg)
    ollama_version = runtime.installed_version(cfg)
    model_installed = runtime.model_installed(cfg)
    models = _gateway_models(cfg) if health is not None else None
    stored = copilot.read_json(cfg.settings_path)
    signin = copilot.load_signin(cfg.copilot_config)
    # `selected` stays the LOCAL Ollama model whichever provider is active.
    local_default = stored.get("model") if isinstance(stored.get("model"), str) else ""
    local_default = local_default or cfg.model
    if models and models.get("provider", "local") == "local" and models.get("selected"):
        selected = models["selected"]
    elif health is not None and health.get("provider", "local") == "local":
        selected = health.get("model", local_default)
    else:
        selected = local_default
    remote = {key: stored.get(f"remote_{key}") if isinstance(stored.get(f"remote_{key}"), str)
              else "" for key in ("url", "model")}
    if health is not None and health.get("provider") in gateway.PROVIDERS:
        provider = health["provider"]
    elif stored.get("provider") == "copilot" and signin["oauth_token"]:
        provider = "copilot"
    elif stored.get("provider") == "remote" and remote["url"] and remote["model"]:
        provider = "remote"
    else:
        provider = "local"
    return {
        "service": cfgmod.SERVICE_NAME,
        "api_version": cfgmod.API_VERSION,
        "ready_to_serve": bool(ollama_version) and model_installed,
        "ollama_version": ollama_version or None,
        "model": cfg.model,
        "model_installed": model_installed,
        "gateway_running": health is not None,
        "model_loaded": bool(health and health.get("model_loaded")),
        "selected": selected,
        "installed": list((models or {}).get("installed") or []),
        "provider": provider,
        "copilot": {"configured": bool(signin["oauth_token"]), "user": signin["user"]},
        "remote": remote,
    }


def cmd_status(cfg, args):
    if args.json:
        report = status_report(cfg)
        print(json.dumps(report))
        return 0 if report["ready_to_serve"] else 1
    print(f"Runtime:  {cfg.runtime_dir}")
    print(f"Ollama:   {runtime.installed_version(cfg) or 'not installed'}")
    print(f"Model:    {cfg.model}")
    signin = copilot.load_signin(cfg.copilot_config)
    print(f"Copilot:  {'signed in as ' + (signin['user'] or '?') if signin['oauth_token'] else 'not signed in'}")
    stored = copilot.read_json(cfg.settings_path)
    if stored.get("remote_url"):
        print(f"Remote:   {stored['remote_url']}, model {stored.get('remote_model') or 'not chosen'}")
    if sys.platform == "darwin":
        print(f"Memory:   {runtime.memory_bytes() // cfgmod.GIB}GB")
    print(f"Service:  {'installed' if _plist_path().exists() else 'not installed'}")
    health = _gateway_health(cfg)
    if health is None:
        print(f"Gateway:  not running on port {cfg.gateway_port}")
        return 1
    print(f"Gateway:  running on port {cfg.gateway_port}, provider "
          f"{health.get('provider', 'local')}, model {health['model']}, backend "
          f"{health['ollama']}, model {'loaded' if health['model_loaded'] else 'not loaded'}")
    return 0 if health["ok"] else 1


def cmd_ask(cfg, args):
    data = Path(args.image).read_bytes()
    fmt, _, _ = imaging.image_size(data)
    payload = {
        "model": cfg.model,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/{fmt};base64,{base64.b64encode(data).decode()}"}},
            {"type": "text", "text": args.prompt},
        ]}],
    }
    headers = {"Content-Type": "application/json"}
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"
    req = urllib.request.Request(cfg.gateway_url + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=gateway.UPSTREAM_TIMEOUT_SECS + 10) as resp:  # nosec B310 - cfg.gateway_url is http://127.0.0.1:<port>
            reply = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise runtime.RuntimeError_(f"Gateway returned {exc.code}: {exc.read().decode(errors='replace')}")
    except OSError as exc:
        raise runtime.RuntimeError_(
            f"Could not reach the gateway ({exc}). Is bin/monkeytester-ai serve running?"
        )
    print(reply["choices"][0]["message"]["content"])
    return 0


def _plist_path() -> Path:
    return runtime.service_plist_path()


def build_plist(cfg, lan: bool) -> dict:
    arguments = [sys.executable, str(ENTRY_POINT), "serve"] + (["--lan"] if lan else [])
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "VTAI_RUNTIME_DIR": str(cfg.runtime_dir),
        "VTAI_MODEL": cfg.model,
        "VTAI_PORT": str(cfg.gateway_port),
        "VTAI_OLLAMA_PORT": str(cfg.ollama_port),
        "VTAI_MAX_PIXELS": str(cfg.max_pixels),
    }
    if cfg.token:
        env["VTAI_TOKEN"] = cfg.token
    log = str(cfg.logs_dir / "service.log")
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": arguments,
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def service_uninstall():
    path = _plist_path()
    if not path.exists():
        return False
    _launchctl("bootout", f"gui/{os.getuid()}/{SERVICE_LABEL}")
    path.unlink()
    return True


def cmd_service(cfg, args):
    runtime.check_platform()
    if args.action == "uninstall":
        print("Service removed" if service_uninstall() else "Service was not installed")
        return 0
    problem = gateway.bind_error("0.0.0.0" if args.lan else "127.0.0.1", cfg.token)  # nosec B104 - check only
    if problem:
        raise runtime.RuntimeError_(problem)
    service_uninstall()
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    # Created private because the plist can hold the token.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        plistlib.dump(build_plist(cfg, args.lan), handle)
    result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if result.returncode != 0:
        path.unlink()
        raise runtime.RuntimeError_(f"launchctl failed: {result.stderr.strip()}")
    print(f"Service installed and started; logs in {cfg.logs_dir}")
    return 0


def cmd_clean(cfg, args):
    if _gateway_health(cfg) is not None and not _plist_path().exists():
        raise runtime.RuntimeError_("The gateway is running. Stop it first.")
    if not args.yes:
        answer = input(f"Delete {cfg.runtime_dir} and any LaunchAgent? [y/N] ")
        if answer.strip().lower() != "y":
            print("Cancelled")
            return 1
    service_uninstall()
    if cfg.runtime_dir.exists():
        shutil.rmtree(cfg.runtime_dir)
    print("Removed")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="monkeytester-ai", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="install Ollama and the model into .runtime/").set_defaults(func=cmd_setup)
    serve = sub.add_parser("serve", help="run the service in the foreground")
    serve.add_argument("--lan", action="store_true", help="listen on all interfaces; needs VTAI_TOKEN")
    serve.set_defaults(func=cmd_serve)
    status = sub.add_parser("status", help="show what is installed and running")
    status.add_argument("--json", action="store_true", help="print one JSON object; exit 1 until set up")
    status.set_defaults(func=cmd_status)
    ask = sub.add_parser("ask", help="ask the running service about an image")
    ask.add_argument("image")
    ask.add_argument("prompt")
    ask.set_defaults(func=cmd_ask)
    service = sub.add_parser("service", help="manage starting at login")
    service.add_argument("action", choices=["install", "uninstall"])
    service.add_argument("--lan", action="store_true", help="listen on all interfaces; needs VTAI_TOKEN")
    service.set_defaults(func=cmd_service)
    clean = sub.add_parser("clean", help="remove .runtime/ and the LaunchAgent")
    clean.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    clean.set_defaults(func=cmd_clean)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(cfgmod.load(), args)
    except (runtime.RuntimeError_, imaging.ImageError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
