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


# Noise that pollutes the classification input: base64 image/data blobs and
# injected system-wrapper tags. Stripped in ALL modes ("cleaner context").
_DATAURI_RE = re.compile(r"data:[^;,\s]+;base64,[A-Za-z0-9+/=]+")
_B64_RE = re.compile(r"[A-Za-z0-9+/]{300,}={0,2}")
_WRAP_RE = re.compile(
    r"</?(?:command-[a-z-]+|local-command-[a-z-]+|ide_[a-z_]+|system-reminder|thinking)[^>]*>",
    re.I,
)


def _clean(text: str) -> str:
    if not text:
        return text
    text = _DATAURI_RE.sub("[image]", text)
    text = _B64_RE.sub("[blob]", text)
    text = _WRAP_RE.sub(" ", text)
    return text


def prepare_messages_for_classification(messages: List[Dict]) -> str:
    """Build the classifier input.

    Always cleans out base64 blobs and injected system-wrapper tags. Default mode
    keeps the user intent (full) + assistant first line, and drops tool results.
    With env LAV_CLASSIFY_RICH=1, it also includes full assistant text, tool names,
    and truncated tool results — the ACTIONS, not just the intent. Truncated to
    config.CLASSIFY_MAX_CHARS (env LAV_CLASSIFY_MAX_CHARS, default 12000)."""
    rich = os.getenv("LAV_CLASSIFY_RICH", "").strip().lower() in ("1", "true", "yes")
    parts = []
    for msg in messages:
        msg_type = msg.get("type", "")
        content = msg.get("content", "")

        if isinstance(content, list):
            is_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            if is_tool_result and not rich:
                continue
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    bt = block.get("type")
                    if bt == "text":
                        text_parts.append(block.get("text", ""))
                    elif bt == "tool_use":
                        text_parts.append(f"[tool: {block.get('name', '')}]")
                    elif bt == "tool_result" and rich:
                        tr = block.get("content", "")
                        if isinstance(tr, list):
                            tr = " ".join(b.get("text", "") for b in tr if isinstance(b, dict))
                        text_parts.append(f"[result: {_clean(str(tr))[:500]}]")
                elif isinstance(block, str):
                    text_parts.append(block)
            content = " ".join(text_parts)
        elif isinstance(content, str) and content.lstrip().startswith("[{") and "tool_result" in content[:200]:
            if not rich:
                continue
            content = _clean(content)[:500]

        content = _clean(str(content)).strip()
        if not content:
            continue

        if msg_type == "user":
            parts.append(f"User: {content}")
        elif msg_type == "assistant":
            parts.append(f"Assistant: {content[:3000] if rich else content.split(chr(10))[0][:300]}")

    text = "\n\n".join(parts)
    return text[:config.CLASSIFY_MAX_CHARS]


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
