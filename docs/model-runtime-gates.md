# Model / runtime gates (early-error traps)

Run these **before Ready or merge** on any model or runtime (generate or
rerank). They exist so a green pull, a GGUF on disk, or a listening
port cannot be mistaken for a working scorer.

Human Terminal cards (one machine, one command per fence):
[ops-terminal.md](ops-terminal.md). ask_mail probe + neg smoke:
[ask_mail.md](ask_mail.md). Rerank lock (fail-open today):
[rerank.md](rerank.md).

## Traps

1. **Interface proof** — `curl` or a ≤5-line probe against the
   **official** path. Generate: LM Studio
   `POST /v1/chat/completions` with the **locked**
   `$MAILROOM_GENERATE_MODEL`. Expected PASS shape is in
   [ask_mail.md](ask_mail.md) and `ask_mail.py --probe`.
2. **Negative smoke** — garbage / stopped / wrong model / port closed /
   unreachable must **fail** or labeled-fail-open. A probe that
   “succeeds” on any model is a failed gate.
3. **Official path named** — LM Studio `/v1/chat/completions` for
   generate; last-token yes/no logits (or lock-C CrossEncoder on MPS)
   for rerank. **Community GGUF is insufficient** without trap 1 PASS.
   Ollama `/api/generate` and `/api/chat` are the **wrong** rerank
   interface (lock B not shipped).
4. **fail-open-only must be labeled** — every ask_mail response
   includes `generate_mode` and `rerank_mode`. Until CrossEncoder C,
   `rerank_mode` is `fail_open` or `none` only (RRF citations; scores
   not claimed). `hits_only` is explicit. Never silent.
5. **CoS withholds merge AR** without trap 1 PASS **or** an explicit
   **fail-open-only** label on the PR.

## This PR (lock A)

| Surface | Runtime | Ready? |
|---|---|---|
| Generate | **LM Studio** `/v1/chat/completions` (MBP + Mini) | Only after probe PASS on MBP with locked model id |
| Generate down | labeled `generate_mode=fail_open` → hits-only | fail-open-only |
| Mini generate fallback | **LM Studio** (same path). Not unnamed Ollama 9B/27B | hits-only if LM Studio is not running |
| Mini embed | Ollama `qwen3-embedding:8b` (official library tag) | embed only — not a scorer |
| Rerank | fail-open RRF (`rerank_mode=fail_open` or `none`) | **fail-open-only** |
| Rerank later | CrossEncoder on MPS (lock C, follow-up) | not this PR |

## Interface proof (generate)

```zsh
# MBP — LM Studio interface proof (locked model id)
curl -sS http://127.0.0.1:1234/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"'"$MAILROOM_GENERATE_MODEL"'","messages":[{"role":"user","content":"Reply with the single word pong."}],"max_tokens":8,"temperature":0}'
```

```zsh
# MBP — same probe via ask_mail (no mail bodies)
MAILROOM_GENERATE_MODEL="$MAILROOM_GENERATE_MODEL" \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --probe
```

Expected PASS shape (`ask_mail.py --probe` normalizes this):

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

Raw LM Studio `200` body must be `object=chat.completion` with a
non-empty `choices[0].message.content` and a `model` that matches the
locked id. Anything else is FAIL.

## Negative smoke (generate)

| Case | Expected `generate_mode` | Expected `generate_error` / stderr |
|---|---|---|
| LM Studio stopped | `fail_open` | `lm_studio_unreachable` ; `warning: generate fail-open: lm_studio_unreachable; hits-only` |
| Wrong model | `fail_open` | `wrong_model` |
| Port closed | `fail_open` | `port_closed` |
| Unreachable | `fail_open` | `lm_studio_unreachable` |
| Env unset, no `--llm` | `hits_only` | (none — not an error) |

Unit tests cover the labeled fallbacks with mocks. Live MBP matrix is
the operator gate.

## Definition of Done

- [ ] Interface proof PASS on MBP with locked `$MAILROOM_GENERATE_MODEL`
      (curl or `ask_mail.py --probe`).
- [ ] Negative smoke PASS (or documented live run) for stopped / wrong
      model / port closed / unreachable.
- [ ] Every `/ask` JSON has `generate_mode` and `rerank_mode`.
- [ ] Rerank is labeled **fail-open-only** (no Ready claim that Ollama
      generate/chat scores work).
- [ ] If interface proof is not PASS: the PR is labeled
      **fail-open-only** before merge. CoS withholds merge AR otherwise.
