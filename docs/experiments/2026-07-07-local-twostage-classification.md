# Failed experiment — local two-stage classification on gemma (M4)

**Date:** 2026-07-06 → 2026-07-07
**Status:** ❌ Failed (not viable for the use case) — kept for the record.
**Tickets:** LAV-70 / LAV-71 (classification quality).

## Goal

Classify ~17,000 stored agent interactions into `classification` (1 of 9) +
`data_sensitivity` (1 of 4) **locally** on the minimacs M4 (Ollama, no API cost,
data stays private), reaching at least the gpt-4.1-mini quality baseline
(classification ~59% / sensitivity ~36% vs a 98-row human gold set).

## Why it's marked failed

The two-stage approach **reached the accuracy goal but failed on throughput.**
It is not the model or the accuracy that killed it — it's latency: ~90s per
interaction, so a bulk backfill of 17k interactions would take **~18 days**
single-threaded. For the intended one-time backfill that is impractical. Local
gemma two-stage is therefore **not deployable at scale** for this use case. The
direction is shifting to a cloud endpoint (Azure AI Foundry backend added).

## What we tried, in order

| # | Lever | Result (vs 98-row gold) |
|---|-------|-------------------------|
| 1 | Baseline: single-call, 9-field strict JSON, full rubric | gemma **10-20%** cls / 15-35% sens — collapses onto ~3 categories |
| 2 | Bigger/cleaner input (12k→60k chars) + temperature 0 | No change. Not an input/sampling problem. |
| 3 | Model swap — QAT quant (`gemma4:e4b-it-qat`) and a different family (`qwen3.5:4b`) | All **~10%** cls. Not quantization, not model family. |
| 4 | **kNN-vote** (no LLM): embed gold, leave-one-out majority vote | **43%** cls (k=5) / **51%** sens (k=1). Retrieval alone beats every local single-call model, and beats gpt-4.1-mini on sensitivity. |
| 5 | **Two-stage**: Stage 1 = tiny enum-constrained JSON `{reasoning, classification, data_sensitivity}` + kNN few-shot; Stage 2 = descriptive fields from the base call | **55%** cls (F1 0.43) / **50%** sens (F1 0.50), parse_ok 85% |

Reference: gpt-4.1-mini (cloud) = **59% / 36%**.

So the two-stage local gemma **matched gpt-4.1-mini on classification and beat it
by 14 points on sensitivity** — a genuine accuracy success.

## Why the single-call collapses (the real finding)

The collapse was **architectural, not model capacity.** Asking a small model to,
in one constrained generation: apply a ~16-rule rubric + pick 1-of-9 + pick 1-of-4
+ emit strict 9-field JSON — overloads it, and it falls back to majority-label
priors (3 easy categories). Decomposing the work fixes it: the *same* gemma goes
10% → 55% once reasoning and formatting are split and the label space is small and
enum-constrained. Constrained decoding alone does **not** fix it (labels were
already in-enum, just wrong) — it fixes format, not choice.

## Why it fails operationally (measured, per interaction, on M4)

Two-stage makes 3 calls per interaction:

| Call | Prompt tokens | Completion tokens | Time |
|------|---------------|-------------------|------|
| Base descriptive (single-call, 9-field) | ~2,000–4,000 | 65–1,050 | 12–50s |
| Embed (kNN, nomic-embed-text) | ~2,000 | — | ~1s |
| Stage 1 (enum-constrained) | ~2,500–3,100 | 90–600 | 27–60s |
| **Total** | **~7,000–11,000** | **~100–1,300** | **~73–110s (median ~90s)** |

- **API cost: $0** (local Ollama). Equivalent token volume on gpt-4.1-mini cloud
  would be ~$0.005/interaction → ~$85 for 17k.
- **Throughput: the blocker.** ~90s × 17,000 ≈ **18 days** serial. On-demand /
  incremental classification (per new interaction) is fine; the one-time bulk
  backfill is not.

