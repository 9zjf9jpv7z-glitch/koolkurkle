#!/usr/bin/env python3
"""CRM gates for PR-7b CrossEncoder rerank.

Exits non-zero if a score vector has the wrong shape. That catches
Ollama comma-garbage and missing scores sold as working.

When torch/weights are missing, labels fail-open-only and still proves
the wired CrossEncoder.predict interface via a stub. Set
MAILROOM_RERANK_SMOKE_LIVE=1 to call the real model (downloads weights).

  python3 scripts/rerank_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import rerank_lib as rl  # noqa: E402

QUERY = "SDGE bill"
RELEVANT = "SDGE bill due Friday"
UNRELATED = "horse boarding newsletter"
PROOF_PATH = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "rerank_interface_proof.json"
)


class _StubEncoder:
    """Tiny stand-in for CrossEncoder.predict when weights are unavailable."""

    def predict(self, pairs, **_kwargs):
        out = []
        for _query, doc in pairs:
            text = str(doc or "")
            if "SDGE" in text and "bill" in text.lower():
                out.append(2.4)
            else:
                out.append(-3.1)
        return out


def _fail(message: str, payload: dict[str, Any]) -> int:
    payload["ok"] = False
    payload["error"] = message
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    sys.stderr.write("rerank smoke FAIL: %s\n" % message)
    return 2


def _assert_shape(raw: Any, expected: int, label: str) -> list[float] | str:
    try:
        return rl.coerce_score_vector(raw, expected)
    except rl.RerankError as exc:
        return "%s: %s" % (label, exc)


def main(argv: list[str] | None = None) -> int:
    del argv
    live_requested = os.environ.get("MAILROOM_RERANK_SMOKE_LIVE", "").strip() in (
        "1",
        "true",
        "yes",
    )
    import_ok = rl.crossencoder_import_ok()
    live = False
    relevant: float | None = None
    unrelated: float | None = None
    rerank_mode = "fail_open"
    label = "fail-open-only"
    path = "stub CrossEncoder.predict (weights unavailable in CI)"

    garbage_ok = False
    try:
        rl.coerce_score_vector("0.12, 0.45, 0.03", 3)
    except rl.RerankError:
        garbage_ok = True
    if not garbage_ok:
        return _fail(
            "comma-garbage was accepted as a score vector",
            {
                "rerank_mode": "fail_open",
                "generate_mode": "off",
                "gate": "negative_smoke",
            },
        )

    parse_ok = False
    try:
        rl.parse_rerank_score("0.12, 0.45, 0.03")
    except rl.RerankError:
        parse_ok = True
    if not parse_ok:
        return _fail(
            "parse_rerank_score sold comma-garbage as a working score",
            {
                "rerank_mode": "fail_open",
                "generate_mode": "off",
                "gate": "negative_smoke",
            },
        )

    scalar_ok = False
    try:
        rl.coerce_score_vector(0.91, 2)
    except rl.RerankError:
        scalar_ok = True
    if not scalar_ok:
        return _fail(
            "a scalar was sold as two scores",
            {
                "rerank_mode": "fail_open",
                "generate_mode": "off",
                "gate": "negative_smoke",
            },
        )

    if live_requested and import_ok:
        try:
            scores = rl.score_documents_crossencoder(
                QUERY, [RELEVANT, UNRELATED]
            )
            relevant, unrelated = scores[0], scores[1]
            live = True
            rerank_mode = "crossencoder"
            label = "live CrossEncoder.predict"
            path = "sentence_transformers.CrossEncoder(%s).predict" % rl.DEFAULT_CE_MODEL
        except rl.RerankError as exc:
            label = "fail-open-only (live predict failed: %s)" % rl._sanitize_err(
                str(exc)
            )
            live = False

    if not live:
        stub = _StubEncoder()
        scores = rl.score_documents_crossencoder(
            QUERY,
            [RELEVANT, UNRELATED],
            encoder=stub,
        )
        relevant, unrelated = scores[0], scores[1]
        if live_requested and import_ok:
            rerank_mode = "fail_open"
        else:
            # Wired path returned floats via the same score_documents_crossencoder.
            rerank_mode = "fail_open"
            label = "fail-open-only"

    if relevant is None or unrelated is None:
        return _fail(
            "interface proof missing scores",
            {"rerank_mode": rerank_mode, "generate_mode": "off"},
        )
    if not (rl.is_live_score(relevant) and rl.is_live_score(unrelated)):
        return _fail(
            "interface proof scores are not finite floats",
            {"rerank_mode": rerank_mode, "generate_mode": "off"},
        )
    if not (relevant > unrelated):
        return _fail(
            "relevant pair did not outscore unrelated pair",
            {
                "rerank_mode": rerank_mode,
                "generate_mode": "off",
                "relevant": relevant,
                "unrelated": unrelated,
            },
        )

    payload = {
        "ok": True,
        "gate": "crm_pr7b",
        "needed_signal": "yes/no logits or CrossEncoder.predict floats",
        "interface": path,
        "rerank_mode": rerank_mode,
        "generate_mode": "off",
        "label": label,
        "weights_available": live,
        "import_ok": import_ok,
        "example": {
            "query": QUERY,
            "relevant": {"document": RELEVANT, "score": relevant},
            "unrelated": {"document": UNRELATED, "score": unrelated},
        },
        "negative_smoke": {
            "comma_garbage_rejected": True,
            "scalar_sold_as_n_rejected": True,
        },
        "backend_default": rl.default_rerank_backend(),
        "model_default": rl.DEFAULT_CE_MODEL,
    }
    if not live:
        payload["fail_open_only"] = True
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    if PROOF_PATH.is_file():
        fixture = json.loads(PROOF_PATH.read_text(encoding="utf-8"))
        if fixture.get("needed_signal") != payload["needed_signal"]:
            return _fail("fixture needed_signal mismatch", payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
