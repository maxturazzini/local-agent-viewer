"""
OpenAI-based interaction classifier using Structured Outputs.

Schema identical to Qdrant KB for 1:1 comparison, plus `process` field.
"""

import json
from typing import Any, Dict, List, Optional

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
        "classification": {
            "type": "string",
            "enum": ["development", "meeting", "analysis", "brainstorm", "support", "learning"],
            "description": "Primary classification of the interaction"
        },
        "data_sensitivity": {
            "type": "string",
            "enum": ["public", "internal", "confidential", "restricted"],
            "description": "Data sensitivity level. If third parties are mentioned, at least internal."
        },
        "sensitive_data_types": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Types of sensitive data present (empty if public)"
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
    },
    "required": [
        "summary", "abstract", "process", "classification", "data_sensitivity",
        "sensitive_data_types", "topics", "people", "clients"
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are an interaction classifier for interactions between users and AI assistants.
Analyze the messages and produce structured metadata.

Fields:
- summary: 1-sentence summary
- abstract: context, problem addressed, and decisions made (2-3 sentences)
- process: the workflow the user is executing, inferred from questions and actions. Describe as a concrete activity (e.g. "debug deploy pipeline", "client workshop preparation", "competitor research for commercial proposal", "DB architecture refactoring"). Empty string if not inferable.
- classification: development | meeting | analysis | brainstorm | support | learning
- data_sensitivity: public | internal | confidential | restricted
- sensitive_data_types: list of sensitive types (empty if public)
- topics: max 5 specific keywords (not generic like "AI" or "coding")
- people: third parties mentioned (not the user or the assistant)
- clients: companies/clients mentioned

Classification definitions:
  development = coding, architecture, system configuration
  meeting = meetings, calls, organizational discussions
  analysis = data analysis, research, evaluations
  brainstorm = ideation, idea exploration, strategy
  support = troubleshooting, fixes, technical assistance
  learning = study, training, education

Data sensitivity definitions:
  public = generic discussions without sensitive data and without people names
  internal = internal work details, architecture, tools
  confidential = client data, commercial strategies, offers, pricing
  restricted = credentials, tokens, API keys, personal financial data

CRITICAL RULE: if third-party names appear in the text (people field not empty), data_sensitivity must be AT LEAST internal, never public.

sensitive_data_types (if not public): credentials, api_keys, financial, personal_data, client_strategy, pricing, contracts, internal_architecture, employee_data"""


def prepare_messages_for_classification(messages: List[Dict]) -> str:
    """Filter: user messages + first line of each assistant message for classification. Truncate to 6000 chars."""
    parts = []
    for msg in messages:
        msg_type = msg.get("type", "")
        content = msg.get("content", "")

        if isinstance(content, list):
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

        content = str(content).strip()
        if not content:
            continue

        if msg_type == "user":
            parts.append(f"User: {content}")
        elif msg_type == "assistant":
            first_line = content.split("\n")[0][:500]
            parts.append(f"Assistant: {first_line}")

    text = "\n\n".join(parts)
    return text[:6000]


def classify_interaction(
    messages: List[Dict],
    openai_client,
    model: str = "gpt-4.1-mini",
) -> Dict[str, Any]:
    """Classify an interaction using OpenAI Structured Outputs.

    Returns dict with summary, abstract, process, classification,
    data_sensitivity, sensitive_data_types, topics, people, clients.
    """
    text = prepare_messages_for_classification(messages)

    if not text.strip():
        return {
            "summary": "(empty interaction)",
            "abstract": "",
            "process": "",
            "classification": "development",
            "data_sensitivity": "internal",
            "sensitive_data_types": [],
            "topics": [],
            "people": [],
            "clients": [],
        }

    response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Classify this interaction:\n\n{text}"},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "interaction_metadata",
                "strict": True,
                "schema": CLASSIFICATION_SCHEMA,
            },
        },
        max_tokens=500,
    )

    result = json.loads(response.choices[0].message.content)

    # Enforce limits
    result["topics"] = result.get("topics", [])[:5]

    # Enforce rule: people present → at least internal
    if result.get("people") and result.get("data_sensitivity") == "public":
        result["data_sensitivity"] = "internal"

    return result
