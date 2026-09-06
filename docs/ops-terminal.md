# Human Terminal cards (Mac ops)

Checklist for Action-required cards a human pastes on a Mac. Keychain
names and Mini daily install:
[scripts/README.mailroom-daily.md](../scripts/README.mailroom-daily.md).
Model/runtime gates (interface proof, neg smoke, fail-open-only):
[model-runtime-gates.md](model-runtime-gates.md). Rerank is fail-open
RRF today — not an Ollama working scorer: [rerank.md](rerank.md).
ask_mail probe: [ask_mail.md](ask_mail.md). Mini-only slim:
[macos-slim/README.md](../macos-slim/README.md).

These cards are chat/operator steps. They are not the writer-lock file
`~/MailArchive/ACTION_REQUIRED` (see
[pr0/with_writer_lock_DESIGN.md](pr0/with_writer_lock_DESIGN.md)).

## One card at a time

Work one Action-required card at a time. A single card may list several
steps for **one** machine.

## One machine per card

- One host per card. A **MBP** or **Mini** banner on the first line
  keeps the paste target obvious.
- Open a new card when switching machines. Keychain items and `$HOME`
  paths then stay on the host they belong to.

## One command per fence

Put each Terminal command in its own fenced code block. Chat copy
buttons paste the whole fence; one command per fence keeps a single
line on the clipboard. Two `security` (or other) lines in one fence
become one paste.

```zsh
# Mini — example banner (first line of the card)
hostname
```

```zsh
# MBP — example banner (new card after switching machines)
hostname
```

## Keychain create

Preferred service name: `mailroom.imap.app-password`. Keep `-w` last so
the secret is typed only at the interactive prompt.

```zsh
# Mini — create IMAP Keychain item (type the secret at the prompt)
security add-generic-password -a "$USER" -s mailroom.imap.app-password -w
```

```zsh
# MBP — create IMAP Keychain item (type the secret at the prompt)
security add-generic-password -a "$USER" -s mailroom.imap.app-password -w
```

Verify **length only**. Never print or paste the secret into chat or git.

```zsh
# Mini — Keychain length check (no secret on stdout)
security find-generic-password -s mailroom.imap.app-password -w | wc -c
```

```zsh
# MBP — Keychain length check (no secret on stdout)
security find-generic-password -s mailroom.imap.app-password -w | wc -c
```

Apple app-specific passwords are typically ~16–19 characters.
`security -w` may add a trailing newline, so `wc -c` can read one
higher. An ~8-character secret will not authenticate to IMAP
(Login denied); regenerate at appleid.apple.com.

Prefer the new name. Leave legacy `mailroom.icloud.app-password` in
place until IMAP smoke PASSes on the new name. The daily wrapper still
falls back to the legacy item when the default name is missing or empty.

## Privacy on GitHub

Zero personal identifiers in this repo or on GitHub — code, tests, PR
bodies, and comments. Generics only:

- `EXAMPLE_USER_LOCAL`
- `example.invalid`
- `$HOME` / `__HOME__`
- `USERNAME`

## GitHub PR description

To change a PR description, edit the first Conversation comment
(⋯ → Edit). The title pencil edits the title only.

## Early-error traps (model / runtime)

Before Ready or merge on generate or rerank, run the gates in
[model-runtime-gates.md](model-runtime-gates.md). CoS withholds merge
AR without interface-proof PASS **or** an explicit **fail-open-only**
label.

1. **Interface proof** — `curl` or `ask_mail.py --probe` against the
   official path (LM Studio `/v1/chat/completions`, locked model id).
2. **Negative smoke** — garbage / stopped / wrong model / port closed /
   unreachable must fail or labeled-fail-open.
3. **Official path named** — community GGUF
   (`dengcao/Qwen3-Reranker-0.6B:Q8_0` or `:F16`; no untagged `latest`)
   is insufficient without trap 1 PASS. Ollama generate/chat is the
   wrong rerank interface.
4. **fail-open-only must be labeled** — `generate_mode` + `rerank_mode`
   on every ask_mail response.
5. **CoS withholds merge AR** without PASS or that label.

```zsh
# MBP — LM Studio interface proof (locked model id)
$HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --probe
```

Do **not** paste `ollama pull` / `ollama cp` of the community reranker
as a working-scorer card. GGUF present ≠ scores. Rerank today is
fail-open RRF ([rerank.md](rerank.md)).

## Ollama embed + Little Snitch

Official **embed** pull is `qwen3-embedding:8b` (not a reranker). If
`ollama pull` fails with `dial tcp … connect: bad file descriptor`
while `curl` / `nc` to `registry.ollama.ai:443` succeed, allow
Ollama.app / `ollama` outbound to `registry.ollama.ai:443` in
Little Snitch, then retry the embed pull.

```zsh
# Mini — official embed pull (not a reranker)
ollama pull qwen3-embedding:8b
```

```zsh
# MBP — official embed pull (not a reranker)
ollama pull qwen3-embedding:8b
```

## ask_mail sequential smoke (do not pin embed + chat)

Retrieve first (Ollama embed `qwen3-embedding:8b`), then **unload**,
then generate (LM Studio / `$MAILROOM_GENERATE_MODEL`). Do not pin
embed 8b and LM Studio 35B-class chat in VRAM together. Recipes:
[ask_mail.md](ask_mail.md).

```zsh
# MBP — ask_mail phase 1 retrieve (embed only)
$HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/ask_mail.py --phase retrieve --json 'SDGE bill'
```

```zsh
# MBP — unload embed before LM Studio generate
ollama stop qwen3-embedding:8b
```

```zsh
# Mini — unload embed before LM Studio generate
ollama stop qwen3-embedding:8b
```

## Heavy packets

Canonical on the Mac Desktop: `$HOME/Desktop/Heavy-Bot/to-bot`.
Before a box / cloud agent reads a packet, sync that directory into
`/workspace`. A Desktop file that was never synced is not visible to
the box. Do not `git add` packet contents.

```zsh
# MBP — canonical Heavy packets (Desktop)
ls "$HOME/Desktop/Heavy-Bot/to-bot"
```

```zsh
# MBP — sync Heavy packets into /workspace before box read
rsync -a "$HOME/Desktop/Heavy-Bot/to-bot/" /workspace/
```
