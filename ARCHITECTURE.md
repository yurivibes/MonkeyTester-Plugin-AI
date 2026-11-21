# Architecture

MonkeyTester-Plugin-AI gives MonkeyTester a vision model that runs on an Apple silicon Mac. It
has two parts: a private copy of Ollama that runs the model, and a small Python gateway
that is the only way in. The gateway can instead forward requests to an OpenAI-compatible
server at a user-entered address (the `remote` provider, e.g. llama.cpp on a GPU machine) or to
GitHub Copilot (the `copilot` provider), using a GitHub sign-in it stores itself. For setup and usage, see [README.md](README.md). For the exact
HTTP contract, see [API.md](API.md).

## Where it fits

```
                     screenshot                  chat completion + image
Mobile Controller ───────────────► MonkeyTester ─────────────────────────► MonkeyTester-Plugin-AI
   (phones)        GET /frame/<id>   (test logic) ◄─────────────────────────   (this project)
                                                       JSON with coordinates
```

MonkeyTester decides what to do with the answers, and this project only answers questions
about images. The Controller never talks to this service. The only coupling to MonkeyTester
is the HTTP contract, which is the OpenAI-style API that `vision_analyzer.py` already speaks,
so MonkeyTester needs only a configuration change. MonkeyTester-Mac bundles this code and runs
`serve` itself. It uses `status --json` to decide whether setup is needed, and `VTAI_*` variables
to keep everything in its data folder, on ports it picks, with a token generated at each launch.
MonkeyTester's **Settings → Models** reads and changes the model through this service's
config API (`GET /models`, `POST /config`) — the model choice belongs to this service, and
the coupling is still only the HTTP contract, which is also the MonkeyTester device plugin
contract (`PLUGIN_API.md` in the MonkeyTester repo, `contract_version: 1`).

## Components

```
┌─ Mac ─────────────────────────────────────────────────────────────┐
│                                                                   │
│  bin/monkeytester-ai ask ──┐                                      │
│                            ▼                                      │
│  MonkeyTester ──► Gateway :8091 ─┬─► Ollama 127.0.0.1:11435       │
│                   127.0.0.1, or  │     │    (provider local)      │
│                   0.0.0.0 --lan  │     └─► Qwen2.5-VL (Metal)     │
│                                  ├─► <remote_url>/v1 ─────────────┼──► model server
│                                  │     (provider remote)          │   (llama.cpp, ...)
│                                  └─► api.githubcopilot.com ───────┼──► GitHub
│                                        (provider copilot)         │
│  .runtime/  ollama/  models/  home/  logs/                        │
│             settings.json  copilot.json  remote.json              │
└───────────────────────────────────────────────────────────────────┘
```

| File | Responsibility |
|---|---|
| `monkeytester_ai/config.py` | Pinned Ollama version, checksum and model digests; settings from `VTAI_*` variables |
| `monkeytester_ai/runtime.py` | Install checks, download and unpack, Ollama's process and environment, model checks |
| `monkeytester_ai/imaging.py` | Image dimensions, resize calculation, `sips`, rescaling coordinates in replies |
| `monkeytester_ai/gateway.py` | HTTP server, auth, request handling, provider switch, persisted selection (`settings.json`), Ollama supervision |
| `monkeytester_ai/remote.py` | Remote server: address normalisation, `/v1/models` listing (5 s timeout), the write-only API key in `remote.json` |
| `monkeytester_ai/copilot.py` | GitHub Copilot: device-flow sign-in, `copilot.json`, Copilot token exchange and cache, vision model list, chat forwarding |
| `monkeytester_ai/cli.py` | Commands, including the LaunchAgent |

The project uses only the Python standard library and runs on the Python 3.9 that ships with
macOS. Image resizing uses `sips`, which is built into macOS.

## Life of a request

Using MonkeyTester's `FIND "login button"` as the example, on a 1170x2532 screenshot:

