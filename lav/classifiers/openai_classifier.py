"""
OpenAI-based interaction classifier using Structured Outputs.

Schema identical to Qdrant KB for 1:1 comparison, plus `process` field.
"""

import json
from typing import Any, Dict, List, Optional

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

_LANGUAGE_INSTRUCTION = ""
if config.CLASSIFY_LANGUAGE and config.CLASSIFY_LANGUAGE != "en":
    _LANGUAGE_INSTRUCTION = f"""
Write summary, abstract, and process in {config.CLASSIFY_LANGUAGE}. All other fields (topics, people, clients, classification, data_sensitivity, sensitive_data_types) must stay in English."""

SYSTEM_PROMPT = f"""You classify user-AI interactions into structured JSON metadata. All fields are required.

Fields (output in this order):
1. summary: 1 sentence describing what the user did or asked
2. abstract: 2-3 sentences with context, problem, decisions
3. process: concrete workflow name (e.g. "debug deploy pipeline"), empty string if unclear
4. topics: 1-5 specific keywords (e.g. "Azure architecture", "sales pipeline"), avoid generic terms
5. people: third-party names mentioned (exclude the user and the assistant), empty array if none
6. clients: companies or clients mentioned, empty array if none
7. classification: one value from the list below
8. data_sensitivity: one value from the list below
9. sensitive_data_types: relevant types from the list below, empty array if data_sensitivity is "public"

## classification — based on what the user DOES in the conversation

- development: actively writing, editing, or committing code
- analysis: reviewing, researching, evaluating, comparing data or options
- brainstorm: generating ideas, planning strategy, creating content (blogs, presentations)
- meeting: meetings, calls, scheduling, role-play conversations, sales simulations
- support: fixing something broken, debugging errors, troubleshooting
- learning: studying, tutorials, asking how something works

Important: reviewing or discussing code/architecture without editing it = analysis, writing new code = development.

Examples: review a technical spec → analysis | plan courses to sell → brainstorm | analyze a transcript → analysis | write a function → development | simulate a sales call → meeting

## data_sensitivity
- public: generic discussion, no names, no internal details
- internal: internal work, architecture, tools
- confidential: client data, strategies, pricing
- restricted: credentials, tokens, API keys, financial data

Rule: if people is non-empty, data_sensitivity must be at least "internal".

## sensitive_data_types (when applicable)
credentials, api_keys, financial, personal_data, client_strategy, pricing, contracts, internal_architecture, employee_data{_LANGUAGE_INSTRUCTION}"""

# Appended to system prompt when using non-OpenAI endpoints (no json_schema support)
JSON_SCHEMA_INSTRUCTION = """

You MUST respond with ONLY a valid JSON object (no markdown, no explanation, no ```json blocks).
Use exactly this structure:
{
  "summary": "string",
  "abstract": "string",
  "process": "string",
  "topics": ["string"],
  "people": ["string"],
  "clients": ["string"],
  "classification": "development|meeting|analysis|brainstorm|support|learning",
  "data_sensitivity": "public|internal|confidential|restricted",
  "sensitive_data_types": ["string"]
}"""

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
    import re
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


def classify_interaction(
    messages: List[Dict],
    openai_client,
    model: str = "",
) -> Dict[str, Any]:
    """Classify an interaction using OpenAI or any OpenAI-compatible API.

    Uses strict json_schema on OpenAI, falls back to json_object + prompt
    instructions for other endpoints (Ollama, vLLM, Azure, etc.).

    Returns dict with summary, abstract, process, topics, people, clients,
    classification, data_sensitivity, sensitive_data_types.
    """
    model = model or config.CLASSIFY_MODEL
    system_prompt = config.CLASSIFY_SYSTEM_PROMPT or SYSTEM_PROMPT
    use_strict = not config.CLASSIFY_BASE_URL  # strict json_schema only on OpenAI

    text = prepare_messages_for_classification(messages)

    if not text.strip():
        result = dict(_EMPTY_RESULT)
        result["summary"] = "(empty interaction)"
        return result

    if use_strict:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "interaction_metadata",
                "strict": True,
                "schema": CLASSIFICATION_SCHEMA,
            },
        }
    else:
        system_prompt = system_prompt + JSON_SCHEMA_INSTRUCTION
        response_format = {"type": "json_object"}

    response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Classify this interaction:\n\n{text}"},
        ],
        response_format=response_format,
        max_tokens=2000,
    )

    raw = _parse_json_response(response.choices[0].message.content)
    return _sanitize_result(raw)
