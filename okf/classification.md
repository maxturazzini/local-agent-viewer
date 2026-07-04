---
type: concept
title: Interaction Classification Vocabulary
description: The controlled vocabulary for the `classification` field and the rules the classifier uses to assign it.
resource: local-agent-viewer/okf/classification
tags: [lav, classification, taxonomy]
timestamp: 2026-06-19T00:00:00Z
---

# Interaction Classification

`classification` is a single controlled-vocabulary value describing **what the user does** in the interaction — not the surface topic. The defining axis is the user's *action*, which is why reviewing code and writing code land in different classes.

The vocabulary is defined in `lav/config.py` as `CLASSIFICATIONS`. Any model output outside this set is reset to `development` by sanitization.

# Schema

| Value | Assigned when the user is… |
|-------|----------------------------|
| `development` | actively writing, editing, or committing code |
| `meeting` | in meetings, calls, scheduling, role-play conversations, sales simulations |
| `analysis` | reviewing, researching, evaluating, comparing data or options |
| `brainstorm` | generating ideas, planning strategy, creating content (blogs, presentations) |
| `support` | fixing something broken, debugging errors, troubleshooting |
| `learning` | studying, following tutorials, asking how something works |
| `marketing` | doing sales/marketing: outreach, campaigns, product pages, SEO, social media |
| `operations` | doing admin/finance/HR/procurement/invoicing, non-code business workflows |

## Decision rules

- **Editing vs. discussing code is the key cut.** Reviewing or discussing code/architecture *without* editing it is `analysis`; writing new code is `development`.
- The classifier sees primarily the **user's** turns (tool results and most assistant output are stripped before classification — see [enrichment-pipeline.md](enrichment-pipeline.md)), so the class reflects user intent, not assistant behavior.
- Exactly one value is assigned. There is no multi-label; secondary themes live in `topics`.

## Disambiguation examples

| Situation | Class |
|-----------|-------|
| Review a technical spec | `analysis` |
| Plan which courses to sell | `marketing` |
| Analyze a meeting transcript | `analysis` |
| Write a function | `development` |
| Simulate a sales call | `meeting` |
| Draft a campaign email | `marketing` |
| Process an invoice | `operations` |

## Known drift

The `kb_indexer.py` docstring lists only six classes (`development|meeting|analysis|brainstorm|support|learning`), omitting `marketing` and `operations`. The authoritative list is the eight values in `config.py`. Older indexed records may therefore never carry `marketing` or `operations` if they were classified before those values existed.

# Citations

1. `lav/config.py` — `CLASSIFICATIONS`
2. `lav/classifiers/openai_strict.py` — classification rules and examples in the system prompt
