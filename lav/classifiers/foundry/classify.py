"""Azure AI Foundry classification path — full single-call, strict json_schema.

Robust cloud models (gpt-5.1-mini, gpt-oss-120b) do the WHOLE 9-field task in one
constrained call, exactly like openai_strict — no two-stage crutch, no kNN. This
file isolates the Foundry-specific call quirks so the working openai_strict path
stays untouched:
  - token-param name (gpt-5/o-series want `max_completion_tokens`, others `max_tokens`);
  - a json_schema → json_object fallback for serverless endpoints that don't
    support strict structured outputs.

Reuses the SAME schema + system prompt as openai_strict (rendered from taxonomy),
so the task definition never drifts. Selected via LAV_CLASSIFY_BACKEND=foundry.
"""

import os
from typing import Any, Dict, List

from lav import config
from lav.classifiers.openai_classifier import (
    CLASSIFICATION_SCHEMA,
    _EMPTY_RESULT,
    _parse_json_response,
    _sanitize_result,
    prepare_messages_for_classification,
)
from lav.classifiers.openai_strict import SYSTEM_PROMPT

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "interaction_metadata", "strict": True, "schema": CLASSIFICATION_SCHEMA},
}


def _token_kwarg(model: str) -> dict:
    # Foundry reasoning models (gpt-5.x, gpt-oss, o-series) consume hidden reasoning
    # tokens BEFORE the JSON, so a tight budget yields empty/truncated output. All
    # current Foundry deployments accept max_completion_tokens; give generous
    # headroom so reasoning + the 9-field JSON both fit. The fallback swaps to
    # max_tokens for any deployment that rejects it.
    return {"max_completion_tokens": int(os.getenv("LAV_FOUNDRY_MAX_TOKENS", "4000"))}


# Per-call token usage (ground truth from the API response — no Azure Monitor lag).
# The eval reads this to build the cost/task table.
USAGE: List[Dict[str, Any]] = []


def _record_usage(model: str, u) -> None:
    if u is None:
        return
    pt = getattr(u, "prompt_tokens", 0) or 0
    ct = getattr(u, "completion_tokens", 0) or 0
    det = getattr(u, "completion_tokens_details", None)
    rt = (getattr(det, "reasoning_tokens", 0) or 0) if det is not None else 0
    USAGE.append({"model": model, "prompt": pt, "completion": ct, "reasoning": rt,
                  "total": getattr(u, "total_tokens", 0) or (pt + ct)})


def classify(messages: List[Dict], openai_client, model: str = "") -> Dict[str, Any]:
    model = model or config.CLASSIFY_MODEL
    system_prompt = config.CLASSIFY_SYSTEM_PROMPT or SYSTEM_PROMPT

    text = prepare_messages_for_classification(messages)
    if not text.strip():
        r = dict(_EMPTY_RESULT)
        r["summary"] = "(empty interaction)"
        return r

    user = f"Classify this interaction:\n\n{text}"
    tok = _token_kwarg(model)

    # Reasoning effort (gpt-5.x, gpt-oss). Classification barely needs reasoning;
    # medium (the default) burns hidden tokens billed as output — the token hog.
    # Set LAV_FOUNDRY_REASONING_EFFORT=minimal|low to cut cost/latency. Models that
    # don't accept the param (e.g. DeepSeek-Flash) drop it via the last-resort call.
    extra = {}
    _re = os.getenv("LAV_FOUNDRY_REASONING_EFFORT", "").strip()
    if _re:
        extra["reasoning_effort"] = _re

    _holder = {}

    def _call(response_format, user_content, token_kwargs, more):
        resp = openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format=response_format,
            **token_kwargs,
            **more,
        )
        _holder["usage"] = getattr(resp, "usage", None)
        return resp.choices[0].message.content or ""

    try:
        content = _call(_RESPONSE_FORMAT, user, tok, extra)
    except Exception:
        # Endpoint may not support strict json_schema (some serverless OSS
        # deployments), the token-param name, or reasoning_effort. Fall back to
        # json_object; last resort drops reasoning_effort and swaps the token kwarg.
        keys = ", ".join(CLASSIFICATION_SCHEMA["properties"].keys())
        hint = f"{user}\n\nReturn ONLY a JSON object with exactly these keys: {keys}."
        alt = {"max_tokens": 2000} if "max_completion_tokens" in tok else {"max_completion_tokens": 2000}
        try:
            content = _call({"type": "json_object"}, hint, tok, extra)
        except Exception:
            content = _call({"type": "json_object"}, hint, alt, {})

    _record_usage(model, _holder.get("usage"))
    raw = _parse_json_response(content)
    result = _sanitize_result(raw)
    if os.getenv("LAV_SENSITIVITY_FLOOR", "").strip().lower() in ("1", "true", "yes"):
        from lav.classifiers.openai_classifier import apply_sensitivity_floor, full_scan_text
        result = apply_sensitivity_floor(result, text, full_text=full_scan_text(messages))
    return result
