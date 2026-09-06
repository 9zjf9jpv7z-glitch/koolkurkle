#!/usr/bin/env python3
"""In-process CrossEncoder rerank (MAILROOM.md §6.2 / PR-7b).

Default production backend is ``sentence_transformers.CrossEncoder`` on
``Qwen/Qwen3-Reranker-0.6B`` (MPS when available, CPU otherwise).
``predict`` floats (or yes/no logits) land on ``Hit.rerank``.

Ollama ``/api/generate`` and ``/api/chat`` cannot score Qwen3-Reranker.
That client remains opt-in (``MAILROOM_RERANK_BACKEND=ollama``) and still
fail-opens. Community GGUF ≠ scores. See docs/rerank.md.

Callers (``semantic_search.rerank_hits``) fail-open on ``RerankError``:
keep RRF order, set ``rerank=None``, stderr warning,
``rerank_mode=fail_open``.
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


BACKEND_CROSSENCODER = "crossencoder"
BACKEND_OLLAMA = "ollama"
BACKEND_OFF = "off"
MODE_FAIL_OPEN = "fail_open"
MODE_NONE = "none"

DEFAULT_RERANK_BACKEND = BACKEND_CROSSENCODER
DEFAULT_CE_MODEL = "Qwen/Qwen3-Reranker-0.6B"
ALT_CE_MODEL = "tomaarsen/Qwen3-Reranker-0.6B-seq-cls"
DEFAULT_RERANK_MODEL = DEFAULT_CE_MODEL
DEFAULT_OLLAMA_RERANK_MODEL = "qwen3-reranker:0.6b"
# Community Ollama port of Qwen/Qwen3-Reranker-0.6B (no official library
# tag; no untagged latest — pull :Q8_0 or :F16). Not a working scorer.
COMMUNITY_OLLAMA_TAG = "dengcao/Qwen3-Reranker-0.6B:Q8_0"
PULL_ONE_LINER = (
    "ollama pull dengcao/Qwen3-Reranker-0.6B:Q8_0 && "
    "ollama cp dengcao/Qwen3-Reranker-0.6B:Q8_0 qwen3-reranker:0.6b"
)
DEFAULT_RERANK_TIMEOUT = 20
RERANK_TOP = 20
DOC_CHAR_CAP = 1500
DEFAULT_MAX_LENGTH = 512
DEFAULT_KEEP_ALIVE = "30m"
OPTIONAL_EXTRA = "requirements-rerank.txt"

RERANK_SYSTEM = (
    "Judge whether the Document meets the requirements based on the Query "
    'and the Instruct provided. Note that the answer can only be "yes" or "no".'
)
RERANK_INSTRUCT = (
    "Given an email search query, retrieve relevant email messages or "
    "passages that answer the query"
)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_YES_NO = re.compile(r"\b(yes|no)\b", re.IGNORECASE)
_FLOAT = re.compile(r"[-+]?(?:\d+\.\d+|\.\d+|\d+)(?:[eE][-+]?\d+)?")
_COMMA_FLOATS = re.compile(
    r"[-+]?(?:\d+\.\d+|\.\d+|\d+)(?:[eE][-+]?\d+)?\s*,\s*"
    r"[-+]?(?:\d+\.\d+|\.\d+|\d+)(?:[eE][-+]?\d+)?"
)

Opener = Callable[..., object]
PredictFn = Callable[..., Any]

_CE_HOLD: dict[str, Any] = {}


class RerankError(RuntimeError):
    """Local reranker failure (never includes secrets or mail bodies)."""


def default_rerank_backend() -> str:
    raw = os.environ.get("MAILROOM_RERANK_BACKEND")
    if raw and raw.strip():
        return resolve_backend(raw)
    return DEFAULT_RERANK_BACKEND


def resolve_backend(value: str | None) -> str:
    raw = (value or default_rerank_backend()).strip().lower()
    if raw in ("crossencoder", "ce", "st", "sentence_transformers"):
        return BACKEND_CROSSENCODER
    if raw in ("ollama",):
        return BACKEND_OLLAMA
    if raw in ("off", "none", "0", "false"):
        return BACKEND_OFF
    return DEFAULT_RERANK_BACKEND


def default_rerank_model(backend: str | None = None) -> str:
    raw = os.environ.get("MAILROOM_RERANK_MODEL")
    if raw and raw.strip():
        return raw.strip()
    resolved = resolve_backend(backend) if backend is not None else default_rerank_backend()
    if resolved == BACKEND_OLLAMA:
        return DEFAULT_OLLAMA_RERANK_MODEL
    return DEFAULT_CE_MODEL


def default_rerank_instruction() -> str:
    raw = os.environ.get("MAILROOM_RERANK_INSTRUCTION")
    if raw and raw.strip():
        return raw.strip()
    return RERANK_INSTRUCT


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


def rerank_device() -> str:
    raw = os.environ.get("MAILROOM_RERANK_DEVICE")
    if raw and raw.strip():
        return raw.strip()
    try:
        import torch

        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def crossencoder_import_ok() -> bool:
    """True when the optional extra can be imported. Does not download weights."""
    try:
        import sentence_transformers  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


def is_live_score(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


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
    instruct = instruction if instruction is not None else default_rerank_instruction()
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


def _as_float(value: Any) -> float:
    if isinstance(value, str):
        raise RerankError("reranker score is text, not a float")
    if isinstance(value, bool):
        raise RerankError("reranker score is a bool, not a float")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RerankError("reranker score is not a number") from exc
    if not math.isfinite(number):
        raise RerankError("reranker score is not finite")
    return number


def _yes_no_softmax(yes_logit: Any, no_logit: Any) -> float:
    yes_s = math.exp(float(yes_logit))
    no_s = math.exp(float(no_logit))
    denom = yes_s + no_s
    if denom <= 0:
        raise RerankError("yes/no logits have a non-positive softmax denom")
    return yes_s / denom


def coerce_score_vector(raw: Any, expected: int) -> list[float]:
    """CRM shape gate: one finite float per document.

    Rejects Ollama comma-garbage, a scalar sold as N scores, and nested
    text. Accepts a list/tuple/ndarray of floats, or per-row yes/no
    logit pairs.
    """
    n = int(expected)
    if n < 0:
        raise RerankError("expected score count is negative")
    if raw is None:
        raise RerankError("reranker returned no scores")
    if isinstance(raw, (bytes, bytearray)):
        raise RerankError("reranker returned bytes, not score floats")
    if isinstance(raw, str):
        raise RerankError("reranker returned text, not score floats")
    if isinstance(raw, dict):
        raise RerankError("reranker returned an object, not score floats")

    values: Any = raw
    try:
        import numpy as np

        if isinstance(raw, np.ndarray):
            values = raw.tolist()
    except ImportError:
        pass

    if isinstance(values, (float, int)) and not isinstance(values, bool):
        if n == 1:
            return [_as_float(values)]
        raise RerankError("reranker returned a scalar, expected %d scores" % n)

    if not isinstance(values, (list, tuple)):
        raise RerankError("reranker score shape is not a list of floats")
    if len(values) != n:
        raise RerankError(
            "reranker returned %d scores, expected %d" % (len(values), n)
        )

    out: list[float] = []
    for item in values:
        if isinstance(item, str):
            raise RerankError("reranker score is text, not a float")
        if isinstance(item, (list, tuple)):
            if len(item) == 2 and not isinstance(item[0], (list, tuple, str)):
                out.append(_yes_no_softmax(item[0], item[1]))
                continue
            raise RerankError("reranker score is a nested list")
        out.append(_as_float(item))
    return out


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
            "Ollama generate/chat cannot score Qwen3-Reranker; "
            "default backend is CrossEncoder. "
            "If you still want the opt-in client: start Ollama and `%s`."
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
    """Map model output to a relevance score in [0, 1].

    Comma-separated number lists (Ollama generate garbage) are rejected.
    """
    if extra:
        from_lp = _score_from_yes_no_logprobs(extra)
        if from_lp is not None:
            return from_lp
    cleaned = _strip_think(text)
    if not cleaned:
        raise RerankError("reranker returned empty text")
    if _COMMA_FLOATS.search(cleaned):
        raise RerankError("reranker output looks like comma-separated garbage")
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


def _import_cross_encoder() -> Any:
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        raise RerankError(
            "sentence_transformers not installed (optional extra %s). "
            "Slim installs fail-open."
            % OPTIONAL_EXTRA
        ) from exc
    return CrossEncoder


def _candidate_ce_models(explicit: str | None) -> list[str]:
    if explicit:
        return [explicit]
    env = os.environ.get("MAILROOM_RERANK_MODEL")
    if env and env.strip():
        return [env.strip()]
    return [DEFAULT_CE_MODEL, ALT_CE_MODEL]


def _load_cross_encoder(
    model_name: str,
    instruction: str,
) -> Any:
    CrossEncoder = _import_cross_encoder()
    device = rerank_device()
    base_kwargs: dict[str, Any] = {
        "device": device,
        "trust_remote_code": True,
        "max_length": DEFAULT_MAX_LENGTH,
    }
    try:
        model = CrossEncoder(
            model_name,
            prompts={"query": instruction},
            default_prompt_name="query",
            **base_kwargs,
        )
    except TypeError:
        model = CrossEncoder(model_name, **base_kwargs)
    except Exception as exc:
        raise RerankError(
            "CrossEncoder load failed (%s): %s"
            % (model_name, _sanitize_err(str(exc)))
        ) from None
    return model


def get_cross_encoder(
    *,
    model: str | None = None,
    instruction: str | None = None,
    encoder: Any = None,
) -> Any:
    if encoder is not None:
        return encoder
    instruct = instruction if instruction is not None else default_rerank_instruction()
    last_err: Exception | None = None
    for name in _candidate_ce_models(model):
        key = "%s|%s|%s" % (name, rerank_device(), instruct)
        held = _CE_HOLD.get(key)
        if held is not None:
            return held
        try:
            loaded = _load_cross_encoder(name, instruct)
        except RerankError as exc:
            last_err = exc
            continue
        _CE_HOLD[key] = loaded
        return loaded
    if last_err is not None:
        raise last_err
    raise RerankError("CrossEncoder load failed")


def unload_cross_encoder() -> None:
    """Drop the in-process reranker before LM Studio 35B generate."""
    _CE_HOLD.clear()
    try:
        import torch

        if hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
            torch.mps.empty_cache()
        cuda = getattr(torch, "cuda", None)
        if cuda is not None and cuda.is_available():
            cuda.empty_cache()
    except Exception:
        pass


def _predict_pairs(
    encoder: Any,
    pairs: list[tuple[str, str]],
    instruction: str,
    predict_fn: PredictFn | None,
) -> Any:
    worker = predict_fn
    if worker is None:
        if encoder is None or not hasattr(encoder, "predict"):
            raise RerankError("CrossEncoder has no predict")
        worker = encoder.predict
    try:
        return worker(
            pairs,
            prompt=instruction,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
    except TypeError:
        try:
            return worker(pairs, show_progress_bar=False)
        except TypeError:
            return worker(pairs)


def score_documents_crossencoder(
    query: str,
    documents: Sequence[str],
    *,
    model: str | None = None,
    instruction: str | None = None,
    encoder: Any = None,
    predict_fn: PredictFn | None = None,
) -> list[float]:
    """Batch-score (query, snippet) pairs via CrossEncoder.predict."""
    docs = list(documents)
    if not docs:
        return []
    instruct = instruction if instruction is not None else default_rerank_instruction()
    pairs = [(query or "", doc or "") for doc in docs]
    loaded = encoder
    if predict_fn is None:
        loaded = get_cross_encoder(model=model, instruction=instruct, encoder=encoder)
    raw = _predict_pairs(loaded, pairs, instruct, predict_fn)
    return coerce_score_vector(raw, len(docs))


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
    """Score one (query, document) pair via local Ollama (opt-in backend)."""
    tag = model or default_rerank_model(BACKEND_OLLAMA)
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


def score_documents_ollama(
    query: str,
    documents: Sequence[str],
    *,
    model: str | None = None,
    ollama_url: str | None = None,
    opener: Opener | None = None,
    timeout: int | None = None,
    instruction: str | None = None,
) -> list[float]:
    docs = list(documents)
    if not docs:
        return []
    scores = [
        score_one(
            query,
            doc,
            model=model,
            ollama_url=ollama_url,
            opener=opener,
            timeout=timeout,
            instruction=instruction,
        )
        for doc in docs
    ]
    return coerce_score_vector(scores, len(docs))


def score_documents(
    query: str,
    documents: Sequence[str],
    *,
    backend: str | None = None,
    model: str | None = None,
    ollama_url: str | None = None,
    opener: Opener | None = None,
    timeout: int | None = None,
    instruction: str | None = None,
    encoder: Any = None,
    predict_fn: PredictFn | None = None,
) -> list[float]:
    """Score each document. Raises on the first failure (caller fail-opens)."""
    resolved = resolve_backend(backend)
    if resolved == BACKEND_OFF:
        raise RerankError("rerank backend is off")
    if resolved == BACKEND_OLLAMA:
        return score_documents_ollama(
            query,
            documents,
            model=model,
            ollama_url=ollama_url,
            opener=opener,
            timeout=timeout,
            instruction=instruction,
        )
    return score_documents_crossencoder(
        query,
        documents,
        model=model,
        instruction=instruction,
        encoder=encoder,
        predict_fn=predict_fn,
    )
