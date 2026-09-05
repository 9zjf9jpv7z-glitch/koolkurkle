# PR-0 schema note — mac-mini.local

As-of: 2026-09-05 ~2:52–2:55 PM PT

`~/MailArchive/mailroom.sqlite` is **0B** (empty). Live worker DB is `mailroom-copy` (~675MB): `journal_mode=wal`, `mmap_size=0`.

Dims histogram: `1024|54483`. Live embed `--max-chars 3000`.

LaunchAgents: `com.mailroom.daily` installed, plus embed-shard1 / rem3 / max4000 / cull / handoff.

Use Mini `~/MailArchive/.venv/bin/python` for sqlite-vec (Apple `/usr/bin/python3` cannot load the extension).
