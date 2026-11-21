# MonkeyTester-Plugin-AI — Agent Guide

## Purpose
The vision-model service for [MonkeyTester](https://github.com/yurivibes/MonkeyTester),
registered there as the `monkeytester-ai` device plugin. MonkeyTester's runner sends every
vision request (an OpenAI-style chat completion with a screenshot) to this service's
`/v1/chat/completions`, and this service decides who answers: a private Ollama running
Qwen2.5-VL (`local`), an OpenAI-compatible server at a user-entered address (`remote`), or
GitHub Copilot (`copilot`). It also owns the model choice, the remote API key and the GitHub
sign-in; MonkeyTester holds none of them. MonkeyTester-Mac bundles this code and runs `serve`
itself. See README.md (usage), ARCHITECTURE.md (design) and API.md (the HTTP contract).

## Setup (do this first)
Install the dependencies with the steps in README.md → **Install** before running or testing
anything; that section is the single source for them. In short:
- The code is Python standard library only (Python 3.9+, the version that ships with macOS).
  There is no `requirements.txt` and no `pip install` step.
- **Nix available** (`command -v nix`): `nix run .#tests` runs the tests, `nix run . -- <command>`
  runs the CLI, `nix develop` gives a shell with Python. Run them from the repo root.
- **No Nix:** a `python3` on `PATH` is all the tests need, on Linux or macOS.
- `setup` and `serve` only work on macOS on Apple silicon: `setup` downloads a pinned Ollama
  and the model (~6 GB) into `.runtime/`. Don't run `setup` just to test a code change; the
  tests use fakes. Don't install Ollama through Homebrew or Nix for this project.
- Verify with README.md → Install → Verify. If a step there doesn't work as written, fix
  README.md in the same PR.

## Critical Rules
- **Work in a git worktree on a feature branch and open a PR; never commit to master.**
- **Standard library only.** No third-party Python packages: the code must keep running on the
  stock macOS Python 3.9 and inside MonkeyTester-Mac's bundled Python. Image work uses `sips`
  (built into macOS). A new system dependency goes into README.md → Install and `flake.nix`
  in the same PR.
- **API.md is the contract.** MonkeyTester (`visual-tests/scripts/plugin_client.py`,
  `vision_analyzer.py`) and MonkeyTester-Mac depend on it. Change API.md first for any
  route, field or error change; bump `api_version` for a breaking change and add a migration
  note. The plugin side also follows `PLUGIN_API.md` in the MonkeyTester repo
  (`contract_version: 1`).
- **Everything lives in `.runtime/`** (or `VTAI_RUNTIME_DIR`). Nothing is written outside it
  except the optional LaunchAgent plist. Don't touch the user's own `~/.ollama` or Ollama port.
- **Secrets are write-only.** `copilot.json` and `remote.json` are mode 600, written atomically,
  and no route returns them; status shows only `{configured, last4}`/user. Keep it that way.
- **Pins:** the Ollama version/SHA-256 and the model digests are pinned in
  `monkeytester_ai/config.py`. Change them only deliberately, and say so in the PR.
- **Tests never touch the network or real hardware**: they run against `tests/fake_ollama.py`,
  `tests/fake_github.py` and `tests/fake_remote.py`. New behaviour gets a test against those.
- **User-relevant changes** (commands, ports, `VTAI_*` variables, setup steps, provider
  behaviour): update README.md in the same PR and add a "For the user" section to the PR
  description; otherwise write "For the user: no change".

## Layout
| Path | What |
|---|---|
| `bin/monkeytester-ai` | CLI entry point (`setup`, `serve`, `status`, `ask`, `service`, `clean`) |
| `monkeytester_ai/config.py` | Pins and `VTAI_*` settings |
| `monkeytester_ai/runtime.py` | Ollama download, install checks, process supervision |
| `monkeytester_ai/gateway.py` | HTTP server, auth, provider switch, jobs, `settings.json` |
| `monkeytester_ai/imaging.py` | Resize (`sips`) and coordinate rescaling |
| `monkeytester_ai/remote.py` | Remote provider: address, `/v1/models`, API key |
| `monkeytester_ai/copilot.py` | GitHub device-flow sign-in and Copilot forwarding |
| `monkeytester_ai/cli.py` | Commands and the LaunchAgent |
| `tests/` | Unit tests with fake Ollama, GitHub and remote servers |

## Ports and environment
Gateway `VTAI_PORT` (default 8091, `127.0.0.1` unless `serve --lan`, which needs `VTAI_TOKEN`);
private Ollama `VTAI_OLLAMA_PORT` (default 11435, localhost only). Every variable is in the
README's Settings table.

## Tests
`nix run .#tests`, or `python3 -m unittest discover tests` (the command CI runs,
`.gitea/workflows/test.yml`). They pass on Linux and macOS. They don't check the real model's
answers; the README's `ask` check does that on a Mac after `setup`.
