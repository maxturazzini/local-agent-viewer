"""
InteractionIndexer - Pipeline for indexing interactions with auto-tagging.

Uses Haiku for fast metadata extraction (summary, classification, topics, etc.)
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Set
import json
import os
import re

import anthropic

from lav import config
from .store import InteractionVectorStore


TAGGING_PROMPT = """Analyze this interaction between a user and an AI assistant and extract structured metadata.

INTERACTION:
{interaction}

Respond ONLY with valid JSON (no text before or after):
{{
  "summary": "1-2 sentence summary of the interaction",
  "abstract": "more detailed 2-3 sentence description of the context, problem addressed, and decisions made",
  "classification": "ONE of: development | meeting | analysis | brainstorm | support | learning",
  "topics": ["topic1", "topic2", "topic3"],
  "people": ["person name if mentioned in the interaction, exclude the user and the assistant"],
  "clients": ["client/company name if mentioned"],
  "data_sensitivity": "ONE of: public | internal | confidential | restricted",
  "sensitive_data_types": ["type1", "type2"]
}}

NOTES:
- classification: development=coding/architecture, meeting=meetings/calls, analysis=data analysis/research, brainstorm=ideation, support=troubleshooting, learning=study/training
- topics: max 5, specific keywords (e.g. "qdrant", "vector-database", not generic like "AI")
- people: only third parties mentioned in the interaction, NOT the user or the assistant
- clients: companies/clients mentioned
- data_sensitivity: public=generic discussions without sensitive data, internal=internal work details/architecture/tools, confidential=client data/commercial strategies/offers/pricing, restricted=credentials/tokens/API keys/personal financial data
- sensitive_data_types: empty list if public, otherwise choose from: credentials, api_keys, financial, personal_data, client_strategy, pricing, contracts, internal_architecture, employee_data
"""


def generate_tags(text: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Generate metadata via Haiku LLM (~1-2 sec)."""
    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("Anthropic API key required. Set ANTHROPIC_API_KEY env var.")

    client = anthropic.Anthropic(api_key=key)
    text_truncated = text[:8000]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": TAGGING_PROMPT.format(interaction=text_truncated)
        }]
    )

    response_text = response.content[0].text.strip()

    json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response_text)
    if json_match:
        response_text = json_match.group(1)

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        result = {
            "summary": "Interaction could not be analyzed",
            "abstract": "",
            "classification": "development",
            "topics": [],
            "people": [],
            "clients": []
        }

    defaults = {
        "summary": "",
        "abstract": "",
        "classification": "development",
        "topics": [],
        "people": [],
        "clients": [],
        "data_sensitivity": "internal",
        "sensitive_data_types": []
    }
    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    return result


class InteractionIndexer:
    """Pipeline: messages -> payload -> embedding -> Qdrant."""

    VALID_CLASSIFICATIONS = set(config.CLASSIFICATIONS)
    VALID_SENSITIVITIES = set(config.SENSITIVITIES)

    def __init__(
        self,
        store: InteractionVectorStore,
        anthropic_api_key: Optional[str] = None
    ):
        self.store = store
        self.anthropic_api_key = anthropic_api_key

    def index(
        self,
        session_id: str,
        messages: List[Dict],
        project: str,
        timestamp: str,
        user: str = "",
        custom_tags: Optional[List[str]] = None,
        pre_metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Index an interaction."""
        full_text = self._messages_to_text(messages)

        if pre_metadata:
            auto_meta = pre_metadata
        else:
            auto_meta = generate_tags(full_text, self.anthropic_api_key)

        classification = auto_meta.get("classification", "development")
        if classification not in self.VALID_CLASSIFICATIONS:
            classification = "development"

        sensitivity = auto_meta.get("data_sensitivity", "internal")
        if sensitivity not in self.VALID_SENSITIVITIES:
            sensitivity = "internal"

        tools_used = self._extract_tools(messages)

        payload = {
            "session_id": session_id,
            "project": project,
            "user": user,
            "timestamp": timestamp,
            "indexed_at": datetime.now().isoformat(),
            "summary": auto_meta.get("summary", ""),
            "abstract": auto_meta.get("abstract", ""),
            "classification": classification,
            "data_sensitivity": sensitivity,
            "sensitive_data_types": auto_meta.get("sensitive_data_types", []),
            "topics": auto_meta.get("topics", [])[:5],
            "people": auto_meta.get("people", []),
            "clients": auto_meta.get("clients", []),
            "tags": custom_tags or [],
            "message_count": len(messages),
            "tools_used": list(tools_used)
        }

        vector = self.store.embed(full_text)
        self.store.upsert(session_id, vector, payload)

        return payload

    def reindex(
        self,
        session_id: str,
        messages: List[Dict],
        project: str,
        timestamp: str,
        user: str = "",
        preserve_tags: bool = True
    ) -> Dict[str, Any]:
        """Re-index an interaction, optionally preserving manual tags."""
        existing_tags = []
        if preserve_tags:
            existing = self.store.get(session_id)
            if existing:
                existing_tags = existing.get("tags", [])

        return self.index(
            session_id=session_id,
            messages=messages,
            project=project,
            timestamp=timestamp,
            user=user,
            custom_tags=existing_tags
        )

    def _messages_to_text(self, messages: List[Dict]) -> str:
        """Convert messages to text for embedding."""
        parts = []
        for msg in messages:
            role = "User" if msg.get("type") == "user" else "Assistant"
            content = msg.get("content", "")

            if isinstance(content, list):
                content = " ".join(str(c) for c in content)

            content = str(content)[:2000]

            if content.strip():
                parts.append(f"{role}: {content}")

        return "\n\n".join(parts)

    def _extract_tools(self, messages: List[Dict]) -> Set[str]:
        """Extract tool names used in interaction."""
        tools = set()

        for msg in messages:
            tool_calls = msg.get("tool_calls", [])
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if isinstance(call, dict):
                        tool_name = call.get("name") or call.get("tool")
                        if tool_name:
                            clean_name = tool_name.split("__")[-1] if "__" in tool_name else tool_name
                            tools.add(clean_name)

            content = msg.get("content", "")
            if isinstance(content, str):
                tool_patterns = [
                    r"Using (\w+) tool",
                    r"<invoke name=\"(\w+)\"",
                    r"Tool: (\w+)",
                ]
                for pattern in tool_patterns:
                    matches = re.findall(pattern, content)
                    tools.update(matches)

        return tools
