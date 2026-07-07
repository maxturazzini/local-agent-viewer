"""
Shared classification utilities and dispatcher.

Schema, sanitization, JSON parsing, message preparation are shared across
both classification backends (openai_strict, ollama_compat).
The classify_interaction() dispatcher routes to the correct backend.
"""

import json
import os
import re
from typing import Any, Dict, List

from lav import config

# Field order: descriptive fields first, classification fields last.
# When models generate JSON in order, they build context (summary, abstract,
# topics, people) before making classification decisions — implicit chain-of-thought.

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "1-sentence summary of the interaction"
        },
        "abstract": {
            "type": "string",
            "description": "Context, problem addressed, and decisions made (2-3 sentences)"
        },
        "process": {
            "type": "string",
            "description": "Workflow or process the user is executing, inferred from questions and actions. Describe as a concrete activity (e.g. 'debug deploy pipeline', 'client workshop preparation'). Empty string if not inferable."
        },
        "topics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 5 specific keywords"
        },
        "people": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Third parties mentioned (not the user or the assistant)"
        },
        "clients": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Companies/clients mentioned"
        },
        "classification": {
            "type": "string",
            "enum": config.CLASSIFICATIONS,
            "description": "Primary classification of the interaction"
        },
        "data_sensitivity": {
            "type": "string",
            "enum": config.SENSITIVITIES,
            "description": "Data sensitivity level. If third parties are mentioned, at least internal."
        },
        "sensitive_data_types": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Types of sensitive data present (empty if public)"
        },
    },
    "required": [
        "summary", "abstract", "process", "topics", "people", "clients",
        "classification", "data_sensitivity", "sensitive_data_types"
    ],
    "additionalProperties": False,
}

VALID_CLASSIFICATIONS = set(config.CLASSIFICATIONS)
VALID_SENSITIVITIES = set(config.SENSITIVITIES)

_EMPTY_RESULT = {
    "summary": "",
    "abstract": "",
    "process": "",
    "topics": [],
    "people": [],
    "clients": [],
    "classification": "development",
    "data_sensitivity": "internal",
    "sensitive_data_types": [],
}


def _sanitize_result(raw: dict) -> dict:
    """Ensure result has all required fields with valid values."""
    result = {}
    for key, default in _EMPTY_RESULT.items():
        val = raw.get(key, default)
        if isinstance(default, list) and not isinstance(val, list):
            val = [val] if val else []
        if isinstance(default, str) and not isinstance(val, str):
            val = str(val) if val else ""
        result[key] = val

    if result["classification"] not in VALID_CLASSIFICATIONS:
        result["classification"] = "development"
    if result["data_sensitivity"] not in VALID_SENSITIVITIES:
        result["data_sensitivity"] = "internal"

    result["topics"] = result["topics"][:5]

    if result["people"] and result["data_sensitivity"] == "public":
        result["data_sensitivity"] = "internal"

    return result


# --------------------------------------------------------------------------- #
# Sensitivity floor (hybrid). data_sensitivity is a soft policy call the model
# UNDER-escalates (dominant gold error: confidential→internal). Rather than trust
# the model alone, compute a deterministic MINIMUM from high-signal detectors +
# the entities the model already extracts, then take MAX(floor, model_guess):
# rules can only RAISE sensitivity, never lower it. Opt-in via LAV_SENSITIVITY_FLOOR=1.
# --------------------------------------------------------------------------- #
_SENS_ORDER = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}

_CRED_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|client[_-]?secret|password|passwd)\b"
    r"|\bsk-[A-Za-z0-9]{20,}\b|\bAKIA[0-9A-Z]{16}\b|\bghp_[A-Za-z0-9]{30,}\b"
    r"|\bBearer\s+[A-Za-z0-9._\-]{20,}\b"
)
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
# Multilingual (IT+EN). `genetic\w*` catches genetic/genetico/genetica — the old
# `\bgenetic\b` missed the Italian "genetico" (a real leak: a genetic report → public).
_HEALTH_RE = re.compile(
    r"(?i)(\bdiagnos\w+|\bpatients?\b|\bpazient\w+|medical record|cartell[ae] clinic\w+|"
    r"\brefert\w+|blood test|esami?\s+del\s+sangue|health data|\bgenetic\w*|\bDNA\b)"
)
# Interactions under the user's sales/ or clients/ folders are client material →
# confidential floor even when the model surfaced no client entity (a real leak class).
_PATH_RE = re.compile(r"(?i)\b(?:sales|clients)/")


