# Rerank (MAILROOM §6.2 step 7) — fail-open RRF today

Hybrid `retrieve()` fuses FTS + sqlite-vec + RRF. `Hit.rerank` is the
optional score slot. **Today this repo is fail-open (lock A):** if a
scorer is missing, times out, or errors, RRF order stays, `rerank=None`,
and a warning is logged (no secrets, no mail bodies). `--no-rerank`
forces `rerank_mode=none`. ask_mail labels `rerank_mode` on every
response — never silent.

This is **not** Ready as a working reranker. Do not write Ready language
that rerank “works” under Ollama.

## Practice (why last-token yes/no logits)

Qwen3-Reranker-0.6B is a **classifier**, not a chat model. Official
scoring reads the last-token **yes / no logits** (softmax over that
pair) after the official instruct prompt. That interface is the one
that produces real scores.

## Why Ollama generate/chat is the wrong interface

Ollama `/api/generate` and `/api/chat` return sampled text. They do
**not** expose the last-token yes/no logit pair. A generate/logprobs
hotfix is **lock B — not in this PR**. `scripts/rerank_lib.py` still
talks to local Ollama the same way `embed_lib` does; callers
**fail-open** on error. That client is not acceptance for Ready.

## GGUF present ≠ scores

A community GGUF on disk (any quant, including
`dengcao/Qwen3-Reranker-0.6B:Q8_0` or `:F16`) only means weights are
present. The community port has no untagged `latest` — that is a
pull-tag fact, not a Ready claim. **Community GGUF is insufficient**
without the interface-proof PASS in
[model-runtime-gates.md](model-runtime-gates.md).

Do **not** treat `ollama pull` / `ollama cp` as a working scorer.
Those install fences are removed on purpose.

## Today: fail-open RRF

`rerank_mode=fail_open` (default) or `none` (`--no-rerank`). Citations
in ask_mail follow Hit order, which is RRF when `Hit.rerank` is null.

Mini generate/fallback runtime for **answers** is **LM Studio**
(`/v1/chat/completions`), not unnamed Ollama 9B/27B chat. Mini Ollama
stays the **embed** runtime (`qwen3-embedding:8b`) only. See
[ask_mail.md](ask_mail.md).

## Later: CrossEncoder on MPS (lock C)

A local CrossEncoder on Apple Silicon MPS is the planned working
scorer. Not in this PR.

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

## SoR + retrieve smoke (fail-open RRF)

SoR path is `$MAILROOM_DB` or `$HOME/MailArchive/mailroom.sqlite`
(`Path.home()` / expanduser — no machine home hardcodes). Apple
`/usr/bin/python3` cannot load sqlite-vec; use the MailArchive venv.

These recipes are **fail-open RRF smoke**, not a working-scorer demo.

```zsh
# MBP — hybrid retrieve (fail-open RRF; rerank_mode=fail_open unless scores land)
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/semantic_search.py 'SDGE bill'
```

```zsh
# MBP — hybrid retrieve JSON
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
# Mini — hybrid retrieve (fail-open RRF). Copy DB is OK; not a second writer.
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

Optional timeout: `MAILROOM_RERANK_TIMEOUT` (seconds, default 20).
