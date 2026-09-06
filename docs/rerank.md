# Qwen3-Reranker-0.6B (MAILROOM §6.2 step 7)

Hybrid `retrieve()` fuses FTS + sqlite-vec + RRF, then scores the **fused
top-20** with a local reranker. Scores land on `Hit.rerank`. If the model is
missing, times out, or errors, retrieve **fail-opens**: RRF order stays,
`rerank=None`, and a warning is logged (no secrets, no mail bodies).

Default tag: `qwen3-reranker:0.6b` (`$MAILROOM_RERANK_MODEL` overrides).
Ollama has no official library reranker yet; pull the community port of
`Qwen/Qwen3-Reranker-0.6B` and alias it. That port has no untagged
`latest` — pull an explicit quant (`:Q8_0` preferred; `:F16` is also
fine).

**Pull + alias** (one command per fence; set the banner to the machine):

```zsh
# Mini — pull reranker (explicit quant; untagged has no latest)
ollama pull dengcao/Qwen3-Reranker-0.6B:Q8_0
```

```zsh
# Mini — alias for MAILROOM default tag
ollama cp dengcao/Qwen3-Reranker-0.6B:Q8_0 qwen3-reranker:0.6b
```

```zsh
# MBP — pull reranker (explicit quant; untagged has no latest)
ollama pull dengcao/Qwen3-Reranker-0.6B:Q8_0
```

```zsh
# MBP — alias for MAILROOM default tag
ollama cp dengcao/Qwen3-Reranker-0.6B:Q8_0 qwen3-reranker:0.6b
```

Or set `MAILROOM_RERANK_MODEL=dengcao/Qwen3-Reranker-0.6B:Q8_0` and skip
the `cp`.

If `ollama pull` fails with `dial tcp … connect: bad file descriptor`
while `curl` / `nc` to `registry.ollama.ai:443` succeed, allow
Ollama.app / `ollama` outbound to `registry.ollama.ai:443` in
Little Snitch, then retry the pull. Human Terminal cards:
[ops-terminal.md](ops-terminal.md).

SoR path is `$MAILROOM_DB` or `$HOME/MailArchive/mailroom.sqlite`
(`Path.home()` / expanduser — no machine home hardcodes). Apple
`/usr/bin/python3` cannot load sqlite-vec; use the MailArchive venv.

`--no-rerank` forces the stub (RRF only) for smoke/debug.

## MBP

```zsh
# MBP — pull reranker (explicit quant; untagged has no latest)
ollama pull dengcao/Qwen3-Reranker-0.6B:Q8_0
```

```zsh
# MBP — alias for MAILROOM default tag
ollama cp dengcao/Qwen3-Reranker-0.6B:Q8_0 qwen3-reranker:0.6b
```

```zsh
# MBP — hybrid retrieve with rerank (default)
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
# MBP — smoke/debug without rerank
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/semantic_search.py --no-rerank 'SDGE bill'
```

## Mini

```zsh
# Mini — pull reranker (explicit quant; untagged has no latest)
ollama pull dengcao/Qwen3-Reranker-0.6B:Q8_0
```

```zsh
# Mini — alias for MAILROOM default tag
ollama cp dengcao/Qwen3-Reranker-0.6B:Q8_0 qwen3-reranker:0.6b
```

```zsh
# Mini — hybrid retrieve with rerank (default). Copy DB is OK; not a second writer.
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
# Mini — smoke/debug without rerank
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  $HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/semantic_search.py --no-rerank 'SDGE bill'
```

Optional timeout: `MAILROOM_RERANK_TIMEOUT` (seconds, default 20).
