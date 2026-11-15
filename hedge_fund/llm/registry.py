"""Which models exist, who serves them, and what key each one needs.

Sourced from ``api_models.json`` next to this module — one list, so the
picker and the clients cannot drift apart.

This lives in ``hedge_fund/llm`` and not ``hedge_fund/tui`` because it states facts about
providers, not about presentation — the TUI imports it, never the reverse.
(It also has to: ``hedge_fund.signals`` needs a client, and routing that through the
TUI package would close an import cycle.)
"""

from __future__ import annotations

import json
from pathlib import Path

API_MODELS_PATH = Path(__file__).resolve().parent / "api_models.json"

# Provider names are the strings used in api_models.json.
PROVIDER_ENV_VARS = {
    "Anthropic": "ANTHROPIC_API_KEY",
    "OpenAI": "OPENAI_API_KEY",
    "xAI": "XAI_API_KEY",
    "DeepSeek": "DEEPSEEK_API_KEY",
    "Google": "GOOGLE_API_KEY",
    "Kimi": "KIMI_API_KEY",
    "TypeSafe": "TYPESAFE_API_KEY",
}

# The provider that has no key to set: the local `claude` CLI answers on the
# account that is signed in to it (a subscription, or CLAUDE_CODE_OAUTH_TOKEN
# on a server). It is a provider like any other to everything above this
# module — it simply never appears in a "set your key" flow.
CLAUDE_CODE = "Claude Code"
KEYLESS_PROVIDERS = frozenset({CLAUDE_CODE})

# Model ids served by the CLI are namespaced, so nothing can confuse
# `claude-code:opus` (the subscription) with `claude-opus-5-5` (the API).
CLI_MODEL_PREFIX = "claude-code:"

# Providers v2 has a client for (see client.py:make_llm). Anything in the
# registry but missing here is shown in the picker and not selectable — better
# a greyed row than a run that dies on an id the transport rejects.
SUPPORTED_PROVIDERS = frozenset(PROVIDER_ENV_VARS) | KEYLESS_PROVIDERS

_FALLBACK = ("Claude (your account)", "claude-code:opus", CLAUDE_CODE)


def load_api_models() -> list[tuple[str, str, str]]:
    """The registry as (display name, model id, provider), file order kept.

    Falls back to the built-in default alone if the file is missing or
    malformed — a broken registry should cost you the picker, not the app.
    """
    try:
        entries = json.loads(API_MODELS_PATH.read_text())
        models = [(e["display_name"], e["model_name"], e["provider"])
                  for e in entries]
    except (OSError, ValueError, KeyError, TypeError):
        return [_FALLBACK]
    return models or [_FALLBACK]


def provider_for(model_id: str) -> str | None:
    """Which provider serves a model id. None if it is not in the registry —
    a hand-exported HEDGE_FUND_LLM_MODEL should not be second-guessed.

    The one exception is the `claude-code:` namespace: the CLI takes any
    alias or model name after the colon, so `claude-code:<anything>` routes
    to the CLI whether or not the registry lists it. That is the point of a
    namespace — a model newer than this file should not need a code change.
    """
    listed = next((prov for _, mid, prov in load_api_models() if mid == model_id),
                  None)
    if listed is not None:
        return listed
    return CLAUDE_CODE if model_id.startswith(CLI_MODEL_PREFIX) else None


def env_var_for(provider: str) -> str | None:
    """The environment variable a provider's key is read from, or None when
    the provider has no key to set (see KEYLESS_PROVIDERS)."""
    return PROVIDER_ENV_VARS.get(provider)


def needs_key(provider: str) -> bool:
    """Whether this provider is reached with a key at all. False for the CLI,
    which carries the account's own session instead."""
    return provider not in KEYLESS_PROVIDERS


def is_supported(provider: str) -> bool:
    """Can v2 actually talk to this provider?"""
    return provider in SUPPORTED_PROVIDERS
