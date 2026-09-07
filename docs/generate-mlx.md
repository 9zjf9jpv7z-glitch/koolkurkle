# Generate process: mlx_lm.server

Preferred practice after AR 07 (READY Y). Do not re-run live generate
probes from this document.

## Process vs path string

| Field | Value | Meaning |
|---|---|---|
| Process | `mlx_lm.server` | What listens on `127.0.0.1:1234` |
| Client | OpenAI `POST /v1/chat/completions` | Unchanged |
| `path` success | `llmster-headless` | **Code string only.** Withhold product-name claim |
| `path` generate down | `fail-open-only` | Required label. Hits-only, `answer` null |
| Embed | Ollama | `ps` / `stop` only. Never generate |

## Recipe (operator, not CI)

1. Retrieve + CrossEncoder rerank (embed may be resident).
2. `ollama stop "$MAILROOM_EMBED_MODEL"` until `ollama ps` is empty.
3. Start generate via LaunchAgent `com.mailroom.mlx-generate`
   (`launchd/com.mailroom.mlx-generate.plist` +
   `scripts/mlx-generate-server.sh`). Wrapper argv is
   `mlx_lm.server --model "$MAILROOM_MLX_MODEL" --host 127.0.0.1 --port 1234`
   with thinking off (`--chat-template-args '{"enable_thinking":false}'`).
   **KeepAlive is true** — generate-down is `launchctl bootout` / unload,
   not `kill`.
4. `$MAILROOM_GENERATE_MODEL` = `id` from `GET /v1/models` (loaded means loaded).
5. `max_tokens` 512–1024 on the generate request.

Do not open LM Studio.app for Mailroom generate. Do not `lms server start`
as the generate path. Do not Ollama chat/generate.

Client library: `scripts/mailroom_generate.py`. Live CLI remains
`scripts/ask_mail.py`. Headless probes (C/D/E/F):
`scripts/ask_mail_generate_probes.py`.
