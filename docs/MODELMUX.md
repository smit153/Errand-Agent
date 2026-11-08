# Setting up the language model (ModelMux)

The Errand Agent reaches a real LLM through [ModelMux](https://github.com/smit153/modelmux/): a
local, OpenAI-compatible Chat Completions gateway that fronts **Claude Code** or **Codex** using your
own login. The gateway runs on this machine (loopback only); the models run on the providers'
servers. Automated tests never need it: they use a scripted model. Why ModelMux, and what that
means for the brief's "run locally" wording, is in [DECISIONS §17](DECISIONS.md).

## Requirements

- Docker with Compose (on Windows, inside WSL2).
- ModelMux CLI: `npm install -g modelmux-cli@0.3.1`. The CLI pins the server image to
  `ghcr.io/smit153/modelmux@sha256:19126eb921debdf123eb90382ea4ce478e15febc8102e95c2153711b7f37c2a0`.

## Log in and start

Run these in your own terminal. Login uses the provider's browser or device flow; never paste
credentials anywhere else.

```bash
modelmux login codex      # and/or: modelmux login claude
modelmux up               # starts the logged-in providers
modelmux status
```

| Provider | API base URL | Select with |
| --- | --- | --- |
| Claude Code | `http://127.0.0.1:8101/v1` | `scripts/serve.sh claude …`, `scripts/smoke.sh claude` |
| Codex | `http://127.0.0.1:8102/v1` | `scripts/serve.sh codex …`, `scripts/smoke.sh codex` |

ModelMux refuses to serve until login and a live model check succeed, so a created container is
not proof that it's ready: check `modelmux status`. Other commands: `modelmux logs codex --tail 30`,
`modelmux doctor`, `modelmux down` (logins are kept).

## The gateway key

ModelMux authenticates clients with its own key, stored in `~/.config/modelmux/secrets.env`
(mode `0600`). It is not a provider API key. The project's launch scripts (`scripts/with_model.sh`,
via `scripts/modelmux_key.sh`) read it from that file and pass it to the app in an environment
variable. It is never committed, printed, logged or traced.

## How the app uses it

- Only `/v1/chat/completions` with `tools`. Action calls send `tool_choice: "required"`;
  ModelMux returns validated tool-call proposals, and the app validates them again before doing
  anything.
- ModelMux ignores `temperature`, `seed` and `max_tokens`. The app sends them anyway and records the
  ignored list in every model-call trace, so real-model runs are not seeded. Offline evaluations are
  deterministic because they use the scripted model.