1. **Auth:** the gateway checks the Bearer token, if one is configured.
   **Provider branch** (`local` / `remote` / `copilot`):
   - `copilot`: the request skips steps 3, 4, 6 and 7: only the model is replaced (with the
     selected Copilot model), and it goes to Copilot's `/chat/completions` with a short-lived
     Copilot token and the vision header. A 401 refreshes the token and retries once.
   - `remote`: the model is replaced with `remote_model` and the request goes to
     `<remote_url>/v1/chat/completions` instead of Ollama, with `Authorization: Bearer <key>` only
     when a key is stored. With `remote_resize` on (the default) steps 3, 4, 6 and 7 run exactly
     as for `local`; off, they are skipped as for Copilot. An HTTP error from the server comes back
     with its status and `upstream_error`; a network failure is `503 model_unavailable`.
   - `local`: the rest of this list.
2. **Model:** the requested model name is replaced with the configured one, so MonkeyTester's
   setting doesn't matter.
3. **Resize:** the image is resized with `sips` to 672x1456. Those are multiples of 28 and fit
   within `VTAI_MAX_PIXELS`.
4. **Hint:** a system message tells the model the image size, and that a single point must be an
   element's center. This is only added when the request has exactly one image.
5. **Inference:** the request goes to Ollama, which handles one at a time and queues the rest.
6. **Rescale:** coordinates in the reply are scaled back to 1170x2532 and clamped to the image.
7. **Center:** if the reply has a box, `coordinates` is replaced with the box's center.
8. **Reply:** MonkeyTester receives device pixels and taps through the Controller.

## Design decisions

### Why there is a gateway instead of exposing Ollama

- **Security:** Ollama has no authentication, and its port also allows pulling and deleting
  models. Ollama listens only on localhost, and the gateway exposes three paths and returns 404
  for everything else.
- **Tap accuracy:** MonkeyTester treats model coordinates as device pixels, which needs the
  geometry handling below.

### Why the plugin owns GitHub Copilot

MonkeyTester used to talk to Copilot itself, with its own sign-in file. Now the plugin owns
both providers, so there is a single place that holds model credentials, and the viewer only
knows the plugin: it lists models, switches provider and runs the GitHub sign-in through this
API, and sends every chat request here whichever provider answers it. The sign-in is plaintext
at rest in `copilot.json` (mode 600) because the service reads it unattended; a key on the same
disk would add nothing. No route returns it. `/health` checks only that the file exists, so it
never waits on GitHub.

The Copilot path uses the OAuth client of GitHub's editor plugins and their headers, because
there is no public Copilot API. That can break without notice; Local keeps working.

Copilot requests aren't resized or rescaled: GPT-class models answer in pixels of the image
they were sent, which is what MonkeyTester expects, and the system hint is Qwen-specific.

### Why the remote provider has a resize switch

The remote provider exists so that a bigger GPU machine can run the model. It may run the same
Qwen2.5-VL (then the pipeline is needed, so it is on by default) or a model that answers in pixels
of the image it was sent (then resizing and rescaling would distort the answer, so it can be
switched off). The API key, like the Copilot sign-in, is stored by the plugin only, mode 600, and
never returned; the viewer sees `{configured, last4}`. Listing the remote models has a 5 s timeout
so that `GET /config` stays responsive when the remote machine is off, and `/health` checks only
that an address and a model are set.

### Why the gateway resizes images

Qwen2.5-VL reports positions relative to its resized input, not the original image. Ollama's
internal resizing isn't documented. If the gateway sends an image that already has valid
dimensions, it knows what size the model saw and can convert the answer back exactly.

The 1M pixel default also limits memory use. A full-resolution phone screenshot takes roughly
3,800 tokens of the 4,096-token context, leaving little for the prompt and reply. At 1M pixels
it takes about 1,300. The cost is that very small icons may be harder to read.

Requests with several images, such as `screenshot_diff.py`, are resized but not rescaled,
because a coordinate wouldn't say which image it refers to.

### Why `coordinates` is overwritten

Testing on the real model showed it puts the box's top-left corner in `coordinates`, while its
`bbox` is accurate to within a few pixels. MonkeyTester taps `coordinates`, so every tap landed
on the element's edge. Using the box's center fixes this. Prompts that don't ask for a box, such
as MonkeyTester's input field check, rely on the system message alone.

### Why everything lives in `.runtime/`

Deleting one folder has to remove everything, and this must not conflict with any Ollama already
on the Mac. Ollama therefore runs with:

