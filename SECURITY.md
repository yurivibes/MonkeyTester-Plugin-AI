# Security: MonkeyTester-Plugin-AI

MonkeyTester-Plugin-AI fronts a private Ollama (which has **no authentication** of its own and can
pull and delete models), holds the GitHub Copilot sign-in and the remote server's API key,
and forwards screenshots to whichever provider is selected. This document covers this
service only; the family-wide overview (every service, its port, bind and auth, and the
hardening tiers) lives in the MonkeyTester repo's `SECURITY.md`.

## Threat model

- **Where:** a home LAN (192.168.125.0/24), one user. The service runs on the Mac
  (192.168.125.71) as a LaunchAgent (`service install --lan`). The MonkeyTester viewer on
  the Linux host (192.168.125.70) calls it over the LAN on port 8091.
- **Who we defend against:** other devices on the LAN (guests, a compromised IoT device, a
  browser page making requests to LAN addresses) that can reach port 8091.
- **Not in scope:** other programs already running on the Mac as any user (they can reach
  Ollama's loopback port directly), and anyone who can read your user's files.

"No known holes" is the goal; this is not a claim of zero vulnerabilities.

## What is protected, and how

| Area | Protection |
|---|---|
| Bind | `127.0.0.1` by default. `serve --lan` (and `service install --lan`) listens on all interfaces and **requires `MTAI_TOKEN`**: without it the command refuses to start, exits with status 1 and says how to set the token. The check is generic: any non-loopback bind needs a token. |
| Access | Every route except `GET /health` needs the token, as `Authorization: Bearer <token>` or (plugin routes) `X-Plugin-Token: <token>`, compared with `hmac.compare_digest`. |
| `/health` | Unauthenticated and contains no secret: service/contract identity, active provider and model, readiness, the plugin contract's `host: {os, hostname}` and `docs: "/docs"` when README.md is readable. No token, key, sign-in, remote address or Copilot user. |
| `/docs` | Needs the token like every other route. Renders README.md (raw HTML in it is escaped, only http(s)/mailto/`#` links kept) and sends `Content-Security-Policy: default-src 'none'; ...`, `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`. |
| Ollama | Bound to `127.0.0.1:<MTAI_OLLAMA_PORT>` only (`OLLAMA_HOST` is forced; any `OLLAMA_*` from the caller's environment is dropped). Only `POST /v1/chat/completions` is forwarded; Ollama's admin routes are not reachable through the gateway. |
| Secrets at rest | `copilot.json` (GitHub OAuth token) and `remote.json` (remote API key) are written atomically with mode 0600; the LaunchAgent plist that holds `MTAI_TOKEN` is created with mode 0600 and rewritten 0600 on token rotation. |
| Secrets over HTTP | No route returns a secret. `GET /config` shows `token: {configured, last4}` (last4 only for tokens of 8+ characters), `copilot: {configured, user, last4, saved_at}` and `remote.key: {configured, last4}`. A token rotated through `POST /config` is returned exactly once, in that response. |
| Copilot token handling | The long-lived OAuth token is sent only to `api.github.com`; the short-lived Copilot token is cached in memory only and sent only to the API base from GitHub's token exchange when it is `https://` (or the configured base), else to the default `https://api.githubcopilot.com`. |
| Child processes | argv lists only, never `shell=True`; `--` before the download URL. `MTAI_TOKEN` is removed from the environment of Ollama, `sips` and `curl`. |
| Requests | `Content-Length` required (411), body ≤ 20 MB (413, checked before reading), at most 16 images per chat request and 15 MB per decoded image (413), image dimensions ≤ 100 M pixels and PNG/JPEG only before `sips` sees them, bad JSON (including invalid UTF-8 and over-deep nesting) → 400, 60 s socket timeout per client read/write. |
| Errors | Unexpected exceptions answer `500 internal_error` with only the exception type (`internal error: ValueError`); details go to the service log, never to the client. |
| Outbound URLs | `urlopen` only opens `http://` / `https://`; `remote_url` must be `http(s)://host[:port][/path]` without credentials, query or fragment. |
| Downloads | Ollama's archive SHA-256 and the model digests are pinned; archive entries that would land outside the install folder are refused. |

## Token setup

1. Generate a token:

   ```sh
   python3 -c 'import secrets; print(secrets.token_hex(32))'
   ```

2. Give it to the service. Either let the CLI write the LaunchAgent:

   ```sh
   export MTAI_TOKEN=<the token>
   bin/monkeytester-ai service install --lan
   ```

   which stores it in `~/Library/LaunchAgents/com.blackjackfruit.monkeytester-ai.plist`
   (mode 0600) under the `EnvironmentVariables` key:

   ```xml
   <key>EnvironmentVariables</key>
   <dict>
     <key>MTAI_TOKEN</key><string>the-token</string>
     ...
   </dict>
   ```

   or add that key to an existing plist yourself and `launchctl bootout` / `bootstrap` it.
3. Enter **the same token** for the `AI` plugin in the MonkeyTester viewer's
   **Settings** page. The viewer sends it on every call.

To rotate: `POST /config {"token": {"generate": true}}` (returns the new token once and
rewrites the plist), or reinstall the service with a new `MTAI_TOKEN`; then update the
viewer's Settings.

## Not covered

- **No TLS.** On `--lan`, the token and the screenshots travel as plaintext HTTP. For access
  from outside the LAN use a VPN (WireGuard, Tailscale), never a port forward.
- **Plaintext secrets at rest.** `MTAI_TOKEN` in the plist, the GitHub OAuth token in
  `copilot.json` and the remote API key in `remote.json` are plaintext (mode 0600). Anyone
  with your user's files has them.
- **Local programs** on the Mac can reach Ollama's loopback port without a token.
- **Providers:** with `copilot` screenshots go to GitHub; with `remote` they (and the key)
  go to the entered address, in plaintext unless it is `https://`.
- Not done yet (tier 3+): TLS/VPN by default, launchd sandboxing, CI dependency/secret
  scanning.

## Reporting a problem

Open an issue in this repository's issue tracker, or tell the owner directly
for anything that should not be written down first.
