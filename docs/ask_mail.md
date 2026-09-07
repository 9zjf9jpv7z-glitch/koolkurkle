# ask_mail (PR-8)

CLI + loopback HTTP + MCP over `semantic_search.retrieve()`. Citations
follow live `Hit.rerank` descending when CrossEncoder scores are
present; otherwise RRF (fail-open; scores not claimed). Mail bodies are
**DATA**. Drafts only — never send. `ask_audit` stores query + ids +
model + host, never bodies.

SoR: `$MAILROOM_DB` or `$HOME/MailArchive/mailroom.sqlite` (`Path.home()`).
No machine home hardcodes.

## Runtimes (named)

| Role | Runtime | Notes |
|---|---|---|
| Generate (preferred process) | **`mlx_lm.server`** `POST /v1/chat/completions` | `127.0.0.1:1234`. `$MAILROOM_LM_STUDIO_URL` / `$MAILROOM_GENERATE_URL` + locked `$MAILROOM_GENERATE_MODEL` |
| Generate (client path string) | `llmster-headless` | Code/JSON `path` only. **Not** the process. Withhold product-name claim |
| Generate down | labeled `fail-open-only` | `generate_mode=fail_open`, `fail_open=true`, hits-only. Never silent |
| Generate (Mini) | same OpenAI client | Not unnamed Ollama 9B/27B chat. If generate is down → labeled `fail_open` / `hits_only` |
| Embed (Mini / MBP) | Ollama `qwen3-embedding:8b` | Official library tag. Embed only — never generate |
| Rerank | CrossEncoder `Qwen/Qwen3-Reranker-0.6B` (optional extra) | `rerank_mode=crossencoder` when live floats land; `fail_open` / `none` / `off` otherwise. Ollama cannot score Qwen3-Reranker |

Ollama `/api/generate` and `/api/chat` are **not** a working scorer and
**not** generate. See [rerank.md](rerank.md),
[model-runtime-gates.md](model-runtime-gates.md),
[generate-mlx.md](generate-mlx.md).

## Response schema (required labels)

Every ask / `/ask` / MCP `ask_mail` object includes:

- `generate_mode`: `lm_studio` \| `hits_only` \| `fail_open` (legacy success enum `lm_studio` means OpenAI-compatible `:1234` success — **process** is `mlx_lm.server`)
- `generate_runtime` / `generate_process`: `mlx_lm.server`
- `path`: `llmster-headless` (success) \| `fail-open-only` (generate down) \| `null` (`hits_only`)
- `fail_open`: boolean
- `rerank_mode`: `crossencoder` \| `fail_open` \| `none` \| `off`
- `citations`: `message_id` list in **rerank** order when live floats
  are present, else **RRF** (never invented)
- `generate_error`: neg-smoke label or null (`lm_studio_unreachable` is a **label**, not the process)

`fail_open` / `none` / `off` / `hits_only` are explicit. Never silent.
When CrossEncoder cannot run, `rerank_mode=fail_open` and citations are
RRF (scores not claimed). Ollama generate/chat is not a working scorer.

## Sequential smoke (retrieve, then generate)

Do **not** pin Ollama embed `qwen3-embedding:8b`, CrossEncoder, and
35B-class generate (`$MAILROOM_GENERATE_MODEL`) in RAM at the
same time. Retrieve+rerank may keep embed resident. Unload the
CrossEncoder and embed **before** `mlx_lm.server` generate.

```zsh
# MBP — phase 1: retrieve + rerank (embed resident; CrossEncoder in-process)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --phase retrieve --json 'SDGE bill'
```

```zsh
# MBP — unload Ollama embed before mlx_lm.server generate
ollama stop qwen3-embedding:8b
```

```zsh
# MBP — phase 2: generate (mlx_lm.server). --fts-only avoids reloading embed 8b
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
MAILROOM_GENERATE_MODEL="$MAILROOM_GENERATE_MODEL" \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --phase generate --fts-only --json 'SDGE bill'
```

```zsh
# Mini — phase 1: retrieve only (same sequential rule)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --phase retrieve --json 'SDGE bill'
```

```zsh
# Mini — unload Ollama embed before generate
ollama stop qwen3-embedding:8b
```

`--no-generate` is the same as `--phase retrieve`. `--llm` is the same
as `--phase generate`. If `mlx_lm.server` is down, `generate_mode=fail_open`
and `path=fail-open-only`.

