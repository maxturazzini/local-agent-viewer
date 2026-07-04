---
type: concept
title: Data Sensitivity Vocabulary
description: The `data_sensitivity` levels, the `sensitive_data_types` categories, and the auto-escalation rule.
resource: local-agent-viewer/okf/sensitivity
tags: [lav, sensitivity, privacy, taxonomy]
timestamp: 2026-06-19T00:00:00Z
---

# Data Sensitivity

Two fields describe how sensitive an interaction's content is:

- `data_sensitivity` — a single ordered level.
- `sensitive_data_types` — zero or more categories explaining *why* it is sensitive.

These let downstream consumers filter or redact (e.g. exclude `restricted` interactions from a shared export).

# Schema

## `data_sensitivity` (ordered levels)

Defined in `lav/config.py` as `SENSITIVITIES`. Out-of-vocabulary values reset to `internal`.

| Level | Meaning |
|-------|---------|
| `public` | Generic discussion. No names, no internal details. |
| `internal` | Internal work: architecture, tooling, process. |
| `confidential` | Client data, strategies, pricing. |
| `restricted` | Credentials, tokens, API keys, financial data. |

The levels are ordered: `public < internal < confidential < restricted`.

## `sensitive_data_types` (categories)

Free-form array drawn from this controlled set. Empty when `data_sensitivity = public`.

| Category | Covers |
|----------|--------|
| `credentials` | Passwords, login secrets |
| `api_keys` | API keys, tokens |
| `financial` | Financial figures, accounts, payment data |
| `personal_data` | PII of individuals |
| `client_strategy` | Client-specific strategic information |
| `pricing` | Pricing, quotes, rate cards |
| `contracts` | Contractual terms |
| `internal_architecture` | Internal systems, tooling, infrastructure design |
| `employee_data` | HR / employee information |

## Auto-escalation rule

Enforced in both the classifier prompt and `_sanitize_result`:

> If `people` is non-empty, `data_sensitivity` must be at least `internal`.

A record naming a third party can never remain `public`; sanitization silently lifts it to `internal` even if the model returned `public`. This is a floor, not a ceiling — the model may already have assigned a higher level.

# Examples

A session referencing client strategy and internal tooling:

```json
{
  "data_sensitivity": "internal",
  "sensitive_data_types": ["client_strategy", "internal_architecture"]
}
```

A generic how-to question with no names:

```json
{
  "data_sensitivity": "public",
  "sensitive_data_types": []
}
```

# Citations

1. `lav/config.py` — `SENSITIVITIES`
2. `lav/classifiers/openai_strict.py` — sensitivity definitions, types, and the escalation rule
3. `lav/classifiers/openai_classifier.py` — `_sanitize_result` escalation enforcement
