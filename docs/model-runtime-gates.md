# Model / runtime gates (early-error traps)

Run these **before Ready or merge** on any model or runtime (generate or
rerank). They exist so a green pull, a GGUF on disk, or a listening
port cannot be mistaken for a working scorer.

Human Terminal cards (one machine, one command per fence):
[ops-terminal.md](ops-terminal.md). ask_mail probe + neg smoke:
[ask_mail.md](ask_mail.md). Rerank (CrossEncoder default, fail-open
forever): [rerank.md](rerank.md).

## Traps

1. **Interface proof** — official path only. Generate: `mlx_lm.server`
   `POST /v1/chat/completions` with the **locked**
   `$MAILROOM_GENERATE_MODEL`. Path string `llmster-headless` is **not**
   the process. Rerank: CrossEncoder `predict` floats
   (or last-token yes/no logits). Expected generate PASS shape is in
   [ask_mail.md](ask_mail.md) and `ask_mail.py --probe`. Expected
   rerank proof: `scripts/rerank_smoke.py` (relevant pair outscores
   unrelated; CI may use a stub and label **fail-open-only**).
2. **Negative smoke** — garbage / stopped / wrong model / port closed /
   unreachable must **fail** or labeled-fail-open. A probe that
   “succeeds” on any model is a failed gate. Rerank comma-garbage
   (`0.12, 0.45, 0.03`) or a scalar sold as N scores must exit
   non-zero (`rerank_smoke.py` / `tests/test_rerank_shape_smoke.py`).
3. **Official path named** — `mlx_lm.server` `/v1/chat/completions` for
   generate; CrossEncoder on `Qwen/Qwen3-Reranker-0.6B` (MPS when
   available) for rerank. **Community GGUF is insufficient** without
   trap 1 PASS. Ollama `/api/generate` and `/api/chat` are the **wrong**
   rerank interface **and** the wrong generate interface — Ollama is
   embed-only.
4. **fail-open-only must be labeled** — every ask_mail response
   includes `generate_mode`, `rerank_mode`, `path`, and `fail_open`.
   `path=fail-open-only` when generate is down. `path=llmster-headless`
   is a client string, not a product/process claim.
5. **CoS withholds merge AR** without trap 1 PASS **or** an explicit
   **fail-open-only** label on the PR.

## This PR (lock C — CrossEncoder)

| Surface | Runtime | Ready? |
|---|---|---|
| Generate | **`mlx_lm.server`** `/v1/chat/completions` | Only after probe PASS on MBP with locked model id |
| Generate down | labeled `path=fail-open-only` / `generate_mode=fail_open` | fail-open-only |
| Mini generate | same OpenAI client. Not unnamed Ollama 9B/27B | hits-only if generate is not running |
| Mini embed | Ollama `qwen3-embedding:8b` (official library tag) | embed only — not a scorer |
| Rerank | CrossEncoder `Qwen/Qwen3-Reranker-0.6B` (`rerank_mode=crossencoder`) | Live floats after optional extra + weights |
| Rerank missing extra / predict fail | fail-open RRF (`rerank=None`, `rerank_mode=fail_open`) | **fail-open-only** |
| Rerank off | `--no-rerank` → `none`; `MAILROOM_RERANK_BACKEND=off` → `off` | intentional |

## Memory

Retrieve+rerank with the **embed** runtime resident. Unload the
CrossEncoder (`rerank_lib.unload_cross_encoder`) and/or short Ollama
`keep_alive` **before** `mlx_lm.server` 35B-class generate. Do not co-pin
embed + 35B + rerank.

## Interface proof (generate)

```zsh
# MBP — mlx_lm.server interface proof (locked model id)
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
  "probe": "generate_chat_completions",
  "runtime": "mlx_lm.server",
  "status": 200,
  "object": "chat.completion",
  "has_choices": true,
  "finish_reason": "stop",
  "error": null
}
```

Raw `200` body must be `object=chat.completion` with a
non-empty `choices[0].message.content` and a `model` that matches the
locked id. Anything else is FAIL. Process is `mlx_lm.server`.

## Interface proof (rerank)

```zsh
# MBP — CrossEncoder shape + stub/live predict (no mail bodies)
$HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/rerank_smoke.py
```

```zsh
# Mini — same CRM smoke
$HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/rerank_smoke.py
```

Needed signal: a finite float per pair from `CrossEncoder.predict`
(or a yes/no logit pair). A relevant subject+snippet must outscore an
unrelated one. CI without weights uses a stub and labels
**fail-open-only**; the real `score_documents_crossencoder` path is
still wired. Live weights: `MAILROOM_RERANK_SMOKE_LIVE=1` after
`pip install -r requirements-rerank.txt`.

Fixture: `tests/fixtures/rerank_interface_proof.json`.

## Negative smoke (generate)

| Case | Expected `generate_mode` | Expected `generate_error` / stderr |
|---|---|---|
| Generate listener stopped | `fail_open` | `lm_studio_unreachable` ; `path=fail-open-only` |
| Wrong model | `fail_open` | `wrong_model` |
| Port closed | `fail_open` | `port_closed` |
| Unreachable | `fail_open` | `lm_studio_unreachable` |
| Env unset, no `--llm` | `hits_only` | (none — not an error) |

Unit tests cover the labeled fallbacks with mocks. Live MBP matrix is
the operator gate.

## Negative smoke (rerank)

| Case | Expected |
|---|---|
| Ollama comma-garbage `"0.12, 0.45, 0.03"` | `RerankError` / smoke exit 2 — not working scores |
| Scalar sold as N scores | `RerankError` / smoke exit 2 |
| Missing sentence-transformers / torch | fail-open; `rerank_mode=fail_open`; stderr warning |
| `predict` exception | same fail-open |
| `--no-rerank` | `rerank_mode=none`; `rerank=None` |
| `MAILROOM_RERANK_BACKEND=off` | `rerank_mode=off`; `rerank=None` |

## Definition of Done

- [ ] Interface proof PASS on MBP with locked `$MAILROOM_GENERATE_MODEL`
      (curl or `ask_mail.py --probe`).
- [ ] Negative smoke PASS (or documented live run) for stopped / wrong
      model / port closed / unreachable.
- [ ] Every `/ask` JSON has `generate_mode` and `rerank_mode`.
- [ ] Rerank default is CrossEncoder. Ollama generate/chat is **not**
      claimed as a working scorer.
- [ ] `scripts/rerank_smoke.py` PASS. If live weights are not in CI:
      explicit **fail-open-only** label on the smoke JSON and PR.
- [ ] If generate interface proof is not PASS: the PR is labeled
      **fail-open-only** before merge. CoS withholds merge AR otherwise.
