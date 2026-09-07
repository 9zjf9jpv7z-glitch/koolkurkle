"""Mailroom ask_mail live generate client.

Headless OpenAI-compatible generate only. Ollama is never used to generate.
Fail-open is hits-only and must be labeled fail-open-only.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

GENERATE_URL_DEFAULT = "http://127.0.0.1:1234/v1/chat/completions"
GENERATE_MODELS_URL_DEFAULT = "http://127.0.0.1:1234/v1/models"
GENERATE_IDENTIFIER = "mailroom-generate"
PATH_LLMSTER = "llmster-headless"
PATH_FAIL_OPEN = "fail-open-only"
MAX_TOKENS_MIN = 512
MAX_TOKENS_MAX = 1024
MAX_TOKENS_DEFAULT = 768
HTTP_TIMEOUT_S = 120.0
READY_TIMEOUT_S = 5.0

# Thinking-off fields. Unknown keys are dropped on a 400 retry.
THINKING_OFF_FIELDS: dict[str, Any] = {
    "enable_thinking": False,
    "think": False,
    "reasoning": {"enabled": False, "effort": "none"},
}


class GenerateDown(Exception):
    """Generate endpoint is not usable. Caller must fail-open."""


def clamp_max_tokens(value: int | str | None) -> int:
    if value is None or value == "":
        n = MAX_TOKENS_DEFAULT
    else:
        n = int(value)
    n = max(MAX_TOKENS_MIN, n)
    n = min(MAX_TOKENS_MAX, n)
    return n


def generate_url() -> str:
    return os.environ.get("MAILROOM_GENERATE_URL", GENERATE_URL_DEFAULT).rstrip("/")


def generate_models_url() -> str:
    explicit = os.environ.get("MAILROOM_GENERATE_MODELS_URL")
    if explicit:
        return explicit.rstrip("/")
    url = generate_url()
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")] + "/models"
    if url.endswith("/v1"):
        return url + "/models"
    return GENERATE_MODELS_URL_DEFAULT


def generate_model_id() -> str:
    return os.environ.get("MAILROOM_GENERATE_MODEL", GENERATE_IDENTIFIER)


def max_tokens() -> int:
    return clamp_max_tokens(os.environ.get("MAILROOM_GENERATE_MAX_TOKENS", MAX_TOKENS_DEFAULT))


def build_chat_payload(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens_value: int | None = None,
    include_thinking_off: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model or generate_model_id(),
        "messages": messages,
        "max_tokens": clamp_max_tokens(max_tokens_value if max_tokens_value is not None else max_tokens()),
        "temperature": 0.2,
        "stream": False,
    }
    if include_thinking_off:
        payload.update(THINKING_OFF_FIELDS)
    return payload


def fail_open_result(
    hits: list[Any],
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        "path": PATH_FAIL_OPEN,
        "answer": None,
        "hits": hits,
        "fail_open": True,
        "reason": reason,
    }


def live_result(answer: str, hits: list[Any]) -> dict[str, Any]:
    return {
        "path": PATH_LLMSTER,
        "answer": answer,
        "hits": hits,
        "fail_open": False,
    }


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: Mapping[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT_S,
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"error": raw}
        return exc.code, parsed
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise GenerateDown(f"{method} {url} failed: {exc}") from exc


def loaded_model_ids() -> list[str]:
    """GET /v1/models. With JIT off this is the loaded set, not the disk set."""
    status, payload = _http_json(generate_models_url(), timeout=READY_TIMEOUT_S)
    if status != 200 or not isinstance(payload, dict):
        raise GenerateDown(f"GET /v1/models status={status}")
    data = payload.get("data") or payload.get("models") or []
    if not isinstance(data, list):
        raise GenerateDown("GET /v1/models missing data list")
    ids: list[str] = []
    for item in data:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            ident = item.get("id") or item.get("name")
            if ident:
                ids.append(str(ident))
    return ids


def generate_is_loaded() -> bool:
    want = generate_model_id()
    try:
        ids = loaded_model_ids()
    except GenerateDown:
        return False
    return want in ids


def post_chat(payload: dict[str, Any]) -> str:
    status, body = _http_json(generate_url(), method="POST", body=payload)
    if status == 400 and any(k in payload for k in THINKING_OFF_FIELDS):
        retry = {k: v for k, v in payload.items() if k not in THINKING_OFF_FIELDS}
        status, body = _http_json(generate_url(), method="POST", body=retry)
    if status != 200 or not isinstance(body, dict):
        raise GenerateDown(f"POST /v1/chat/completions status={status}")
    choices = body.get("choices") or []
    if not choices:
        raise GenerateDown("empty choices")
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    if not isinstance(content, str) or not content.strip():
        raise GenerateDown("empty content")
    return content.strip()


OLLAMA_ALLOWED = frozenset({"ps", "stop"})


def _ollama_cmd(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    if not args or args[0] != "ollama" or (len(args) > 1 and args[1] not in OLLAMA_ALLOWED):
        raise GenerateDown(f"refused ollama command (embed-only): {args!r}")
    return subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)


def ollama_ps_text() -> str:
    try:
        proc = _ollama_cmd(["ollama", "ps"], timeout=15)
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired as exc:
        raise GenerateDown(f"ollama ps failed: {exc}") from exc
    if proc.returncode != 0:
        raise GenerateDown(f"ollama ps rc={proc.returncode}: {proc.stderr.strip()}")
    return (proc.stdout or "") + (proc.stderr or "")


def ollama_ps_empty() -> bool:
    """True when no Ollama model is loaded. Required before generate load."""
    text = ollama_ps_text().strip()
    if not text:
        return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    if len(lines) == 1 and lines[0].lower().startswith(("name", "model", "id")):
        return True
    body = [ln for ln in lines if not ln.lower().startswith(("name", "model", "id"))]
    return len(body) == 0


def unload_embed(*, embed_model: str | None = None) -> None:
    """Stop the embed model. Ollama is embed-only; never generate here."""
    model = embed_model or os.environ.get("MAILROOM_EMBED_MODEL")
    if model:
        _ollama_cmd(["ollama", "stop", model], timeout=30)
    leftover = ollama_ps_text()
    for line in leftover.splitlines()[1:]:
        name = line.split()[0] if line.strip() else ""
        if name and name.lower() not in {"name", "model", "id"}:
            _ollama_cmd(["ollama", "stop", name], timeout=30)
    if not ollama_ps_empty():
        raise GenerateDown("ollama ps not empty after unload_embed")


def _hits_list(ranked: Any) -> list[Any]:
    if ranked is None:
        return []
    if isinstance(ranked, list):
        return ranked
    if isinstance(ranked, dict) and "hits" in ranked:
        hits = ranked["hits"]
        return hits if isinstance(hits, list) else [hits]
    return [ranked]


def compose_messages(query: str, hits: list[Any]) -> list[dict[str, str]]:
    packed = json.dumps(hits, default=str, ensure_ascii=False)
    if len(packed) > 24000:
        packed = packed[:24000] + "…"
    system = (
        "Answer the user from the retrieved mail hits only. "
        "Cite the messages you use. If the hits do not contain the answer, say so."
    )
    user = f"Question:\n{query}\n\nHits:\n{packed}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def generate_from_hits(query: str, hits: list[Any]) -> str:
    if not generate_is_loaded():
        raise GenerateDown(
            f"GET /v1/models does not list {generate_model_id()!r}; JIT must stay off"
        )
    payload = build_chat_payload(compose_messages(query, hits))
    return post_chat(payload)


def ask_mail_live(
    query: str,
    *,
    retrieve: Callable[[str], Any],
    rerank: Callable[[str, Any], Any],
    embed_model: str | None = None,
) -> dict[str, Any]:
    """retrieve+rerank → unload embed → generate. Fail-open hits-only if generate is down."""
    retrieved = retrieve(query)
    ranked = rerank(query, retrieved)
    hits = _hits_list(ranked)
    try:
        unload_embed(embed_model=embed_model)
    except GenerateDown as exc:
        return fail_open_result(hits, reason=f"embed unload failed: {exc}")
    try:
        answer = generate_from_hits(query, hits)
    except GenerateDown as exc:
        return fail_open_result(hits, reason=str(exc))
    return live_result(answer, hits)
