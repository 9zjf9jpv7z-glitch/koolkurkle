# Rerank (MAILROOM §6.2 step 7) — CrossEncoder

Hybrid `retrieve()` fuses FTS + sqlite-vec + RRF, then scores the **fused
top-20** short subject+snippet pairs with an in-process CrossEncoder.
Scores land on `Hit.rerank` and sort descending when present.

**Preferred practice:** `sentence_transformers.CrossEncoder("Qwen/Qwen3-Reranker-0.6B")`
on Apple Silicon MPS when available (CPU is fine in CI/cloud). That API
returns the needed signal: `predict` floats, or yes/no logits. Optional
extra: `requirements-rerank.txt` (`sentence-transformers`). Slim installs
omit torch and **fail-open**.

If torch, weights, or `predict` is missing/fails: keep RRF order, set
`rerank=None`, log a stderr warning (no secrets, no mail bodies), and
label `rerank_mode=fail_open`. `--no-rerank` forces `rerank_mode=none`.
`MAILROOM_RERANK_BACKEND=off` forces `rerank_mode=off`. ask_mail labels
`rerank_mode` on every response — never silent.

This is fail-open **forever**. A missing extra is not a crash.

## Why CrossEncoder (not Ollama generate/chat)

Qwen3-Reranker-0.6B is a **classifier**, not a chat model. Official
scoring reads last-token **yes / no logits** (or the CrossEncoder
`predict` wrap of that pair) after the instruct prompt. Ollama
`/api/generate` and `/api/chat` return sampled text. They are the
**wrong interface**: they do **not** expose that logit pair. Comma-separated generate leftovers are garbage,
not scores. **Ollama cannot score Qwen3-Reranker.** CrossEncoder is the
path. A generate/logprobs hotfix is not acceptance.

## GGUF present ≠ scores

A community GGUF on disk (any quant, including
`dengcao/Qwen3-Reranker-0.6B:Q8_0` or `:F16`) only means weights are
present. The community port has no untagged `latest` — that is a
pull-tag fact, not a Ready claim. **Community GGUF is insufficient**
without the CrossEncoder interface-proof PASS in
[model-runtime-gates.md](model-runtime-gates.md).

Do **not** treat `ollama pull` / `ollama cp` as a working scorer.
Those install fences stay removed on purpose.

## Env

| Variable | Default | Notes |
|---|---|---|
| `MAILROOM_RERANK_BACKEND` | `crossencoder` | `crossencoder` \| `ollama` \| `off` |
| `MAILROOM_RERANK_MODEL` | `Qwen/Qwen3-Reranker-0.6B` | HF id. Alt: `tomaarsen/Qwen3-Reranker-0.6B-seq-cls` |
| `MAILROOM_RERANK_INSTRUCTION` | English email instruct below | Override OK |
| `MAILROOM_RERANK_DEVICE` | MPS if available, else CPU | |
| `MAILROOM_RERANK_TIMEOUT` | 20 | Opt-in Ollama client only |

Default instruction:

`Given an email search query, retrieve relevant email messages or passages that answer the query`

`MAILROOM_RERANK_BACKEND=ollama` is opt-in only and still fail-opens.
It is not a Qwen3 scorer.

## Memory (retrieve + rerank, then generate)

Keep the **embed** runtime resident during retrieve+rerank. Unload the
in-process CrossEncoder (`rerank_lib.unload_cross_encoder`) and/or use
a short Ollama `keep_alive` **before** LM Studio 35B-class generate.
Do **not** co-pin embed + 35B + rerank. Sequential smoke:
[ask_mail.md](ask_mail.md). Gates:
[model-runtime-gates.md](model-runtime-gates.md).

Mini generate/fallback runtime for **answers** is **LM Studio**
(`/v1/chat/completions`), not unnamed Ollama 9B/27B chat. Mini Ollama
stays the **embed** runtime (`qwen3-embedding:8b`) only.

## Early-error traps

Before Ready or merge on any model/runtime, run the gates in
[model-runtime-gates.md](model-runtime-gates.md). Human Terminal cards:
[ops-terminal.md](ops-terminal.md). CoS withholds merge AR without
probe PASS **or** an explicit **fail-open-only** label.

If `ollama pull` of the **official embed** tag fails with
`dial tcp … connect: bad file descriptor` while `curl` / `nc` to
`registry.ollama.ai:443` succeed, allow Ollama.app / `ollama` outbound
to `registry.ollama.ai:443` in Little Snitch. A successful embed pull
is still not a rerank Ready.

## SoR + retrieve smoke

SoR path is `$MAILROOM_DB` or `$HOME/MailArchive/mailroom.sqlite`
(`Path.home()` / expanduser — no machine home hardcodes). Apple
`/usr/bin/python3` cannot load sqlite-vec; use the MailArchive venv.

```zsh
# MBP — optional CrossEncoder extra (not a slim-install requirement)
$HOME/MailArchive/.venv/bin/pip install -r $HOME/MailArchive/requirements-rerank.txt
```

```zsh
# Mini — optional CrossEncoder extra (not a slim-install requirement)
$HOME/MailArchive/.venv/bin/pip install -r $HOME/MailArchive/requirements-rerank.txt
```

```zsh
# MBP — CRM shape + interface smoke (CI-safe; fail-open-only without weights)
$HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/rerank_smoke.py
```

```zsh
# Mini — CRM shape + interface smoke (copy DB is not required)
$HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/rerank_smoke.py
```

```zsh
# MBP — hybrid retrieve (CrossEncoder when extra is installed; else fail-open)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/semantic_search.py 'SDGE bill'
```

```zsh
# MBP — hybrid retrieve JSON (rerank_mode on stderr)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/semantic_search.py --json --k 20 'Caddell'
```

```zsh
# MBP — hybrid retrieve (horse)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/semantic_search.py 'horse'
```

```zsh
# MBP — force rerank_mode=none
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/semantic_search.py --no-rerank 'SDGE bill'
```

```zsh
# Mini — hybrid retrieve. Copy DB is OK; not a second writer.
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/semantic_search.py 'SDGE bill'
```

```zsh
# Mini — hybrid retrieve JSON
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/semantic_search.py --json --k 20 'Caddell'
```

```zsh
# Mini — hybrid retrieve (horse)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/semantic_search.py 'horse'
```

```zsh
# Mini — force rerank_mode=none
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/semantic_search.py --no-rerank 'SDGE bill'
```
