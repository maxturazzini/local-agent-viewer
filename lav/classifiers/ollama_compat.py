"""
Ollama/vLLM/OpenAI-compatible endpoint classification path.

Uses example-based prompting (no json_schema support on these endpoints).
Language instruction embedded in user message. Always uses max_tokens.
Strips thinking tags (Qwen, DeepSeek) via shared _parse_json_response.
"""

from typing import Any, Dict, List

from lav import config
from lav.classifiers.openai_classifier import (
    _EMPTY_RESULT,
    _parse_json_response,
    _sanitize_result,
    prepare_messages_for_classification,
)

_LANGUAGE_INSTRUCTION = ""
if config.CLASSIFY_LANGUAGE and config.CLASSIFY_LANGUAGE != "en":
    _LANGUAGE_INSTRUCTION = (
        f"\nIMPORTANT: Write summary, abstract, and process in {config.CLASSIFY_LANGUAGE}. "
        f"All other fields (topics, people, clients, classification, data_sensitivity, sensitive_data_types) must stay in English.\n"
    )

_SAMPLE_JSON = (
    '{"classification": "support", '
    '"data_sensitivity": "internal", '
    '"summary": "User debugged a Python import error", '
    '"abstract": "User encountered a ModuleNotFoundError. The fix was reinstalling the package.", '
    '"process": "debug python dependency", '
    '"topics": ["python", "import", "debugging"], '
    '"people": [], "clients": [], '
    '"sensitive_data_types": []}'
)

_SYSTEM_PROMPT = """You classify user-AI interactions into structured JSON metadata.

## classification values (pick ONE based on what the user DOES)
- development: actively writing, editing, or committing code
- analysis: reviewing, researching, evaluating, comparing data or options
- brainstorm: generating ideas, planning strategy, creating content
- meeting: meetings, calls, scheduling, role-play conversations
- support: fixing something broken, debugging errors, troubleshooting
- learning: studying, tutorials, asking how something works
- marketing: sales, marketing content, outreach, campaigns, product pages, SEO
- operations: admin, finance, HR, procurement, invoicing, non-code business workflows

## data_sensitivity values (pick ONE)
- public: generic discussion, no names, no internal details
- internal: internal work, architecture, tools
- confidential: client data, strategies, pricing
- restricted: credentials, tokens, API keys, financial data

Rule: if people is non-empty, data_sensitivity must be at least "internal".

Respond with ONLY a valid JSON object. No markdown, no explanation, no ```json blocks."""


def classify(
    messages: List[Dict],
    openai_client,
    model: str = "",
) -> Dict[str, Any]:
    """Classify using example-based prompting for Ollama/vLLM/compatible endpoints."""
    model = model or config.CLASSIFY_MODEL
    system_prompt = config.CLASSIFY_SYSTEM_PROMPT or _SYSTEM_PROMPT

    text = prepare_messages_for_classification(messages)

    if not text.strip():
        result = dict(_EMPTY_RESULT)
        result["summary"] = "(empty interaction)"
        return result

    user_content = (
        f"Classify this interaction and return ONLY a JSON object (no markdown, no explanation).\n"
        f"{_LANGUAGE_INSTRUCTION}\n"
        f"Interaction:\n{text}\n\n"
        f"Return JSON exactly like this example:\n{_SAMPLE_JSON}\n\n"
        f"Your JSON:"
    )

    response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        max_tokens=2000,
    )

    raw = _parse_json_response(response.choices[0].message.content)
    return _sanitize_result(raw)
