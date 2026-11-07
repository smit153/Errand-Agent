# ModelMux in WSL

The Errand Agent will use the user's [ModelMux](https://github.com/smit153/modelmux/) as a local Chat Completions gateway. Both Claude Code and Codex services are being prepared. **The user will complete provider login; application implementation remains paused.** Models run on the providers' infrastructure, not on this laptop.

## Installed environment

- Ubuntu 26.04 on WSL2, distribution `Ubuntu`, user `laptop`.
- Docker Engine 29.9.3 and Docker Compose 5.7.0, installed from Docker's official Ubuntu repository. Docker is enabled through systemd.
- ModelMux CLI 0.3.1, installed with `npm install -g modelmux-cli@0.3.1`.
- The CLI pins the server image to `ghcr.io/smit153/modelmux@sha256:19126eb921debdf123eb90382ea4ce478e15febc8102e95c2153711b7f37c2a0`.
- Reference source checkout: `.local/modelmux`, commit `5fcddd1054cf7dc361d4e4b36547fe17146cf4fd`; excluded from this project's Git tracking.

`laptop` has been added to the Docker group. **Open a new WSL terminal** to refresh group membership before running the commands below. The group grants access to the local Docker daemon. Docker Desktop is not required for this installation.

## Finish provider login

Run these yourself in an interactive WSL terminal:

```bash
modelmux login claude
modelmux login codex
modelmux status
```

The login commands use the providers' own browser/device flows, retain each login in its own Docker volume, then start and test that provider. Do not paste credentials into chat. Login does not require sharing the host's CLI credential files with the container.

| Service | API base URL | Login volume |
| --- | --- | --- |
| `modelmux-claude` | `http://127.0.0.1:8101/v1` | `modelmux_claude-home` |
| `modelmux-codex` | `http://127.0.0.1:8102/v1` | `modelmux_codex-home` |

ModelMux intentionally rejects startup until its login and live model checks succeed. An unauthenticated container can be created and started, but will exit instead of serving requests. Do not treat container creation as API readiness.

## Daily commands

```bash
modelmux up
modelmux status
modelmux logs claude --tail 30
modelmux logs codex --tail 30
modelmux doctor
modelmux down
```

`up` starts only logged-in providers. `down` retains the login volumes. `doctor` and login/startup checks may contact the providers; a successful service startup includes a small real inference request.

For direct container inspection:

```bash
docker compose -f "$HOME/.config/modelmux/compose.yaml" ps -a
```

## Client configuration

```bash
modelmux config openai-python --provider claude
modelmux config openai-python --provider codex
```

The generated snippets refer to `MODELMUX_API_KEY`. The gateway key is stored in `~/.config/modelmux/secrets.env` with mode `0600`; it authenticates clients to ModelMux and is not a provider API key. The Compose file and client key stay outside this repository. Never commit the key or print it in traces.

Use `/v1/chat/completions`, not `/v1/responses`. With LangChain, explicitly set `use_responses_api=False`. Discover available model IDs from `/v1/models` after login. ModelMux returns validated tool-call proposals; the Errand Agent still owns the approval gate and mock-tool execution. `max_tokens`, `temperature`, and `seed` are ignored by the gateway, so application execution limits and scripted offline evaluations remain necessary.

## Verification status

- Docker `hello-world`: passed.
- ModelMux image download and service startup: in progress.
- Provider authentication and live model verification: deferred to the user.
- Errand Agent tests: none exist yet; implementation is paused.
