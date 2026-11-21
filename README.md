# MonkeyTester-Plugin-AI

Runs a vision model on your Mac so that [MonkeyTester](https://github.com/yurivibes/MonkeyTester)
can understand screenshots from your phones. It has three providers:

- **Local** (the default): Qwen2.5-VL through a private copy of Ollama. With Local selected,
  everything stays on your machine.
- **Remote server**: forwards each request to an OpenAI-compatible model server at an address
  you enter, for example llama.cpp's `llama-server` on a GPU machine. Screenshots go to that
  server. See [Remote server](#remote-server).
- **GitHub Copilot**: the service signs in to your GitHub account and forwards each request to
  Copilot's vision models (GPT-4o by default). With Copilot selected, **screenshots are sent to
  GitHub's Copilot API**. See [GitHub Copilot](#github-copilot).

Everything it installs goes in a `.runtime/` folder inside this project. It won't touch any
Ollama you already have, and deleting the folder removes it.

To learn how it works, see [ARCHITECTURE.md](ARCHITECTURE.md). For the HTTP contract, see [API.md](API.md).

## Install

The code uses only the Python standard library, so there is no `pip install` step.

| Dependency | Why | Where |
|---|---|---|
| Python 3.9 or newer | everything | macOS: comes with the Xcode command line tools (`xcode-select --install`); Linux: the OS package `python3` |
| macOS on Apple silicon | `setup` and `serve` (Ollama with Metal, `sips` for resizing) | Linux and Intel Macs can run the tests and `status`, not the service |
| 16GB of memory or more | the default model (8GB works with the smaller model, see [Settings](#settings)) | for `serve` |
| About 8GB of free disk space | Ollama and the model in `.runtime/` | for `setup` |

Ollama is **not** a dependency to install: `setup` downloads its own pinned copy into
`.runtime/` (see below). Don't install it with Homebrew or Nix for this project.

```bash
git clone https://github.com/yurivibes/MonkeyTester-Plugin-AI.git
cd MonkeyTester-Plugin-AI
```

### With Nix (macOS or Linux)

Needs Nix with flakes enabled (`experimental-features = nix-command flakes` in
`~/.config/nix/nix.conf`). Run these from the repo root, because `.runtime/` lives in the
checkout (or set `VTAI_RUNTIME_DIR`):

```bash
nix run .#tests            # the unit tests (any OS)
nix run . -- setup         # = bin/monkeytester-ai setup (Apple silicon)
nix run . -- serve         # = bin/monkeytester-ai serve
nix develop                # a shell with python3
```

### Without Nix

**macOS:** `python3 --version`; if it's missing, run `xcode-select --install`.

**Debian / Ubuntu (tests only):** `sudo apt-get install -y python3`

### Verify

```bash
python3 --version                       # 3.9 or newer
python3 -m unittest discover tests      # or: nix run .#tests
```

The tests use a fake Ollama, a fake GitHub and a fake remote server, so they download
nothing and work on Linux too. On a Mac, continue with Setup to install the model.

## Setup

From this folder:

```bash
bin/monkeytester-ai setup
```

This downloads Ollama and the model (about 6GB) and verifies both. You can safely rerun it.

Then start the service and leave it running:

```bash
bin/monkeytester-ai serve
```

In a second terminal, check that it can see:

```bash
bin/monkeytester-ai ask tests/fixtures/target.png \
  'Find the red rectangle. Return ONLY JSON: {"found": true/false, "coordinates": [x, y], "bbox": [x1, y1, x2, y2]}'
```

`coordinates` should be close to `[585, 1280]`, the center of the rectangle in that image.

## Connect MonkeyTester

How you start the service depends on where MonkeyTester runs.

The **model** is not one of MonkeyTester's settings anymore: MonkeyTester's **Settings →
Models** lists the models installed here (`GET /models`) and switches between them
(`POST /config`), so this service owns the choice. The **Model** row in the
**Configuration** tables below stays ignored: whatever it says, the service answers with
the selected model.

### MonkeyTester for Mac

The app includes this service and runs it for you, so there's nothing to connect. Open **Window > Tool Status** and click **Set Up Vision Model** to download the model once.

### MonkeyTester on the same Mac, without the app

Run `bin/monkeytester-ai serve`, then under **Configuration** in MonkeyTester set:

| Setting | Value |
|---|---|
| Endpoint | `http://127.0.0.1:8091/v1/chat/completions` |
| Model | Any value; it's ignored |
| API key | Leave empty |

### MonkeyTester on another machine

Create a token and listen on the network:

```bash
export VTAI_TOKEN=$(openssl rand -hex 32)
echo $VTAI_TOKEN
bin/monkeytester-ai serve --lan
```

Then under **Configuration** in MonkeyTester set:

| Setting | Value |
|---|---|
| Endpoint | `http://<mac-ip>:8091/v1/chat/completions` |
| Model | Any value; it's ignored |
| API key | The token |

In the current MonkeyTester viewer, enter the same token for the `monkeytester-ai` plugin
in its **Settings** page.

`--lan` refuses to start without `VTAI_TOKEN`: on the network the token is mandatory, on
`127.0.0.1` it is optional. Traffic isn't encrypted, so only do this on a network you trust.
See [SECURITY.md](SECURITY.md) for the threat model, token setup and what is not covered.

Press **Test** in MonkeyTester's Configuration to confirm the connection.

## Remote server

Use this when the model runs on another machine: typically llama.cpp's `llama-server` (or vLLM, or
another Ollama's `/v1`) on a box with a GPU, e.g. `http://192.168.125.70:8080`. MonkeyTester's
**Settings → Device Plugins → Configure** has the controls; they map to `POST /config` keys
(see [API.md](API.md)):

- **Address** (`remote_url`): `http(s)://host:port`, optionally with a path. A trailing `/`, `/v1`
  or `/v1/chat/completions` is stripped. The address is only saved if `GET <address>/v1/models`
  answers within 5 seconds, and the model list comes from there.
- **Model** (`remote_model`): one of the ids that server lists.
- **Resize screenshots for Qwen2.5-VL** (`remote_resize`, on by default): runs the same resize,
  system hint and coordinate rescale as Local. Keep it on when the remote model is Qwen2.5-VL
  (it answers in pixels of the resized image). Turn it off for models that answer in pixels of
  the image they were sent (GPT-class, Gemma, most others); then the request goes through
  unchanged except for the model, like Copilot.
- **API key** (`remote_api_key`, optional): for a server started with `--api-key`. It is sent as
  `Authorization: Bearer <key>` to that server only, stored in `.runtime/remote.json` (mode 600;
  `VTAI_REMOTE_CONFIG` moves it) and is write-only: no route returns it, you only see whether one
  is set and its last four characters. A server that refuses the key gets
  "the remote server rejected the API key".

Clearing the address switches back to Local. `/health` reports a remote provider as ready when an
address and a model are set; it never contacts the remote server. Traffic to the remote server is
plain HTTP unless you give an `https://` address.

## GitHub Copilot

MonkeyTester's **Settings → Models** can switch the provider to GitHub Copilot. The sign-in is
GitHub's device flow, run by this service (`POST /copilot/device/start` and `/copilot/device/poll`,
see [API.md](API.md)): you enter a short code at `github.com/login/device`, and the service checks
that the account really has Copilot before it stores anything. The sign-in is kept in
`.runtime/copilot.json` (mode 600; `VTAI_COPILOT_CONFIG` moves it) and no route ever returns it.
**Remove** deletes it and switches back to Local.

Be aware of what this path is:

- With Copilot selected, every screenshot MonkeyTester asks about goes to GitHub.
- It uses the OAuth client of GitHub's own editor plugins and sends editor headers, because there
  is no official public Copilot API. GitHub can change or block this at any time, and then the
  Copilot provider stops working until this code is updated. Local is unaffected.
- Copilot requests are passed through unchanged except for the model: no resizing and no
  coordinate rescaling, because GPT-class models answer in pixels of the image they were sent.

The provider, the model of each provider and the remote address and resize switch are saved in
`.runtime/settings.json` and survive restarts.

## Start it automatically at login

```bash
bin/monkeytester-ai service install
```

For network access, export `VTAI_TOKEN` first and run `service install --lan` (it refuses
without a token). The token is written to the LaunchAgent plist's `EnvironmentVariables`
(`VTAI_TOKEN` key, file mode 600). The service runs with the settings in your shell at the
time you install it, so reinstall it to change them.

To turn it off:

```bash
bin/monkeytester-ai service uninstall
```

## Commands

Run these as `bin/monkeytester-ai <command>`.

| Command | What it does |
|---|---|
| `setup` | Installs Ollama and the model |
| `serve [--lan]` | Runs the service until you press Ctrl-C |
| `status [--json]` | Shows what's installed and running. `--json` prints one object for programs, and exits 1 until setup has been done |
| `ask <image> "<question>"` | Asks the running service about a PNG or JPEG. If the service uses a token, export `VTAI_TOKEN` first |
| `service install [--lan]` | Starts the service now and at every login |
| `service uninstall` | Stops that and removes it |
| `clean [--yes]` | Deletes `.runtime/` and the login item, including the Copilot sign-in, the remote API key and the saved model choice. You'd need to run `setup` again |

## Settings

Set these as environment variables, for example `VTAI_PORT=9000 bin/monkeytester-ai serve`.

| Variable | Default | What it does |
|---|---|---|
| `VTAI_MODEL` | `qwen2.5vl:7b-q4_K_M` | The **starting** Local selection, used until something is saved in `.runtime/settings.json`. MonkeyTester's Settings → Models can switch the model later through the config API without restarting; only models already installed in `.runtime/` can be picked. If a saved model is no longer installed, the service falls back to `VTAI_MODEL`. For Macs under 16GB, use `qwen2.5vl:3b-q4_K_M` (3.2GB) and rerun `setup` |
| `VTAI_TOKEN` | None | The password clients must send (`Authorization: Bearer` or `X-Plugin-Token`) on every route except `/health`. **Mandatory with `--lan`** (any non-loopback bind): `serve --lan` and `service install --lan` refuse to start without it. Optional on the default `127.0.0.1` bind. Never passed to Ollama, `sips` or `curl` |
| `VTAI_PORT` | `8091` | The port MonkeyTester connects to |
| `VTAI_OLLAMA_PORT` | `11435` | Ollama's private port |
| `VTAI_MAX_PIXELS` | `1000000` | The largest image the model sees. Raising it can help with tiny icons but is slower and uses more memory. Stay under about 3M |
| `VTAI_RUNTIME_DIR` | `.runtime` | Where everything is stored, including `settings.json`, `copilot.json` and `remote.json` |
| `VTAI_COPILOT_CONFIG` | `<runtime>/copilot.json` | Where the GitHub Copilot sign-in is stored |
| `VTAI_REMOTE_CONFIG` | `<runtime>/remote.json` | Where the remote server's API key is stored |
| `VTAI_GITHUB_URL`, `VTAI_GITHUB_API`, `VTAI_COPILOT_API` | GitHub's | Base URLs for the Copilot sign-in and API. Only for tests against a fake GitHub |

Set `VTAI_MODEL` the same way for `setup`, `serve` and `service install`; it only picks
where the service starts, a later Settings → Models switch doesn't need any of them rerun.
`serve` still needs Ollama and the `VTAI_MODEL` model installed even when Copilot or a remote
server is selected.

## Troubleshooting

Logs are in `.runtime/logs/`: `ollama.log` for the model, and `service.log` when it runs at login.

| Problem | Fix |
|---|---|
| `Ollama is not installed` or `<model> is not installed` | Run `setup` |
| `Port 11435 is already in use` | A previous run may still be alive. Find it with `lsof -i :11435` and stop it, or set `VTAI_OLLAMA_PORT` |
| `Address already in use` on start | Something's on port 8091, possibly the login service. Check `status`, or set `VTAI_PORT` |
| `401` from `ask` or MonkeyTester | The token doesn't match. Check `VTAI_TOKEN` and MonkeyTester's API key |
| `503 model backend unavailable` | Ollama is down or restarting. Check `ollama.log` |
| `503 GitHub Copilot is not signed in` | Copilot is selected but the sign-in is gone. Sign in again in Settings → Models, or switch to Local |
| Copilot `401`/`403` `upstream_error` | GitHub refused the sign-in (Copilot subscription ended, token revoked, or GitHub changed the editor API). Remove and sign in again |
| `could not reach <address>/v1/models` | The remote server is off, the address or port is wrong, or it isn't OpenAI-compatible. Try `curl <address>/v1/models` from the Mac |
| `the remote server rejected the API key` | The remote server needs a key (`--api-key`) and none or the wrong one is set. Replace the key |
| `503 remote server ... unavailable` | The remote server went away after it was set up. Start it, or switch to Local |
| MonkeyTester's **Test** times out | It only waits 15 seconds, and the model reloads after 30 minutes idle. Try again, and check `status` shows the model as loaded |
| Other machines can't connect | Confirm you used `--lan`, and that the macOS firewall allows incoming connections for Python |
| Digest error | Ollama republished the model. If you trust the new version, update the pin in `monkeytester_ai/config.py` |
| Slow answers or heavy swapping | Close memory-heavy apps such as Xcode, or use the smaller model |
| Taps land in the wrong place | Run the `ask` check above. If that's right, the problem is with that screen, not the setup |

## Limitations

- It has only been tested on a generated image so far, not real app screens or a real device.
- The model can be too agreeable, for example calling a plain red rectangle a "red button".
  Write test descriptions as specifically as you can.
- It handles one request at a time, so parallel test runs wait in line.
- MonkeyTester's loaded-model display won't reflect this service.

## Running the tests

```bash
python3 -m unittest discover tests      # or: nix run .#tests
```

These work on Linux too, and use a fake Ollama, a fake GitHub/Copilot server and a fake
OpenAI-compatible remote server, so they don't download anything or contact GitHub. They don't
check the real model's answers; the `ask` check above does that.
