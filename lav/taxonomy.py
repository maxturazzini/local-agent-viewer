"""
Single source of truth for the classification taxonomy.

Loads ``lav/taxonomy.json`` with the stdlib ``json`` module — LAV core stays
zero-dependency (PyYAML is deliberately avoided). Exposes the value lists (the
enums used by ``config`` and the classification JSON schema) plus renderers that
build the taxonomy section of the classifier prompts, so category names and
descriptions live in exactly one place.

Consumers:
- ``lav/config.py`` — ``CLASSIFICATIONS`` / ``SENSITIVITIES``
- ``lav/classifiers/openai_strict.py`` / ``ollama_compat.py`` — prompt blocks
- ``internal_docs/annotate_server.py`` — golden-set editor tooltips
"""
import json
import os
from pathlib import Path

# The real taxonomy (lav/taxonomy.json) carries the user's context + labeling rules
# and is PRIVATE (gitignored). Only the public lav/taxonomy.example.json is committed.
# Prefer the real one when present, else fall back to the example so a fresh clone works.
# INVARIANT: taxonomy.example.json must keep the same category/sensitivity *names* as the
# real file — they are the schema enum / DB values; only context + rules may differ.
_DIR = Path(__file__).resolve().parent
_REAL = _DIR / "taxonomy.json"
_EXAMPLE = _DIR / "taxonomy.example.json"
# LAV_TAXONOMY_FILE lets an eval A/B different taxonomy versions without editing code
# (e.g. taxonomy_v1.json = the pre-rules prompt). Value = filename in lav/ or a full path.
_OVERRIDE = os.getenv("LAV_TAXONOMY_FILE")
if _OVERRIDE:
    _PATH = Path(_OVERRIDE) if Path(_OVERRIDE).is_absolute() else _DIR / _OVERRIDE
else:
    _PATH = _REAL if _REAL.exists() else _EXAMPLE

with _PATH.open(encoding="utf-8") as _f:
    _DATA = json.load(_f)

_CLS = _DATA["classification"]
_SENS = _DATA["data_sensitivity"]

# Enums (order = prompt presentation order; the enum is a set, order is cosmetic)
CLASSIFICATIONS = [v["name"] for v in _CLS["values"]]
SENSITIVITIES = [v["name"] for v in _SENS["values"]]
SENSITIVE_DATA_TYPES = list(_DATA["sensitive_data_types"])

# Descriptions (name -> description)
CLASSIFICATION_DESCRIPTIONS = {v["name"]: v["description"] for v in _CLS["values"]}
SENSITIVITY_DESCRIPTIONS = {v["name"]: v["description"] for v in _SENS["values"]}

# User context — a short identity blurb that helps disambiguate (e.g. workshop
# demos, code for self/clients). Kept intentionally light.
USER_CONTEXT = _DATA.get("context", {}).get("user", "")

# Output fields (summary/abstract/process/topics/people/clients + the label fields).
# Centralized so people/clients descriptions live in ONE place and can't drift
# between the openai_strict and ollama_compat prompts.
_FIELDS = _DATA.get("fields", {})
FIELDS_INSTRUCTION = _FIELDS.get("instruction", "")
FIELDS_ITEMS = list(_FIELDS.get("items", []))
FIELDS_ENTITIES_NOTE = _FIELDS.get("entities_note", "")
_FIELDS_SAMPLE = _FIELDS.get("sample_json")

CLASSIFICATION_HEADER = _CLS.get("header", "")
CLASSIFICATION_GUIDANCE = _CLS.get("guidance", "")
CLASSIFICATION_EXAMPLES = _CLS.get("examples", "")
CLASSIFICATION_RULES = list(_CLS.get("rules", []))
SENSITIVITY_RULES = list(_SENS.get("rules", []))


def classification_block() -> str:
    """Render '- name: description' lines for the classifier prompts."""
    return "\n".join(f"- {v['name']}: {v['description']}" for v in _CLS["values"])


def sensitivity_block() -> str:
    """Render '- name: description' lines for data_sensitivity."""
    return "\n".join(f"- {v['name']}: {v['description']}" for v in _SENS["values"])


def classification_rules_block() -> str:
    """Render classification disambiguation rules as '- rule' lines."""
    return "\n".join(f"- {r}" for r in CLASSIFICATION_RULES)


def sensitivity_rules_block() -> str:
    """Render data_sensitivity rules as '- rule' lines."""
    return "\n".join(f"- {r}" for r in SENSITIVITY_RULES)


def sensitive_data_types_line() -> str:
    return ", ".join(SENSITIVE_DATA_TYPES)


def fields_block(numbered: bool = True) -> str:
    """Render the output-fields list. numbered=True → '1. name: desc' (openai_strict),
    numbered=False → '- name: desc' (ollama_compat)."""
    out = []
    for i, f in enumerate(FIELDS_ITEMS, 1):
        prefix = f"{i}. " if numbered else "- "
        out.append(f"{prefix}{f['name']}: {f['desc']}")
    return "\n".join(out)


def sample_json_str() -> str:
    """Compact JSON example for example-based (Ollama) prompting."""
    return json.dumps(_FIELDS_SAMPLE, ensure_ascii=False) if _FIELDS_SAMPLE else ""