| Variable | Value | Reason |
|---|---|---|
| `HOME` | `.runtime/home` | Keeps Ollama's keys out of `~/.ollama` |
| `OLLAMA_MODELS` | `.runtime/models` | Keeps the weights in the project |
| `OLLAMA_HOST` | `127.0.0.1:11435` | Avoids Ollama's usual port, and the network |
| `OLLAMA_NUM_PARALLEL` | `1` | Limits memory use |
| `OLLAMA_CONTEXT_LENGTH` | `4096` | Makes the context size explicit |
| `OLLAMA_KEEP_ALIVE` | `30m` | Frees memory when idle |

Any other `OLLAMA_*` variables in the environment are removed. The optional LaunchAgent is the
only file written outside the project, and `service uninstall` or `clean` removes it.

## Process lifecycle

`serve` does the following, in order:

1. Refuses `--lan` without a token, stops any Ollama left behind by a gateway that was killed
   (recorded in `.runtime/ollama.pid`), and refuses to start if Ollama's port is still taken.
2. Starts Ollama and waits until it responds.
3. Checks the model is installed and its digest matches the pin.
4. Applies `settings.json` (a saved Local model that is no longer installed falls back to
   `VTAI_MODEL`), starts listening, and with the `local` provider loads the model in the
   background so that the first request isn't slow. A saved `remote` provider without an address
   and model starts as `local`. With `remote` or `copilot` nothing is preloaded; switching
   back to `local` lets the next request load the model.
5. Checks every 2 seconds that Ollama is alive, restarting it and reloading the model if not.
   A failed restart is logged and retried, so the supervisor itself never stops.
6. On SIGTERM or SIGINT, stops listening and stops Ollama.

## Security model

| Boundary | Protection |
|---|---|
| Network to gateway | Localhost by default. `--lan` (any non-loopback bind) requires a token, compared in constant time; without one the service refuses to start. `/health` is open and reveals only the provider, model name, status and the plugin contract's host OS/hostname, never a secret |
| Gateway to Ollama | Ollama is reachable only from the Mac itself |
| Downloads | Ollama's SHA-256 and the model digest are pinned. The archive is rejected if any entry would land outside its folder |
| Image input | PNG and JPEG only, 20MB body limit (`Content-Length` required), at most 16 images per request and 15MB per decoded image, 100M pixel source limit, and images are checked before reaching `sips` |
| Errors | Bad JSON is a 400; an unexpected exception is a 500 whose message is only the exception type. Details go to the log |
| Child processes | argv lists, no shell; `VTAI_TOKEN` is removed from the environment of Ollama, `sips` and `curl` |
| Secrets | A token stored by `service install` goes in a plist with mode 600. The GitHub sign-in goes in `copilot.json` and the remote API key in `remote.json`, both mode 600, written atomically, and no route returns them |
| Copilot | With the `copilot` provider, screenshots leave the Mac for GitHub's Copilot API |
| Remote server | With the `remote` provider, screenshots (and the key, if set) go to the entered address, in plain text unless it is `https://` |

**Not protected:** traffic is plain HTTP, so on `--lan` the token and screenshots can be read by
anyone on the network. Other programs running on the Mac can also reach Ollama's port directly.
Secrets are plaintext at rest (mode 600). See [SECURITY.md](SECURITY.md).

## Failure behaviour

Every failure reaches the client as the one error body in `API.md`, including unexpected
exceptions while handling a request, so a client never gets a dropped connection it can't explain.
A request that can't reach Ollama gets a 503 `model_unavailable`, which is safe to retry.

## Testing

`tests/` runs on Linux and macOS using `fake_ollama.py`, either in-process or as a fake `ollama`
executable, plus a fake `sips`, and `fake_github.py`, which stands in for github.com,
api.github.com and the Copilot API (its token exchange points the Copilot API back at itself), and
`fake_remote.py`, an OpenAI-compatible server with only `GET /v1/models` and
`POST /v1/chat/completions` (optionally key-protected, answering 401 like llama-server). These tests cover the gateway's logic and process handling, but
can't show that the model gives good answers. That needs the real model, using the `ask` check in
the README.

## Known limits

- Only tested on a generated image so far, not real app screens or a real device.
- The model can be lenient: it reported a plain red rectangle as a "red button".
- One request runs at a time, so parallel test runs queue up.
- Streaming isn't supported, because coordinates can't be rescaled until the reply is complete.
- MonkeyTester's loaded-model display queries `127.0.0.1:8080`, so it won't reflect this service.