def _bump(cur: str, level: str) -> str:
    return level if _SENS_ORDER.get(level, 0) > _SENS_ORDER.get(cur, 0) else cur


def sensitivity_floor(text: str, result: dict) -> str:
    """Deterministic MINIMUM sensitivity from detectors + already-extracted entities.

    Tuned "balanced-cautious" (Max's call): the costly error is UNDER-escalation
    (a leak), so escalate on any POSITIVE business/entity signal, but leave clearly
    generic rows at the model's guess to keep public/internal discrimination."""
    floor = "public"
    cls = result.get("classification")
    if cls == "content_production":
        floor = _bump(floor, "internal")      # own work-in-progress → internal until published
    if cls in ("meeting", "marketing"):
        floor = _bump(floor, "confidential")  # client meetings / offers & proposals
    if result.get("clients"):
        floor = _bump(floor, "confidential")  # a named org → client/business content
    if result.get("people"):
        floor = _bump(floor, "confidential")  # a named third-party person
    sdt = set(result.get("sensitive_data_types") or [])
    if sdt & {"credentials", "api_keys", "financial", "personal_data", "employee_data"}:
        floor = _bump(floor, "restricted")
    if sdt & {"client_strategy", "pricing", "contracts", "internal_architecture"}:
        floor = _bump(floor, "confidential")
    t = text or ""
    if _PATH_RE.search(t):                     # under sales/ or clients/ → client material
        floor = _bump(floor, "confidential")
    if _CRED_RE.search(t) or _IBAN_RE.search(t) or _HEALTH_RE.search(t):
        floor = _bump(floor, "restricted")
    return floor


def apply_sensitivity_floor(result: dict, text: str, full_text: str = None) -> dict:
    """MAX(rule_floor, model_guess) for data_sensitivity — never lowers it. When
    full_text is given, the regex detectors scan it (the WHOLE session) instead of the
    truncated head, so a credential/PII appearing late in a long session is caught."""
    scan = full_text if full_text is not None else text
    result["data_sensitivity"] = _bump(
        result.get("data_sensitivity", "internal"), sensitivity_floor(scan, result)
    )
    return result


def _parse_json_response(content: str) -> dict:
    """Parse JSON from model response, handling markdown fences and thinking tags."""
    if not content or not content.strip():
        return {}
    text = content.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]  # remove opening ```json or ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    # Strip thinking tags (Qwen, DeepSeek)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Find first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


# Noise that pollutes the classification input: base64 image/data blobs, injected
# system-wrapper tags, and recurring IDE/harness prose that carries no user intent.
# Stripped in ALL modes ("cleaner context").
_DATAURI_RE = re.compile(r"data:[^;,\s]+;base64,[A-Za-z0-9+/=]+")
_B64_RE = re.compile(r"[A-Za-z0-9+/]{300,}={0,2}")
_WRAP_RE = re.compile(
    r"</?(?:command-[a-z-]+|local-command-[a-z-]+|ide_[a-z_]+|system-reminder|thinking)[^>]*>",
    re.I,
)
_NOISE_RE = re.compile(
    r"The user opened the file [^\n]*? in the IDE\."
    r"|This may or may not be related to the current task\."
    r"|\[Request interrupted by user[^\]]*\]"
    r"|Caveat: The messages below were generated by the user while running local commands\.[^\n]*",
    re.I,
)


def _clean(text: str) -> str:
    if not text:
        return text
    text = _DATAURI_RE.sub("[image]", text)
    text = _B64_RE.sub("[blob]", text)
    text = _WRAP_RE.sub(" ", text)
    text = _NOISE_RE.sub(" ", text)
    return text


def _coerce_blocks(content):
    """Return a list of content blocks if `content` is a block list OR a JSON-
    stringified block list (e.g. '[{"type":"text","text":"..."}]'), else None.
    Parsing the stringified form is key: otherwise the model sees raw JSON, not text."""
    if isinstance(content, list):
        return content
    if isinstance(content, str) and content.lstrip().startswith("[{"):
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, list) else None
        except Exception:
            return None
    return None


