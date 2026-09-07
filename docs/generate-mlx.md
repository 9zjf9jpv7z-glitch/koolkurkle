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

## One-command install (MBP)

From a checkout of this repo:

```zsh
# MBP — copy generate scripts + docs into $HOME/MailArchive, stage LaunchAgent, bootstrap, kickstart, curl /v1/models
./scripts/install-mlx-generate.sh
```

RunAtLoad is **false**. The installer kickstarts after bootstrap. KeepAlive is
**true**.

```zsh
# MBP — files only (no launchctl)
./scripts/install-mlx-generate.sh stage
```

```zsh
# MBP — bootstrap + kickstart an already-staged plist
./scripts/install-mlx-generate.sh load
```

```zsh
# MBP — generate-down (bootout, not kill)
./scripts/install-mlx-generate.sh down
```

```zsh
# MBP — verify listener
curl -sS http://127.0.0.1:1234/v1/models
```

What `install` does:

1. Copy `scripts/mlx-generate-server.sh`, `scripts/ask_mail.py`,
   `scripts/mailroom_generate.py`, `scripts/ask_mail_generate_probes.py`,
   and this installer into `$HOME/MailArchive/scripts/` (plus docs into
   `$HOME/MailArchive/docs/`).
2. Stage `launchd/com.mailroom.mlx-generate.plist` →
   `$HOME/Library/LaunchAgents/` with `__HOME__` substituted for `$HOME`
   (launchd does not expand `$HOME`).
3. `launchctl bootstrap gui/$(id -u)` then `kickstart` (RunAtLoad is false).
4. `curl http://127.0.0.1:1234/v1/models`.

HARD DECK: the installer refuses a stub `ask_mail.py` (MCP placeholder /
tiny file). It copies the SoR CLI; it does not replace it with a stub.

## Generate-down (KeepAlive)

Do **not** `kill` the mlx PID. KeepAlive would restart it.

```zsh
# MBP — generate-down
./scripts/install-mlx-generate.sh down
```

```zsh
# MBP — equivalent bootout
launchctl bootout "gui/$(id -u)/com.mailroom.mlx-generate"
```

```zsh
# MBP — port free is not Done until this paste
lsof -nP -iTCP:1234 -sTCP:LISTEN || echo ":1234 free"
```

## Recipe (operator, not CI)

1. Retrieve + CrossEncoder rerank (embed may be resident).
2. `ollama stop "$MAILROOM_EMBED_MODEL"` until `ollama ps` is empty.
3. Start generate via LaunchAgent `com.mailroom.mlx-generate`
   (`./scripts/install-mlx-generate.sh` or `load`). Wrapper argv is
   `mlx_lm.server --model "$MAILROOM_MLX_MODEL" --host 127.0.0.1 --port 1234`
   with thinking off (`--chat-template-args '{"enable_thinking":false}'`).
   **KeepAlive is true** — generate-down is `launchctl bootout` / `down`,
   not `kill`.
4. `$MAILROOM_GENERATE_MODEL` = `id` from `GET /v1/models` (loaded means loaded).
5. `max_tokens` 512–1024 on the generate request.

Do not open LM Studio.app for Mailroom generate. Do not `lms server start`
as the generate path. Do not Ollama chat/generate.

Client library: `scripts/mailroom_generate.py`. Live CLI remains
`scripts/ask_mail.py`. Headless probes (C/D/E/F):
`scripts/ask_mail_generate_probes.py`.
