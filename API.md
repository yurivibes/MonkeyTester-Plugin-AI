# MonkeyTester-AI HTTP API (api_version 3)

This contract is the only coupling between MonkeyTester and this service. Change
`API_VERSION` in `monkeytester_ai/config.py` when you break it.

This service is also a MonkeyTester **device plugin**: it implements
PLUGIN_API.md (contract `monkeytester-plugin`, `contract_version: 1`) in the MonkeyTester
repo, so MonkeyTester's **Settings → Models** can list and change the model here. The
plugin endpoints (`/devices`, `/actions`, `/jobs`) answer errors in the contract's flat
shape `{"error": message, "code": code}` with the contract's codes; everything else on
this API keeps the nested shape below.

If `VTAI_TOKEN` is set, every request except `/health` must send the token, in either
spelling — both are accepted:

- `Authorization: Bearer <token>` (the OpenAI-style chat call)
- `X-Plugin-Token: <token>` (the plugin contract's spelling)

The same auth applies to the `/copilot/*` routes, exactly as to `/config`.

Any path not listed here returns 404, so Ollama's own API is never reachable. The token's
value is never returned by any `GET`; a rotated token appears in exactly one response, the
one from the request that rotated it.

## Errors

Every failure has a non-2xx status and the same body, which is also the Mobile Controller's
error shape:

```json
{"error": {"code": "model_unavailable", "message": "model backend unavailable: ..."}}
```

Branch on `code`. `message` is for people and can change.

| Status | Code | Meaning |
|---|---|---|
| 400, 411 | `invalid_request` | The request is wrong. Retrying won't help |
| 401 | `unauthorized` | Missing or wrong token |
| 404 | `not_found` | Not a route of this API |
| 413 | `body_too_large` | Body over 20MB, more than 16 images in one chat request, or one image over 15MB decoded |
| 500 | `internal_error` | A bug. `message` is only `internal error: <exception type>`; the details are in the service log |
| 502 | `upstream_error` | GitHub refused or failed the device sign-in start, or listing Copilot models failed while validating `copilot_model` |
| 503 | `model_unavailable` | Ollama is down or restarting, Copilot is selected but not signed in, Copilot is unreachable, or the remote server is unreachable or timed out. Worth retrying |
| Ollama's, the remote server's or Copilot's status | `upstream_error` | Ollama, the remote server or Copilot refused the request |

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | `{ok, service, api_version, provider, model, model_loaded, ollama}` plus the plugin fields `{contract, contract_version, plugin, host, devices, capabilities, error}`. `provider` is `local`, `remote` or `copilot`. `model` is the **active** model (the selected model of the active provider). `ok`: with `local`, Ollama is reachable; with `remote`, `remote_url` and `remote_model` are set; with `copilot`, a sign-in is stored (config-only checks: `/health` never waits on the remote server or GitHub). `model_loaded`: with `local`, false after 30 minutes idle (the next request reloads the model); with `remote` and `copilot`, same as `ok`. `ollama` always reports the private Ollama |
| `GET /v1/models` | `{object, data: [{id, object}]}` with the active model |
| `GET /models` | `{object, data, provider, selected, installed, error}`. `data` lists the **active provider's** models: installed Ollama models (`{id, object}`), the remote server's `/v1/models` ids (`{id, object}`), or the signed-in account's Copilot vision models (`{id, object, name, vendor}`). `selected` is the active model. `installed` is always the private Ollama list (empty while Ollama is offline). `error` is null, or why listing remote or Copilot models failed |
| `GET /devices` | `{devices: [{id, kind, model, state, details}], error: null}`. One device, `id: "gateway"`; `model` is the active model; `state` is `ready` when the active provider is usable (as `/health` `ok`), `offline` otherwise. `details`: `{provider, port, ollama_port, token_configured, model_loaded}` |
| `GET /actions` | `{actions: [...]}`: the plugin actions, each with its parameter schema |
| `POST /jobs` | Start an action as a job; `202 {"job": <job>}` |
| `GET /jobs/<id>?wait=<s>` | The job, blocking up to `wait` (0–60) seconds until it is terminal |
| `POST /jobs/<id>/cancel` | Ask a running job to stop; `200 {"job": <job>}` |
| `GET /config` | `{provider, model: {selected, installed}, remote: {...}, copilot: {...}, token: {configured, last4}, port, ollama_port, max_pixels, api_version, service}`. `model` is the **Local** (Ollama) selection. The token's value is never returned, only `configured` and `last4` (`null` for tokens shorter than 8 characters). See [Providers](#providers) for `remote` and `copilot` |
| `POST /config` | Change settings; returns `{ok, config}` plus `new_token` once when the token rotated |
| `POST /copilot/device/start` | Start the GitHub device sign-in; `200 {user_code, verification_uri, interval, expires_in}` |
| `POST /copilot/device/poll` | Poll the sign-in once; `200 {state, ...}` |
| `POST /copilot/remove` | Delete the stored sign-in; `200` with the `copilot` object |
| `POST /v1/chat/completions` | OpenAI-style chat completion, see below |

`/devices`, `/actions` and `/jobs` follow the plugin contract, including its error
shape and codes (`bad_request`, `unauthorized`, `not_found`, `unknown_action`,
`unknown_device`, `device_busy`, `internal`). `capabilities` in `/health` is empty:
events and uploads aren't implemented.

## Model choice

The service owns the model: `VTAI_MODEL` is only the starting Local selection. MonkeyTester's
**Settings → Models** reads `GET /models` and switches with `POST /config` or the
`set-model` job. Ollama hot-swaps the model, so the next chat request may reload it
(`model_loaded` can become false).

The provider, the Local model, the Copilot model and `remote_url`, `remote_model`,
`remote_resize` are saved to `<runtime>/settings.json` (mode 600, written atomically) on every
change, and that file wins over `VTAI_MODEL` at start. A saved Local model that is no longer
installed falls back to `VTAI_MODEL` (logged). A saved `copilot` provider without a stored
sign-in, or a saved `remote` provider without an address and model, starts as `local`.

## Providers

| Provider | Answers with | Model key in `POST /config` |
|---|---|---|
| `local` (default) | The private Ollama, with the resize/rescale pipeline below | `model` |
| `remote` | An OpenAI-compatible server at `remote_url` (e.g. llama.cpp's `llama-server`), with the resize/rescale pipeline unless `remote_resize` is false | `remote_model` |
| `copilot` | GitHub Copilot's chat API, request passed through unchanged except `model` | `copilot_model` |

`GET /config` → `remote`:

```json
{"url": "http://192.168.125.70:8080", "model": "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf",
 "models": [{"id": "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"}],
 "resize": true, "key": {"configured": true, "last4": "abcd"}, "error": null}
```

`url` is the normalised address (`""` when unset), `model` the selected remote model (`""` until
chosen). `models` comes from `GET {url}/v1/models` (the OpenAI list shape), asked with a 5 second
timeout on every `GET /config`; it is `[]` when no address is set or the server can't be listed,
and then `error` says why (`could not reach <url>/v1/models: <reason>`, or `the remote server
rejected the API key`). `resize` defaults to `true`. The API key is stored in
`<runtime>/remote.json` (mode 600, atomic; `VTAI_REMOTE_CONFIG` moves it) and is write-only:
only `key.configured` and `key.last4` (empty for keys under 8 characters) are ever returned.

`GET /config` → `copilot`:

```json
{"configured": true, "user": "octocat", "last4": "abcd", "saved_at": "2026-11-11T10:00:00",
 "model": "gpt-4o",
 "models": [{"id": "gpt-4o", "name": "GPT-4o", "vendor": "Azure OpenAI"}],
 "error": null}
```

`model` is the selected Copilot model (default `gpt-4o`, kept while signed out). `models` lists
the account's vision-capable chat models that are enabled in GitHub's model picker; `[]` when not
signed in or when listing failed, in which case `error` says why. `last4` is the last four
characters of the stored GitHub token; the token itself is never returned.

### GitHub sign-in

GitHub's OAuth device flow, using the OAuth client of GitHub's editor plugins (there is no public
Copilot API; it may break). The routes take no body (an empty JSON object is fine too).

1. `POST /copilot/device/start` → `200 {"user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device", "interval": 5, "expires_in": 900}`.
   Show the code and the URI. A GitHub failure is `502 upstream_error`. Starting again replaces
   any sign-in in progress.
2. Every `interval` seconds, `POST /copilot/device/poll` → always `200` with `state`:

| `state` | Meaning | Other fields |
|---|---|---|
| `pending` | The user hasn't entered the code yet | `interval` |
| `slow_down` | Polling too fast; use the new `interval` | `interval` |
| `done` | Signed in and stored | the whole `copilot` object above, flattened in |
| `expired` | The code expired; start again | `error` |
| `denied` | The user declined | `error` |
| `error` | No sign-in in progress, GitHub unreachable, or the account has no Copilot (then nothing is stored) | `error` |

3. `POST /copilot/remove` → `200` with the `copilot` object (`configured: false`). If the
   provider was `copilot`, it switches to `local` and that is saved.

### Jobs

The one action: `set-model`, choosing the model that answers future chat requests
(`needs_device: false`, `default_timeout_ms: 60000`). `POST /jobs` body:

```json
{"action": "set-model", "params": {"model": "qwen2.5vl:3b-q4_K_M"}}
```

The finished job comes back in `202 {"job": ...}` with the contract's job fields; its
`result` is `{"previous": "<id>", "current": "<id>"}`. A model that isn't installed ends
the job `failed` with error code `model_unavailable`; unknown actions get `404 unknown_action`
at the HTTP level. Finished jobs stay queryable for at least 10 minutes.

### Config writes

`POST /config` takes one or more of:

- `{"model": "<id>"}` — validated against the installed Ollama models; unknown models
  get `400 invalid_request`.
- `{"token": "<new>"}` or `{"token": {"generate": true}}` — rotates the token for the
  running service and returns the new value once in `new_token`; where the token is
  persisted (the start-at-login service) it is persisted there too.
- `{"port": ...}` — always `400 invalid_request`: a running server cannot rebind.
  Stop the service and restart it with `VTAI_PORT` (or reinstall the login service).
- `{"provider": "local" | "remote" | "copilot"}` — `copilot` without a stored sign-in is
  `400 invalid_request` "sign in to GitHub Copilot first"; `remote` without `remote_url` and
  `remote_model` (stored or in the same request) is `400` "set remote_url and remote_model first".
- `{"remote_url": "<address>"}` — normalised (surrounding spaces and a trailing `/`, `/v1` or
  `/v1/chat/completions` are stripped); must be `http(s)://host[:port][/path]`. It is checked with
  `GET <address>/v1/models` within 5 seconds, sending the stored key, or the `remote_api_key` of the
  same request, as Bearer. Unreachable, a non-2xx answer or not an OpenAI model list is
  `400 invalid_request` "could not reach <address>/v1/models: <reason>"; a 401/403 is `400` "the
  remote server rejected the API key". Nothing is stored on failure. Setting a different address
  keeps `remote_model` only if the new server lists it; otherwise the model is cleared and, if the
  provider was `remote`, it switches to `local`. `""` clears the address and the model, and switches
  the provider to `local` if it was `remote` (saved).
- `{"remote_model": "<id>"}` — must be one of the ids `remote_url` lists (`400` otherwise, or when no
  address is set). Setting it doesn't change the provider.
- `{"remote_resize": true | false}` — run the Local resize/hint/rescale pipeline for remote requests.
- `{"remote_api_key": "<key>"}` sets the key, `null` or `""` removes it. Never returned.
- `{"copilot_model": "<id>"}` — validated against the signed-in account's vision models
  (`copilot.models`); not signed in or not in the list is `400 invalid_request`; if the list
  can't be fetched, `502 upstream_error`. Setting it doesn't change the provider.

`model` always means the Local Ollama model, whichever provider is active. Every key is
validated before anything changes, so a rejected request changes nothing. Unknown keys are
`400 invalid_request`.

The `set-model` job applies to the **active** provider: an installed Ollama model with
`local`, a model listed by the remote server with `remote`, a Copilot vision model of the account
with `copilot`; anything else fails the job with `model_unavailable`.

## Chat completions

The request and response follow the OpenAI format, with these differences:

- **`model`** is ignored and replaced with the active model.
- **`stream: true`** returns 400. Coordinates can't be rescaled until the whole reply exists.
- **Images** must be `image_url` parts holding base64 data URLs of PNG or JPEG images. The body limit is 20MB.

### With the `remote` provider

The request goes to `{remote_url}/v1/chat/completions` with `model` replaced by `remote_model`, and
`Authorization: Bearer <key>` only when a key is stored. With `remote_resize: true` (the default)
the images, system hint and reply coordinates go through exactly the same pipeline as `local`
([Coordinates](#coordinates-local-provider)); with `false` the request is passed through unchanged
except `model`, as for Copilot. The 20MB limit and `stream: true` → 400 still apply.

- No address or model set → `503 model_unavailable`.
- The remote server answers an HTTP error → that status with `upstream_error` (a 401 there means
  the remote server refused the stored API key).
- Network failure or timeout → `503 model_unavailable`.

### With the `copilot` provider

The request goes to Copilot's `/chat/completions` unchanged except `model` (the selected
Copilot model), with the editor headers and `Copilot-Vision-Request: true`. There is no
resizing, no system hint and no rescaling: GPT-class models already answer in pixels of the
image they were sent. The 20MB limit and `stream: true` → 400 still apply.

- Not signed in → `503 model_unavailable` "GitHub Copilot is not signed in".
- Copilot answers an HTTP error → that status with `upstream_error`. A 401 first refreshes the
  short-lived Copilot token and retries once.
- GitHub or Copilot unreachable → `503 model_unavailable`.

### Coordinates (`local` provider, and `remote` with `remote_resize`)

Coordinates in replies are device pixels, meaning pixels of the image you sent.

For this to work, each image is resized to multiples of 28 within `VTAI_MAX_PIXELS`. When
a request has exactly one image, the gateway then:

1. Adds a system message telling the model the resized dimensions.
2. Finds the JSON in the reply, with or without a code fence.
3. Scales `coordinates` and `position` (`[x, y]`) and `bbox` and `bbox_2d`
   (`[x1, y1, x2, y2]`) at any depth, and clamps them to the image.
4. When an object has both `coordinates` and a box, sets `coordinates` to the box's center.
   Callers tap `coordinates`, and the model tends to put the top-left corner there.

Replies without parseable JSON pass through unchanged. Requests with several images are
resized but **not** rescaled, because a coordinate wouldn't say which image it refers to.

## Status JSON

`bin/monkeytester-ai status --json` keeps its keys (`selected` and `installed` stay the Local
Ollama model and list) and adds `provider` (`local`, `remote` or `copilot`),
`copilot: {configured, user}` and `remote: {url, model}` (from `settings.json`, `""` when unset;
never the key). Without a running gateway, `provider` comes from `settings.json`.

## Additions within version 3

The `remote` provider (`remote_url`, `remote_model`, `remote_resize`, `remote_api_key` in
`POST /config`, the `remote` block of `GET /config`, `remote` in `status --json`) is additive:
`API_VERSION` stays 3 and clients that ignore it see no change.

## Migrating from version 2

New routes (`/models`, `/devices`, `/actions`, `/jobs`, `/config`), `/health` gained the
plugin fields, `X-Plugin-Token` is accepted as an alias of the bearer header, and the
model is now selected through this API instead of being pinned by configuration. The
selection persists in `settings.json`. A second provider, GitHub Copilot, can answer chat
requests (`provider`, `copilot_model`, `/copilot/*`); with the default `local` provider nothing
about `/v1/chat/completions` changed except that its model is the selected one.

## Migrating from version 1

Only the error body changed, from `{"error": "text"}` to `{"error": {"code": ..., "message": ...}}`,
and `/health` gained `service`.
