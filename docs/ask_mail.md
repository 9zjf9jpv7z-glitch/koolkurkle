# ask_mail (PR-8)

CLI + loopback HTTP + MCP over `semantic_search.retrieve()`. Citations
follow RRF order until CrossEncoder C (scores not claimed). Mail bodies are
**DATA**. Drafts only — never send. `ask_audit` stores query + ids +
model + host, never bodies.

SoR: `$MAILROOM_DB` or `$HOME/MailArchive/mailroom.sqlite` (`Path.home()`).
No machine home hardcodes.

## Runtimes (named)

| Role | Runtime | Notes |
|---|---|---|
| Generate (MBP) | **LM Studio** `POST /v1/chat/completions` | `$MAILROOM_LM_STUDIO_URL` (default `http://127.0.0.1:1234`) + locked `$MAILROOM_GENERATE_MODEL` |
| Generate (Mini) | **LM Studio** (same path) | Not unnamed Ollama 9B/27B chat. If LM Studio is down → labeled `fail_open` / `hits_only` |
| Embed (Mini / MBP) | Ollama `qwen3-embedding:8b` | Official library tag. Not a reranker |
| Rerank today | fail-open RRF | `rerank_mode=fail_open` or `none`. Lock C (CrossEncoder on MPS) is follow-up |

Ollama `/api/generate` and `/api/chat` are **not** a working scorer.
See [rerank.md](rerank.md) and [model-runtime-gates.md](model-runtime-gates.md).

## Response schema (required labels)

Every ask / `/ask` / MCP `ask_mail` object includes:

- `generate_mode`: `lm_studio` | `hits_only` | `fail_open`
- `rerank_mode`: `fail_open` | `none` (fail-open-only until CrossEncoder C; scores not claimed)
- `generate_runtime`: `lm_studio` (always named)
- `citations`: `message_id` list in **RRF** order until CrossEncoder C (never invented)
- `generate_error`: neg-smoke label or null

`fail_open` / `none` / `hits_only` are explicit. Never silent.
`rerank_mode` is **fail-open-only until lock C**. Citations are RRF.
Unofficial `Hit.rerank` numbers do not reorder citations and are not
a Ready claim.

## Sequential smoke (retrieve, then generate)

Do **not** pin Ollama embed `qwen3-embedding:8b` and LM Studio chat
(35B-class / `$MAILROOM_GENERATE_MODEL`) in VRAM at the same time.
Smoke is two phases with an unload in between.

```zsh
# MBP — phase 1: retrieve only (embed 8b; rerank_mode=fail_open)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --phase retrieve --json 'SDGE bill'
```

```zsh
# MBP — unload Ollama embed before LM Studio generate
ollama stop qwen3-embedding:8b
```

```zsh
# MBP — phase 2: generate (LM Studio). --fts-only avoids reloading embed 8b
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
# Mini — unload Ollama embed before LM Studio generate
ollama stop qwen3-embedding:8b
```

`--no-generate` is the same as `--phase retrieve`. `--llm` is the same
as `--phase generate`. If LM Studio is down, `generate_mode=fail_open`.

## Interface proof (acceptance)

Positive LM Studio probe on **MBP** with the locked model id **before
Ready**. Gates: [model-runtime-gates.md](model-runtime-gates.md).

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
  "model": "<MAILROOM_GENERATE_MODEL>",
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
  "probe": "lm_studio_chat_completions",
  "runtime": "lm_studio",
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
| LM Studio stopped | `generate_mode=fail_open`, `generate_error=lm_studio_unreachable` | `warning: generate fail-open: lm_studio_unreachable; hits-only` |
| Wrong model | `generate_mode=fail_open`, `generate_error=wrong_model` | `warning: generate fail-open: wrong_model; hits-only` |
| Port closed | `generate_mode=fail_open`, `generate_error=port_closed` | `warning: generate fail-open: port_closed; hits-only` |
| Unreachable | `generate_mode=fail_open`, `generate_error=lm_studio_unreachable` | `warning: generate fail-open: lm_studio_unreachable; hits-only` |
| Env unset | `generate_mode=hits_only` | (none) |

Mocks in `tests/test_ask_mail.py`. Live MBP matrix is the operator gate.

## Definition of Done

- [ ] Interface proof PASS (curl or `--probe`) on MBP with locked model id.
- [ ] Negative smoke PASS (stopped / wrong model / port closed / unreachable).
- [ ] Schema labels present (`generate_mode`, `rerank_mode`).
- [ ] Rerank Ready language is **fail-open-only** (no Ollama-as-working-scorer).
- [ ] Else: explicit **fail-open-only** label before merge. CoS withholds
      merge AR without probe PASS or that label.

## CLI

```zsh
# MBP — ask (LM Studio generate when MAILROOM_GENERATE_MODEL is set)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
MAILROOM_GENERATE_MODEL="$MAILROOM_GENERATE_MODEL" \
MAILROOM_LM_STUDIO_URL=http://127.0.0.1:1234 \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --json 'SDGE bill'
```

```zsh
# Mini — ask. Generate runtime is LM Studio (same /v1/chat/completions).
# If LM Studio is not running: generate_mode=hits_only or fail_open (labeled).
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