def _blocks_to_text(blocks, rich: bool) -> str:
    """Extract readable text from content blocks: text + tool names (+ tool results in rich)."""
    out = []
    for block in blocks:
        if isinstance(block, dict):
            bt = block.get("type")
            if bt == "text":
                out.append(block.get("text", ""))
            elif bt == "tool_use":
                out.append(f"[tool: {block.get('name', '')}]")
            elif bt == "tool_result" and rich:
                tr = block.get("content", "")
                if isinstance(tr, list):
                    tr = " ".join(b.get("text", "") for b in tr if isinstance(b, dict))
                out.append(f"[result: {_clean(str(tr))[:500]}]")
        elif isinstance(block, str):
            out.append(block)
    return " ".join(out)


def prepare_messages_for_classification(messages: List[Dict]) -> str:
    """Build the classifier input.

    Cleans base64 blobs, system-wrapper tags, and recurring IDE/harness noise, and
    parses JSON-stringified content-block lists so the model sees real text, not raw
    JSON. Default mode keeps the user intent (full) + assistant first line and drops
    tool results; LAV_CLASSIFY_RICH=1 adds full assistant text, tool names, and
    truncated tool results. Truncated to config.CLASSIFY_MAX_CHARS."""
    rich = os.getenv("LAV_CLASSIFY_RICH", "").strip().lower() in ("1", "true", "yes")
    parts = []
    for msg in messages:
        msg_type = msg.get("type", "")
        content = msg.get("content", "")

        blocks = _coerce_blocks(content)
        if blocks is not None:
            is_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)
            if is_tool_result and not rich:
                continue
            content = _blocks_to_text(blocks, rich)

        content = _clean(str(content)).strip()
        if not content:
            continue

        if msg_type == "user":
            parts.append(f"User: {content}")
        elif msg_type == "assistant":
            parts.append(f"Assistant: {content[:3000] if rich else content.split(chr(10))[0][:300]}")

    text = "\n\n".join(parts)
    return text[:config.CLASSIFY_MAX_CHARS]


def full_scan_text(messages: List[Dict]) -> str:
    """Uncapped concat of ALL message text (incl. tool results), cleaned — for the
    deterministic sensitivity detectors, which should see the WHOLE session, not just
    the truncated head the LLM receives (a long session sends the model ~3-4% of it)."""
    parts = []
    for msg in messages:
        blocks = _coerce_blocks(msg.get("content", ""))
        if blocks is None:
            parts.append(str(msg.get("content", "")))
            continue
        for b in blocks:
            if isinstance(b, dict):
                bt = b.get("type")
                if bt == "text":
                    parts.append(b.get("text", ""))
                elif bt == "tool_result":
                    tr = b.get("content", "")
                    if isinstance(tr, list):
                        tr = " ".join(x.get("text", "") for x in tr if isinstance(x, dict))
                    parts.append(str(tr))
            elif isinstance(b, str):
                parts.append(b)
    return _clean(" ".join(parts))


def _resolve_backend() -> str:
    """Resolve which backend to use based on config."""
    backend = config.CLASSIFY_BACKEND
    if backend == "auto":
        return "ollama" if config.CLASSIFY_BASE_URL else "openai"
    return backend


def classify_interaction(
    messages: List[Dict],
    openai_client,
    model: str = "",
    session_key=None,
) -> Dict[str, Any]:
    """Classify an interaction, dispatching to the correct backend.

    Backend is determined by LAV_CLASSIFY_BACKEND (auto/openai/ollama/foundry).
    When auto: uses openai if no BASE_URL, ollama otherwise. ``session_key`` is a
    (session_id, project_id) tuple accepted for callers that pass it; no current
    backend uses it.
    """
    backend = _resolve_backend()
    if backend == "foundry":
        from lav.classifiers.foundry.classify import classify
        return classify(messages, openai_client, model)
    if backend == "openai":
        from lav.classifiers.openai_strict import classify
    else:
        from lav.classifiers.ollama_compat import classify
    return classify(messages, openai_client, model)
