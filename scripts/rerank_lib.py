#!/usr/bin/env python3
"""Local Qwen3-Reranker-0.6B client (MAILROOM.md §6.2 step 7 / PR-7).

Talks to Ollama the same way ``embed_lib`` does: urllib POST to a local
base URL, no API key, no cloud. Callers (``semantic_search.rerank_hits``)
fail-open on ``RerankError``.

Default tag is ``qwen3-reranker:0.6b`` (override with ``MAILROOM_RERANK_MODEL``).
Ollama has no official library reranker yet; pull the community model and
alias it (see ``PULL_ONE_LINER`` / docs/rerank.md).
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Sequence

try:
    import embed_lib as el
except ImportError:  # script dir on sys.path (same as embed_lib callers)
    el = None  # type: ignore[assignment]


DEFAULT_RERANK_MODEL = "qwen3-reranker:0.6b"
# Community Ollama port of Qwen/Qwen3-Reranker-0.6B (no official library tag).
COMMUNITY_OLLAMA_TAG = "dengcao/Qwen3-Reranker-0.6B"
PULL_ONE_LINER = (
    "ollama pull dengcao/Qwen3-Reranker-0.6B && "
    "ollama cp dengcao/Qwen3-Reranker-0.6B qwen3-reranker:0.6b"
)
DEFAULT_RERANK_TIMEOUT = 20
RERANK_TOP = 20
DOC_CHAR_CAP = 1500
DEFAULT_KEEP_ALIVE = "30m"

RERANK_SYSTEM = (
    "Judge whether the Document meets the requirements based on the Query "
    'and the Instruct provided. Note that the answer can only be "yes" or "no".'
)
RERANK_INSTRUCT = (
    "Given a mail search query, retrieve the most relevant email."
)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_YES_NO = re.compile(r"\b(yes|no)\b", re.IGNORECASE)
_FLOAT = re.compile(r"[-+]?(?:\d+\.\d+|\.\d+|\d+)(?:[eE][-+]?\d+)?")

Opener = Callable[..., object]


class RerankError(RuntimeError):
    """Local reranker failure (never includes secrets or mail bodies)."""


def default_rerank_model() -> str:
    raw = os.environ.get("MAILROOM_RERANK_MODEL")
    if raw and raw.strip():
        return raw.strip()
    return DEFAULT_RERANK_MODEL


def default_rerank_timeout() -> int:
    raw = os.environ.get("MAILROOM_RERANK_TIMEOUT")
    if raw and raw.strip():
        try:
            return max(1, int(raw.strip()))
        except ValueError:
            return DEFAULT_RERANK_TIMEOUT
    return DEFAULT_RERANK_TIMEOUT


def default_ollama_url() -> str:
    if el is not None:
        return el.DEFAULT_OLLAMA_URL
    return "http://127.0.0.1:11434"


def hit_document(hit: dict[str, Any], *, cap: int = DOC_CHAR_CAP) -> str:
    """Subject + snippet only. Never uses a body field. Caps length."""
    subject = str(hit.get("subject") or "").strip()
    snippet = str(hit.get("snippet") or "").strip()
    if subject and snippet:
        text = "%s\n%s" % (subject, snippet)
    else:
        text = subject or snippet
    if len(text) > cap:
        return text[:cap]
    return text


def format_pair(
    query: str,
    document: str,
    *,
    instruction: str | None = None,
) -> str:
    instruct = instruction if instruction is not None else RERANK_INSTRUCT
    return "<Instruct>: %s\n<Query>: %s\n<Document>: %s" % (
        instruct,
        query or "",
        document or "",
    )


def official_generate_prompt(
    query: str,
    document: str,
    *,
    instruction: str | None = None,
) -> str:
    """Qwen3-Reranker raw prompt (system + user + empty think suffix)."""
    pair = format_pair(query, document, instruction=instruction)
    return (
        "<|im_start|>system\n"
        "%s<|im_end|>\n"
        "<|im_start|>user\n"
        "%s<|im_end|>\n"
        "<|im_start|>assistant\n"
        "<think>\n\n</think>\n\n"
    ) % (RERANK_SYSTEM, pair)


def _join_url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _sanitize_err(text: str, limit: int = 180) -> str:
    """Short error snippet: no secrets, no mail bodies."""
    collapsed = " ".join((text or "").split())
    return collapsed[:limit]


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    opener: Opener | None,
    timeout: int,
) -> tuple[int, bytes]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", None) or getattr(resp, "code", 200)
            return int(status), raw
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RerankError(
            "Ollama rerank HTTP %s at %s: %s"
            % (exc.code, url, _sanitize_err(err_body))
        ) from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RerankError(
            "Cannot reach Ollama at %s (%r). "
            "Start it locally (`brew services start ollama` or `ollama serve`) "
            "and pull the reranker: `%s`."
            % (url, reason, PULL_ONE_LINER)
        ) from None
    except TimeoutError:
        raise RerankError("Ollama rerank timed out at %s" % url) from None


def _strip_think(text: str) -> str:
    return _THINK_BLOCK.sub("", text or "").strip()


def _score_from_yes_no_logprobs(data: dict[str, Any]) -> float | None:
    """Softmax(yes, no) when Ollama returns token logprobs."""
    candidates: list[Any] = []
    for key in ("logprobs", "probs"):
        val = data.get(key)
        if isinstance(val, list):
            candidates.extend(val)
        elif isinstance(val, dict):
            inner = val.get("content") or val.get("tokens") or val.get("top_logprobs")
            if isinstance(inner, list):
                candidates.extend(inner)
    message = data.get("message")
    if isinstance(message, dict):
        for key in ("logprobs", "probs"):
            val = message.get(key)
            if isinstance(val, list):
                candidates.extend(val)
    yes_lp: float | None = None
    no_lp: float | None = None
    for item in candidates:
        if not isinstance(item, dict):
            continue
        token = str(item.get("token") or item.get("text") or "").strip().lower()
        if token.startswith("yes"):
            token = "yes"
        elif token.startswith("no"):
            token = "no"
        lp = item.get("logprob")
        if lp is None and item.get("prob") is not None:
            try:
                p = float(item["prob"])
                if p > 0:
                    lp = math.log(p)
            except (TypeError, ValueError):
                lp = None
        if lp is None:
            continue
        try:
            lp_f = float(lp)
        except (TypeError, ValueError):
            continue
        if token == "yes" and yes_lp is None:
            yes_lp = lp_f
        elif token == "no" and no_lp is None:
            no_lp = lp_f
    if yes_lp is None and no_lp is None:
        return None
    if yes_lp is None:
        yes_lp = -10.0
    if no_lp is None:
        no_lp = -10.0
    yes_s = math.exp(yes_lp)
    no_s = math.exp(no_lp)
    denom = yes_s + no_s
    if denom <= 0:
        return None
    return yes_s / denom


def parse_rerank_score(text: str, extra: dict[str, Any] | None = None) -> float:
    """Map model output to a relevance score in [0, 1]."""
    if extra:
        from_lp = _score_from_yes_no_logprobs(extra)
        if from_lp is not None:
            return from_lp
    cleaned = _strip_think(text)
    if not cleaned:
        raise RerankError("reranker returned empty text")
    yn = _YES_NO.search(cleaned)
    if yn:
        return 1.0 if yn.group(1).lower() == "yes" else 0.0
    num = _FLOAT.search(cleaned)
    if num:
        try:
            value = float(num.group(0))
        except ValueError as exc:
            raise RerankError("reranker score is not a number") from exc
        if value < 0.0:
            return 0.0
        if value > 1.0:
            # logits / 0-100 scales: squash into (0, 1)
            if value <= 100.0:
                return max(0.0, min(1.0, value / 100.0))
            return 1.0 / (1.0 + math.exp(-value))
        return value
    raise RerankError("reranker output is not yes/no or a score")


def _response_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("response"), str):
        return data["response"]
    message = data.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return ""


def score_one(
    query: str,
    document: str,
    *,
    model: str | None = None,
    ollama_url: str | None = None,
    opener: Opener | None = None,
    timeout: int | None = None,
    instruction: str | None = None,
) -> float:
    """Score one (query, document) pair via local Ollama."""
    tag = model or default_rerank_model()
    base = (ollama_url or default_ollama_url()).rstrip("/")
    wait = int(timeout if timeout is not None else default_rerank_timeout())
    gen_payload: dict[str, Any] = {
        "model": tag,
        "prompt": official_generate_prompt(
            query, document, instruction=instruction
        ),
        "stream": False,
        "raw": True,
        "keep_alive": DEFAULT_KEEP_ALIVE,
        "options": {"temperature": 0.0, "num_predict": 8},
    }
    chat_payload: dict[str, Any] = {
        "model": tag,
        "messages": [
            {"role": "system", "content": RERANK_SYSTEM},
            {
                "role": "user",
                "content": format_pair(query, document, instruction=instruction),
            },
        ],
        "stream": False,
        "think": False,
        "keep_alive": DEFAULT_KEEP_ALIVE,
        "options": {"temperature": 0.0, "num_predict": 8},
    }

    data: dict[str, Any] | None = None
    last_err: Exception | None = None
    try:
        _status, raw = _post_json(
            _join_url(base, "/api/generate"),
            gen_payload,
            opener=opener,
            timeout=wait,
        )
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict):
            data = parsed
    except RerankError as exc:
        last_err = exc
        msg = str(exc)
        if "HTTP 404" not in msg and "HTTP 400" not in msg:
            raise
        data = None
    except (ValueError, json.JSONDecodeError):
        last_err = RerankError("Ollama /api/generate returned non-JSON")
        data = None

    if data is None:
        try:
            _status, raw = _post_json(
                _join_url(base, "/api/chat"),
                chat_payload,
                opener=opener,
                timeout=wait,
            )
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise RerankError("Ollama /api/chat returned a non-object")
            data = parsed
        except RerankError:
            if last_err is not None:
                raise last_err
            raise

    try:
        return parse_rerank_score(_response_text(data), data)
    except RerankError:
        raise


def score_documents(
    query: str,
    documents: Sequence[str],
    *,
    model: str | None = None,
    ollama_url: str | None = None,
    opener: Opener | None = None,
    timeout: int | None = None,
    instruction: str | None = None,
) -> list[float]:
    """Score each document. Raises on the first failure (caller fail-opens)."""
    if not documents:
        return []
    return [
        score_one(
            query,
            doc,
            model=model,
            ollama_url=ollama_url,
            opener=opener,
            timeout=timeout,
            instruction=instruction,
        )
        for doc in documents
    ]
