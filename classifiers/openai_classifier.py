"""
OpenAI-based conversation classifier using Structured Outputs.

Schema identical to Qdrant KB for 1:1 comparison, plus `process` field.
"""

import json
from typing import Any, Dict, List, Optional

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Riassunto in 1 frase della conversazione"
        },
        "abstract": {
            "type": "string",
            "description": "Contesto, problema affrontato e decisioni prese (2-3 frasi)"
        },
        "process": {
            "type": "string",
            "description": "Flusso di lavoro o processo eseguito dall'utente, inferito dalle domande e azioni. Descrivilo come attivita concreta (es. 'debug deploy pipeline', 'preparazione workshop cliente'). Stringa vuota se non inferibile."
        },
        "classification": {
            "type": "string",
            "enum": ["development", "meeting", "analysis", "brainstorm", "support", "learning"],
            "description": "Classificazione principale della conversazione"
        },
        "data_sensitivity": {
            "type": "string",
            "enum": ["public", "internal", "confidential", "restricted"],
            "description": "Livello di sensibilita dei dati. Se sono menzionate persone terze, almeno internal."
        },
        "sensitive_data_types": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tipi di dati sensibili presenti (vuota se public)"
        },
        "topics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Fino a 5 keyword specifiche"
        },
        "people": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Persone terze menzionate (non l'utente o l'assistente)"
        },
        "clients": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Aziende/clienti menzionati"
        },
    },
    "required": [
        "summary", "abstract", "process", "classification", "data_sensitivity",
        "sensitive_data_types", "topics", "people", "clients"
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Sei un classificatore di conversazioni tra utenti e assistenti AI.
Analizza i messaggi e produci metadati strutturati.

Campi:
- summary: riassunto in 1 frase
- abstract: contesto, problema affrontato e decisioni prese (2-3 frasi)
- process: il flusso di lavoro che l'utente sta eseguendo, inferito dalle domande e azioni. Descrivilo come attivita concreta (es. "debug deploy pipeline", "preparazione workshop cliente", "ricerca competitor per proposta commerciale", "refactoring architettura DB"). Stringa vuota se non inferibile.
- classification: development | meeting | analysis | brainstorm | support | learning
- data_sensitivity: public | internal | confidential | restricted
- sensitive_data_types: lista tipi sensibili (vuota se public)
- topics: max 5 keyword specifiche (no generiche come "AI" o "coding")
- people: persone terze menzionate (non l'utente o l'assistente)
- clients: aziende/clienti menzionati

Definizioni classification:
  development = coding, architettura, configurazione sistemi
  meeting = riunioni, call, discussioni organizzative
  analysis = analisi dati, ricerca, valutazioni
  brainstorm = ideazione, esplorazione idee, strategia
  support = troubleshooting, fix, assistenza tecnica
  learning = studio, formazione, apprendimento

Definizioni data_sensitivity:
  public = discussioni generiche senza dati sensibili e senza nomi di persone
  internal = dettagli interni di lavoro, architettura, tool
  confidential = dati clienti, strategie commerciali, offerte, prezzi
  restricted = credenziali, token, API key, dati finanziari personali

REGOLA CRITICA: se nel testo compaiono nomi di persone terze (campo people non vuoto), data_sensitivity e ALMENO internal, mai public.

sensitive_data_types (se non public): credentials, api_keys, financial, personal_data, client_strategy, pricing, contracts, internal_architecture, employee_data"""


def prepare_messages_for_classification(messages: List[Dict]) -> str:
    """Filter: user messages + first line of each assistant message. Truncate to 6000 chars."""
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


def classify_conversation(
    messages: List[Dict],
    openai_client,
    model: str = "gpt-4.1-mini",
) -> Dict[str, Any]:
    """Classify a conversation using OpenAI Structured Outputs.

    Returns dict with summary, abstract, process, classification,
    data_sensitivity, sensitive_data_types, topics, people, clients.
    """
    text = prepare_messages_for_classification(messages)

    if not text.strip():
        return {
            "summary": "(empty conversation)",
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
            {"role": "user", "content": f"Classifica questa conversazione:\n\n{text}"},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "conversation_metadata",
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