## Interface proof (acceptance)

Positive generate probe on **MBP** with the locked model id **before
Ready**. Process is `mlx_lm.server`. Gates: [model-runtime-gates.md](model-runtime-gates.md).

```zsh
# MBP — interface proof curl (locked model id)
curl -sS http://127.0.0.1:1234/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"'"$MAILROOM_GENERATE_MODEL"'","messages":[{"role":"user","content":"Reply with the single word pong."}],"max_tokens":8,"temperature":0}'
```

Expected raw PASS shape:

```json
{
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "model": "$MAILROOM_GENERATE_MODEL",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "pong"},
      "finish_reason": "stop"
    }
  ]
}
```

```zsh
# MBP — interface proof via ask_mail
MAILROOM_GENERATE_MODEL="$MAILROOM_GENERATE_MODEL" \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --probe
```

Expected `--probe` PASS JSON:

```json
{
  "ok": true,
  "probe": "generate_chat_completions",
  "runtime": "mlx_lm.server",
  "status": 200,
  "object": "chat.completion",
  "has_choices": true,
  "finish_reason": "stop",
  "error": null
}
```

## Negative smoke

| Case | JSON | stderr |
|---|---|---|
| Generate listener stopped | `generate_mode=fail_open`, `path=fail-open-only`, `generate_error=lm_studio_unreachable` | `warning: generate fail-open: lm_studio_unreachable; hits-only` |
| Wrong model | `generate_mode=fail_open`, `generate_error=wrong_model` | `warning: generate fail-open: wrong_model; hits-only` |
| Port closed | `generate_mode=fail_open`, `generate_error=port_closed` | `warning: generate fail-open: port_closed; hits-only` |
| Unreachable | `generate_mode=fail_open`, `generate_error=lm_studio_unreachable` | `warning: generate fail-open: lm_studio_unreachable; hits-only` |
| Env unset | `generate_mode=hits_only` | (none) |

Mocks in `tests/test_ask_mail.py`. Live MBP matrix is the operator gate.

## Definition of Done

- [ ] Interface proof PASS (curl or `--probe`) on MBP with locked model id.
- [ ] Negative smoke PASS (stopped / wrong model / port closed / unreachable).
- [ ] Schema labels present (`generate_mode`, `rerank_mode`, `path`).
- [ ] Rerank default is CrossEncoder. Fail-open is labeled when scores
      cannot run. No Ollama-as-working-scorer.
- [ ] Else: explicit **fail-open-only** label before merge. CoS withholds
      merge AR without probe PASS or that label.

## CLI

```zsh
# MBP — ask (mlx_lm.server when MAILROOM_GENERATE_MODEL is set)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
MAILROOM_GENERATE_MODEL="$MAILROOM_GENERATE_MODEL" \
MAILROOM_LM_STUDIO_URL=http://127.0.0.1:1234 \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --json 'SDGE bill'
```

```zsh
# Mini — ask. Generate process is mlx_lm.server (same /v1/chat/completions).
# If generate is not running: generate_mode=hits_only or fail_open (labeled).
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --json 'SDGE bill'
```

```zsh
# MBP — HTTP loopback (8743; 8744 if bound)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --serve
```

```zsh
# MBP — POST /ask
curl -sS http://127.0.0.1:8743/ask \
  -H 'Content-Type: application/json' \
  -d '{"query":"SDGE bill","k":8}'
```

```zsh
# Mini — MCP stdio (ask_mail, hybrid_search, get_thread, draft_reply)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --mcp
```

`draft_reply` writes `drafts` + a file under `$HOME/MailArchive/drafts`.
`send=true` is refused. No IMAP.

## Injection

Mail between `BEGIN_UNTRUSTED_MAIL_DATA` and `END_UNTRUSTED_MAIL_DATA`
is DATA. The model is told to ignore instructions inside DATA. Citations
are the retrieve Hit ids only — the model cannot invent `message_id`s
on the response `citations` list.

## Audit

`ask_audit` columns: `query`, `hit_ids`, plus `detail` JSON
`{model, host, generate_mode, rerank_mode, runtime}`. Never bodies,
snippets, or subjects.

Human Terminal cards: [ops-terminal.md](ops-terminal.md).
