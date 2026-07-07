"""OpenAI-compatible client factory for Azure AI Foundry deployments.

Both gpt-5.1-mini (Azure OpenAI) and gpt-oss-120b (serverless MaaS) speak the
OpenAI chat-completions API. We build a plain `openai.OpenAI` client pointed at the
Foundry endpoint, adding the `api-version` query param when set (Azure OpenAI-style
endpoints need it; the OpenAI-compatible `/openai/v1/` route does not).

Config (in .env), shared across models:
    LAV_FOUNDRY_ENDPOINT      base URL from the deployment's "Target URI"
    LAV_FOUNDRY_KEY           deployment key
    LAV_FOUNDRY_API_VERSION   e.g. 2024-12-01-preview (leave empty for /openai/v1/ route)
    LAV_FOUNDRY_TIMEOUT       per-request timeout in seconds (default 40)
    LAV_FOUNDRY_MAX_RETRIES   SDK retries on timeout/429/5xx (default 3)

Per-model overrides (when a model lives on a different endpoint/key) — suffix the
deployment name uppercased, non-alnum → '_'. For deployment `gpt-oss-120b`:
    LAV_FOUNDRY_ENDPOINT_GPT_OSS_120B
    LAV_FOUNDRY_KEY_GPT_OSS_120B
    LAV_FOUNDRY_API_VERSION_GPT_OSS_120B
"""

import os
import re


def _env_for(deployment: str, suffix: str) -> str:
    slug = re.sub(r"[^A-Z0-9]", "_", deployment.upper())
    return os.getenv(f"LAV_FOUNDRY_{suffix}_{slug}") or os.getenv(f"LAV_FOUNDRY_{suffix}", "")


def make_client(deployment: str):
    """Build an OpenAI-compatible client for a Foundry deployment. Raises if unconfigured."""
    import openai

    endpoint = _env_for(deployment, "ENDPOINT").strip()
    key = _env_for(deployment, "KEY").strip()
    api_version = _env_for(deployment, "API_VERSION").strip()
    if not endpoint or not key:
        raise RuntimeError(
            f"Foundry deployment '{deployment}' not configured — set LAV_FOUNDRY_ENDPOINT "
            f"and LAV_FOUNDRY_KEY (or the per-model *_{re.sub(r'[^A-Z0-9]', '_', deployment.upper())} "
            f"overrides) in .env"
        )
    # Azure calls can hang for many minutes without a client-side deadline; a bulk
    # run must never block on one request. The SDK retries timeouts/429/5xx itself.
    kwargs = {
        "base_url": endpoint,
        "api_key": key,
        "timeout": float(os.getenv("LAV_FOUNDRY_TIMEOUT", "40")),
        "max_retries": int(os.getenv("LAV_FOUNDRY_MAX_RETRIES", "3")),
    }
    if api_version:
        kwargs["default_query"] = {"api-version": api_version}
    return openai.OpenAI(**kwargs)
