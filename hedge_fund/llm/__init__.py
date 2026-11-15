"""v2 LLM layer — provider protocol, provider clients, prompt cache."""

from hedge_fund.llm.cache import PromptCache, prompt_key
from hedge_fund.llm.client import (
    DEFAULT_MODEL,
    AnthropicLLM,
    ChatLLM,
    JevLLM,
    LLMCallError,
    LLMClient,
    LLMParseError,
    extract_json,
    make_llm,
)
from hedge_fund.llm.registry import (
    CLAUDE_CODE,
    CLI_MODEL_PREFIX,
    KEYLESS_PROVIDERS,
    PROVIDER_ENV_VARS,
    SUPPORTED_PROVIDERS,
    env_var_for,
    is_supported,
    load_api_models,
    needs_key,
    provider_for,
)
# Imported last: it reads both modules above, and nothing above reads it.
from hedge_fund.llm.claude_code import (
    ClaudeCodeError,
    ClaudeCodeLLM,
    check_login,
    cli_version,
    is_available as claude_cli_available,
)
from hedge_fund.llm.watch import ThesisStream

__all__ = [
    "AnthropicLLM",
    "CLAUDE_CODE",
    "CLI_MODEL_PREFIX",
    "ChatLLM",
    "ClaudeCodeError",
    "ClaudeCodeLLM",
    "DEFAULT_MODEL",
    "JevLLM",
    "KEYLESS_PROVIDERS",
    "LLMCallError",
    "LLMClient",
    "LLMParseError",
    "PROVIDER_ENV_VARS",
    "PromptCache",
    "SUPPORTED_PROVIDERS",
    "ThesisStream",
    "check_login",
    "claude_cli_available",
    "cli_version",
    "env_var_for",
    "extract_json",
    "is_supported",
    "load_api_models",
    "make_llm",
    "needs_key",
    "prompt_key",
    "provider_for",
]
