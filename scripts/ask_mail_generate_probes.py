#!/usr/bin/env python3
"""Headless generate probes (C/D/E/F). Not the SoR live CLI.

SoR live CLI remains scripts/ask_mail.py. This module is probes only.
Path strings: llmster-headless | fail-open-only. Process: mlx_lm.server.
Does not open LM Studio.app. Does not call Ollama generate.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

import mailroom_generate as mg

PATH_LABELS = (mg.PATH_LLMSTER, mg.PATH_FAIL_OPEN)


def _load_hits(path: str) -> list[Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "hits" in data:
        hits = data["hits"]
        return hits if isinstance(hits, list) else [hits]
    if isinstance(data, list):
        return data
    return [data]


def _load_callable(spec: str) -> Callable[..., Any]:
    if ":" not in spec:
        raise SystemExit(f"hook must be module:attr, got {spec!r}")
    mod_name, attr = spec.rsplit(":", 1)
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, attr)
    if not callable(fn):
        raise SystemExit(f"hook not callable: {spec}")
    return fn


def _hooks(args: argparse.Namespace) -> tuple[Callable[[str], Any], Callable[[str, Any], Any]]:
    if args.hits_json:
        hits = _load_hits(args.hits_json)

        def retrieve(_q: str) -> list[Any]:
            return hits

        def rerank(_q: str, raw: Any) -> Any:
            return raw

        return retrieve, rerank
    if args.retrieve_module and args.rerank_module:
        return _load_callable(args.retrieve_module), _load_callable(args.rerank_module)
    raise SystemExit("need --hits-json (cmd 10 CrossEncoder dump) or --retrieve-module and --rerank-module")


def _print(result: dict[str, Any]) -> int:
    print(json.dumps(result, default=str, ensure_ascii=False, indent=2))
    path = result.get("path")
    if path not in PATH_LABELS:
        return 2
    if result.get("fail_open") and path != mg.PATH_FAIL_OPEN:
        return 2
    return 0 if path == mg.PATH_LLMSTER else 1


def cmd_live(args: argparse.Namespace) -> int:
    retrieve, rerank = _hooks(args)
    result = mg.ask_mail_live(
        args.query,
        retrieve=retrieve,
        rerank=rerank,
        embed_model=args.embed_model,
    )
    return _print(result)


def cmd_probe_c(_args: argparse.Namespace) -> int:
    try:
        ids = mg.loaded_model_ids()
        down = False
        reason = ""
    except mg.GenerateDown as exc:
        ids = []
        down = True
        reason = str(exc)
    want = mg.generate_model_id()
    loaded = want in ids
    catalog_leak = (not down) and (len(ids) > 0) and (not loaded)
    payload = {
        "probe": "C",
        "generate_down": down,
        "reason": reason,
        "ids": ids,
        "want": want,
        "loaded_identifier": loaded,
        "jit_catalog_leak": catalog_leak,
        "justInTimeModelLoading_must_be": False,
        "pass_pre_load": (not down) and (ids == []),
        "pass_post_load": (not down) and loaded and (not catalog_leak),
        "path_claim_allowed": False,
    }
    print(json.dumps(payload, indent=2))
    if down:
        return 1
    return 0


def cmd_probe_d(_args: argparse.Namespace) -> int:
    if not mg.generate_is_loaded():
        print(json.dumps({"probe": "D", "pass": False, "reason": "identifier not loaded; no POST"}))
        return 1
    payload = mg.build_chat_payload(
        [{"role": "user", "content": "Reply with the single word pong."}],
        max_tokens_value=512,
    )
    try:
        text = mg.post_chat(payload)
    except mg.GenerateDown as exc:
        print(json.dumps({"probe": "D", "pass": False, "reason": str(exc)}))
        return 1
    ok = bool(text)
    print(json.dumps({"probe": "D", "pass": ok, "content": text[:200], "max_tokens": payload["max_tokens"]}))
    return 0 if ok else 1


def cmd_probe_f(args: argparse.Namespace) -> int:
    retrieve, rerank = _hooks(args)
    result = mg.ask_mail_live(
        args.query,
        retrieve=retrieve,
        rerank=rerank,
        embed_model=args.embed_model,
    )
    labeled = result.get("path") == mg.PATH_FAIL_OPEN and result.get("fail_open") is True
    has_hits = bool(result.get("hits"))
    no_answer = result.get("answer") is None
    payload = {
        "probe": "F",
        "pass": labeled and no_answer and has_hits,
        "path": result.get("path"),
        "fail_open": result.get("fail_open"),
        "answer": result.get("answer"),
        "hit_count": len(result.get("hits") or []),
        "reason": result.get("reason"),
        "label_required": mg.PATH_FAIL_OPEN,
    }
    print(json.dumps(payload, default=str, indent=2))
    return 0 if payload["pass"] else 1


def cmd_jit_off(_args: argparse.Namespace) -> int:
    """Flip existing justInTimeModelLoading keys to false. Do not invent GUI labels."""
    roots = [
        Path.home() / ".lmstudio",
        Path.home() / "Library" / "Application Support" / "LM Studio",
    ]
    hits: list[str] = []
    changed: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".json", ".json5"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "justInTimeModelLoading" not in text:
                continue
            hits.append(str(path))
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if _set_jit_false(data):
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                changed.append(str(path))
    print(json.dumps({"key": "justInTimeModelLoading", "found": hits, "set_false": changed}, indent=2))
    if not hits:
        print("no justInTimeModelLoading key found; probe C remains the gate", file=sys.stderr)
        return 0
    return 0


def _set_jit_false(obj: Any) -> bool:
    changed = False
    if isinstance(obj, dict):
        if "justInTimeModelLoading" in obj and obj["justInTimeModelLoading"] is not False:
            obj["justInTimeModelLoading"] = False
            changed = True
        for value in obj.values():
            if _set_jit_false(value):
                changed = True
    elif isinstance(obj, list):
        for item in obj:
            if _set_jit_false(item):
                changed = True
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Mailroom ask_mail live generate (headless)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    live = sub.add_parser("live", help="probe E: retrieve+rerank hits then generate or fail-open")
    live.add_argument("--query", required=True)
    live.add_argument("--hits-json")
    live.add_argument("--retrieve-module")
    live.add_argument("--rerank-module")
    live.add_argument("--embed-model", default=None)
    live.set_defaults(func=cmd_live)

    p_c = sub.add_parser("probe-c", help="GET /v1/models means loaded")
    p_c.set_defaults(func=cmd_probe_c)

    p_d = sub.add_parser("probe-d", help="POST /v1/chat/completions max_tokens + thinking off")
    p_d.set_defaults(func=cmd_probe_d)

    p_f = sub.add_parser("probe-f", help="negative smoke: generate down → fail-open-only")
    p_f.add_argument("--query", required=True)
    p_f.add_argument("--hits-json")
    p_f.add_argument("--retrieve-module")
    p_f.add_argument("--rerank-module")
    p_f.add_argument("--embed-model", default=None)
    p_f.set_defaults(func=cmd_probe_f)

    jit = sub.add_parser("jit-off", help="set existing justInTimeModelLoading keys false")
    jit.set_defaults(func=cmd_jit_off)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
