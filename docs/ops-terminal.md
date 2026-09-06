# Human Terminal cards (Mac ops)

Checklist for Action-required cards a human pastes on a Mac. Keychain
names and Mini daily install:
[scripts/README.mailroom-daily.md](../scripts/README.mailroom-daily.md).
Ollama pull + Little Snitch: [rerank.md](rerank.md). Mini-only slim:
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

## Ollama + Little Snitch

If `ollama pull` fails with `dial tcp … connect: bad file descriptor`
while `curl` / `nc` to `registry.ollama.ai:443` succeed, allow
Ollama.app / `ollama` outbound to `registry.ollama.ai:443` in
Little Snitch, then retry the pull. Recipes: [rerank.md](rerank.md).
