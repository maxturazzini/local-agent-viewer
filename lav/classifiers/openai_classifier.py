"""
Shared classification utilities and dispatcher.

Schema, sanitization, JSON parsing, message preparation are shared across
both classification backends (openai_strict, ollama_compat).
The classify_interaction() dispatcher routes to the correct backend.
"""

import json
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


def prepare_messages_for_classification(messages: List[Dict]) -> str:
    """Extract user intent messages for classification. Skips tool results and
    assistant responses to focus on what the user asked/did."""
    parts = []
    for msg in messages:
        msg_type = msg.get("type", "")
        content = msg.get("content", "")

        if isinstance(content, list):
            # Check if this is a tool_result (noise for classification)
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        text_parts.append(f"[tool: {block.get('name', '')}]")
                elif isinstance(block, str):
                    text_parts.append(block)
            content = " ".join(text_parts)
        elif isinstance(content, str) and content.lstrip().startswith("[{") and "tool_result" in content[:200]:
            # tool_result stored as JSON string
            continue

        content = str(content).strip()
        if not content:
            continue

        if msg_type == "user":
            parts.append(f"User: {content}")
        elif msg_type == "assistant":
            first_line = content.split("\n")[0][:300]
            parts.append(f"Assistant: {first_line}")

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
) -> Dict[str, Any]:
    """Classify an interaction, dispatching to the correct backend.

    Backend is determined by LAV_CLASSIFY_BACKEND (auto/openai/ollama).
    When auto: uses openai if no BASE_URL, ollama otherwise.
    """
    backend = _resolve_backend()
    if backend == "openai":
        from lav.classifiers.openai_strict import classify
    else:
        from lav.classifiers.ollama_compat import classify
    return classify(messages, openai_client, model)
