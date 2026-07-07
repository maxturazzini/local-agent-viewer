"""
OpenAI strict JSON schema classification path.

Uses response_format with json_schema strict mode (OpenAI native).
System prompt with full instructions + language instruction.
"""

from typing import Any, Dict, List

from lav import config, taxonomy
from lav.classifiers.openai_classifier import (
    CLASSIFICATION_SCHEMA,
    _EMPTY_RESULT,
    _parse_json_response,
    _sanitize_result,
    prepare_messages_for_classification,
)

_LANGUAGE_INSTRUCTION = ""
if config.CLASSIFY_LANGUAGE and config.CLASSIFY_LANGUAGE != "en":
    _LANGUAGE_INSTRUCTION = f"""
Write summary, abstract, and process in {config.CLASSIFY_LANGUAGE}. All other fields (topics, people, clients, classification, data_sensitivity, sensitive_data_types) must stay in English."""

SYSTEM_PROMPT = f"""You classify user-AI interactions into structured JSON metadata. All fields are required.

Context — {taxonomy.USER_CONTEXT}

{taxonomy.FIELDS_INSTRUCTION}
{taxonomy.fields_block(numbered=True)}

{taxonomy.FIELDS_ENTITIES_NOTE}

## {taxonomy.CLASSIFICATION_HEADER}

{taxonomy.classification_block()}

Important: {taxonomy.CLASSIFICATION_GUIDANCE}

Rules:
{taxonomy.classification_rules_block()}

Examples: {taxonomy.CLASSIFICATION_EXAMPLES}

## data_sensitivity
{taxonomy.sensitivity_block()}

Rules:
{taxonomy.sensitivity_rules_block()}

## sensitive_data_types (when applicable)
{taxonomy.sensitive_data_types_line()}{_LANGUAGE_INSTRUCTION}"""


def classify(
    messages: List[Dict],
    openai_client,
    model: str = "",
) -> Dict[str, Any]:
    """Classify using OpenAI strict json_schema mode."""
    model = model or config.CLASSIFY_MODEL
    system_prompt = config.CLASSIFY_SYSTEM_PROMPT or SYSTEM_PROMPT

    text = prepare_messages_for_classification(messages)

    if not text.strip():
        result = dict(_EMPTY_RESULT)
        result["summary"] = "(empty interaction)"
        return result

    # Newer OpenAI models require max_completion_tokens instead of max_tokens
    _new_style = model.startswith(("gpt-5", "o3-", "o4-"))
    token_kwarg = {"max_completion_tokens": 2000} if _new_style else {"max_tokens": 2000}

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "interaction_metadata",
            "strict": True,
            "schema": CLASSIFICATION_SCHEMA,
        },
    }

    response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Classify this interaction:\n\n{text}"},
        ],
        response_format=response_format,
        **token_kwarg,
    )

    raw = _parse_json_response(response.choices[0].message.content)
    return _sanitize_result(raw)
