"""Azure AI Foundry classification backend (A/B test, LAV-72).

Isolated from the working openai_strict / ollama_compat paths so we can iterate on
cloud-model quirks without risk. Full single-call classification (all 9 fields,
strict json_schema) — no two-stage, no kNN. Selected via LAV_CLASSIFY_BACKEND=foundry.
"""
