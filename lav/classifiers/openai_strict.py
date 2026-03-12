"""
OpenAI strict JSON schema classification path.

Uses response_format with json_schema strict mode (OpenAI native).
System prompt with full instructions + language instruction.
"""

from typing import Any, Dict, List

from lav import config
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
- marketing: sales activities, marketing content, outreach, campaigns, product pages, SEO, social media
- operations: admin, finance, HR, procurement, invoicing, non-code business workflows

Important: reviewing or discussing code/architecture without editing it = analysis, writing new code = development.

Examples: review a technical spec → analysis | plan courses to sell → marketing | analyze a transcript → analysis | write a function → development | simulate a sales call → meeting | draft a campaign email → marketing | process an invoice → operations

## data_sensitivity
- public: generic discussion, no names, no internal details
- internal: internal work, architecture, tools
- confidential: client data, strategies, pricing
- restricted: credentials, tokens, API keys, financial data

Rule: if people is non-empty, data_sensitivity must be at least "internal".

## sensitive_data_types (when applicable)
credentials, api_keys, financial, personal_data, client_strategy, pricing, contracts, internal_architecture, employee_data{_LANGUAGE_INSTRUCTION}"""


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