Two gotchas found while building (documented so we don't relearn them):
1. Constrained decoding (`json_schema`) on gemma via Ollama returns an **empty
   completion on long prompts** (~>8-10k chars) — root cause is `num_ctx` default
   4096 overflowing. Mitigated by truncating Stage 1 input; the real fix is raising
   `num_ctx` + retry on empty (see lessons).
2. `format:json` on Ollama constrains JSON *syntax only*, not enum *values* — the
   real enum constraint needs a strict `json_schema` (or grammar) call.

## Follow-up: 3-task decomposed benchmark (isolate WHERE gemma fails)

Ran gemma4:latest as three SEPARATE single-call prompts — one per capability — to
see if the failure is one specific sub-task (full report:
`internal_docs/bench_decomposed_report.md`, private). Gold = first 20 rows;
Task 2 quantitative (n=17), Tasks 1 & 3 qualitative (n=6, sonnet judge).

| Task | Metric | Verdict |
|------|--------|---------|
| 1 · SUMMARY | faithfulness **4.0/5** | gemma **OK** (hallucinates only on truncated long inputs) |
| 2 · CONSTRAINED TAG | class **41%** (F1 0.31) / sens **53%** (F1 0.42) | **weak, but 2–3.5× better isolated** than the 9-field call (10-20% / 15%) |
| 3 · FREE TAG | people+clients precision **79%** (21% hallucination) | gemma **quasi-OK** (breaks on dense/tabular data) |

**Confirmed:** gemma does NOT fail everywhere. It summarizes and extracts entities
decently; the weak spot is constrained tagging — and even that **jumps 2-3.5×
when isolated**, so the "collapse to 3 labels" was largely a **multitasking**
effect, not incapacity. Decomposition recovers quality. But it needs **3 calls**,
so it makes the latency wall *worse*, not better — quality win, not an operational
one. Local bulk backfill stays impractical; the cloud pivot holds.

Residual gemma tagging biases (attenuated, not gone): classification uses
`analysis` as a dumping ground (predicts only 5/9 classes); data_sensitivity
under-escalates and **never predicts `restricted`** — the costly miss. For
sensitivity, kNN or a rules floor is safer than gemma alone.

## What we keep vs what rolls back

**KEEP (benchmark infrastructure — not the experiment):**
- `tests/evals/eval_classify.py` `--gold` mode (P/R/F1 vs human gold).
- `internal_docs/golden_set_v2.csv` (98-row human gold) and the annotation tooling.
- `lav/taxonomy.*` centralization (single source of truth for labels + rules).
- `internal_docs/knn_probe.py` (self-contained kNN-vote measurement; the 43%/51%
  baseline is reproducible from it).
- The classifier input cleaning + `LAV_CLASSIFY_RICH/MAX_CHARS/TEMPERATURE` levers.

**ROLLED BACK (the failed classifier):**
- `lav/classifiers/twostage.py`, `lav/classifiers/knn_examples.py`, the `twostage`
  dispatch branch, `internal_docs/oos_sample.py`, and the minimacs `knn_index.json`.

## Lessons

1. The bottleneck was never the model — it was (a) an inconsistent gold set
   earlier (fixed: golden_set_v2), then (b) the single-call **multitasking**
   architecture (one call doing summary + 2 tags + 4 free fields + strict JSON).
2. A small local model **can** hit cloud-level accuracy on this task with the right
   decomposition — but not at a throughput that makes local bulk backfill practical
   (2-stage ~90s/interaction; 3-task decomposed is worse).
3. Per-capability: gemma is fine at **summary** and **entity extraction**; only
   **constrained tagging** is weak. A future hybrid could do summary+entities
   locally and route only the tags to kNN or a cloud model.
4. **kNN retrieval carries real signal** (43%/51% with zero LLM), and beats gemma
   and gpt-4.1-mini on sensitivity. If we ever want a fast local pass, embed-and-vote
   is the cheapest thing that works.
5. **Infra — Ollama `num_ctx`**: the empty-completion failures were a default
   `num_ctx=4096` overflow (rubric + long user text), NOT just "long input". Real
   fix = raise `num_ctx` (16k–32k) + retry on empty; truncating input is only a
   band-aid. (gemma4 still returns empty on a few even at 32k — a model quirk.)
   This applies to ANY local Ollama classification path.
6. Next direction: a cloud endpoint (Azure AI Foundry) for the bulk pass, keeping
   the same benchmark harness to score it against the gold set.
