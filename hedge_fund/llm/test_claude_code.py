"""The CLI provider: what it runs, what it refuses to leak, how it fails.

Nothing here starts a real `claude` — subprocess is faked at the seam. What
is actually under test is the contract the rest of the package depends on:
a prompt that never reaches argv, an API key that never reaches the child,
an old CLI that degrades instead of dying, and failures that say what to do.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from hedge_fund.llm import claude_code
from hedge_fund.llm.claude_code import (
    ClaudeCodeError,
    ClaudeCodeLLM,
    cli_model,
    resolve_binary,
)
from hedge_fund.llm import LLMClient, make_llm, provider_for

BINARY = "/fake/bin/claude"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """A CLI that exists, no inherited env, and the flag memo reset — it is
    module state, and one test proving a stale CLI must not teach the next."""
    monkeypatch.setenv("HEDGE_FUND_CLAUDE_BIN", BINARY)
    monkeypatch.delenv("HEDGE_FUND_CLAUDE_ARGS", raising=False)
    monkeypatch.delenv("HEDGE_FUND_CLAUDE_ALLOW_API_KEY", raising=False)
    monkeypatch.delenv("HEDGE_FUND_LLM_MODEL", raising=False)
    monkeypatch.setattr(claude_code, "_HARDENING_OK", None)


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _result(text: str, **extra) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": text, **extra})


class Recorder:
    """Stands in for subprocess.run and keeps every call it was asked to make."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        response = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def last(self) -> dict:
        return self.calls[-1]


# ---------------------------------------------------------------------------
# The call itself
# ---------------------------------------------------------------------------

def test_complete_returns_the_result_field(monkeypatch):
    run = Recorder(_completed(_result('{"signal": "bullish"}')))
    monkeypatch.setattr(subprocess, "run", run)

    answer = ClaudeCodeLLM(model="claude-code:opus").complete("be a value investor", "AAPL?")

    assert answer == '{"signal": "bullish"}'
    assert run.last["args"][0] == BINARY
    assert "-p" in run.last["args"]
    assert run.last["args"][run.last["args"].index("--model") + 1] == "opus"


def test_the_user_prompt_goes_in_on_stdin_not_argv(monkeypatch):
    """A fundamentals prompt in argv is visible in `ps` and can exceed ARG_MAX."""
    run = Recorder(_completed(_result("ok")))
    monkeypatch.setattr(subprocess, "run", run)

    ClaudeCodeLLM().complete("system prompt", "SECRET-PROMPT-BODY")

    assert run.last["input"] == "SECRET-PROMPT-BODY"
    assert "SECRET-PROMPT-BODY" not in " ".join(run.last["args"])
    assert "system prompt" in run.last["args"]  # the persona is small and is a flag


def test_tools_and_mcp_are_disabled(monkeypatch):
    """An investor agent is a completion: no Bash, no Read, no MCP server."""
    run = Recorder(_completed(_result("ok")))
    monkeypatch.setattr(subprocess, "run", run)

    ClaudeCodeLLM().complete("s", "u")

    args = run.last["args"]
    assert args[args.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in args
    assert "--no-session-persistence" in args


def test_extra_args_are_appended(monkeypatch):
    monkeypatch.setenv("HEDGE_FUND_CLAUDE_ARGS", "--add-dir /tmp/x --settings '/a b/s.json'")
    run = Recorder(_completed(_result("ok")))
    monkeypatch.setattr(subprocess, "run", run)

    ClaudeCodeLLM().complete("s", "u")

    assert run.last["args"][-4:] == ["--add-dir", "/tmp/x", "--settings", "/a b/s.json"]


# ---------------------------------------------------------------------------
# The account, not the key
# ---------------------------------------------------------------------------

def test_the_api_key_is_kept_out_of_the_child(monkeypatch):
    """With ANTHROPIC_API_KEY set the CLI would bill tokens to the key —
    which is the one thing this provider exists to avoid."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-used")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token-should-survive")
    run = Recorder(_completed(_result("ok")))
    monkeypatch.setattr(subprocess, "run", run)

    ClaudeCodeLLM().complete("s", "u")

    env = run.last["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token-should-survive"


def test_the_api_key_can_be_allowed_back_in(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deliberate")
    monkeypatch.setenv("HEDGE_FUND_CLAUDE_ALLOW_API_KEY", "1")
    run = Recorder(_completed(_result("ok")))
    monkeypatch.setattr(subprocess, "run", run)

    ClaudeCodeLLM().complete("s", "u")

    assert run.last["env"]["ANTHROPIC_API_KEY"] == "sk-ant-deliberate"


# ---------------------------------------------------------------------------
# Failing usefully
# ---------------------------------------------------------------------------

def test_an_expired_session_says_how_to_sign_in(monkeypatch):
    payload = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "Failed to authenticate: OAuth session expired"})
    monkeypatch.setattr(subprocess, "run", Recorder(_completed(payload)))

    with pytest.raises(ClaudeCodeError) as exc:
        ClaudeCodeLLM().complete("s", "u")

    assert "claude login" in str(exc.value)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc.value)


def test_a_usage_limit_is_named_as_the_accounts(monkeypatch):
    payload = json.dumps({"type": "result", "is_error": True,
                          "result": "Claude usage limit reached"})
    monkeypatch.setattr(subprocess, "run", Recorder(_completed(payload)))

    with pytest.raises(ClaudeCodeError, match="limit"):
        ClaudeCodeLLM().complete("s", "u")


def test_a_timeout_is_reported_as_one(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        Recorder(subprocess.TimeoutExpired(cmd="claude", timeout=1.0)))

    with pytest.raises(ClaudeCodeError, match="did not answer"):
        ClaudeCodeLLM(timeout=1.0).complete("s", "u")


def test_a_missing_binary_says_what_to_install(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Recorder(FileNotFoundError()))

    with pytest.raises(ClaudeCodeError, match="HEDGE_FUND_CLAUDE_BIN"):
        ClaudeCodeLLM().complete("s", "u")


def test_non_json_output_is_a_failure_not_a_thesis(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Recorder(_completed("I am a plain sentence")))

    with pytest.raises(ClaudeCodeError, match="no JSON"):
        ClaudeCodeLLM().complete("s", "u")


# ---------------------------------------------------------------------------
# An older CLI
# ---------------------------------------------------------------------------

def test_an_unknown_flag_retries_without_the_hardening(monkeypatch):
    """A CLI too old for --tools should still answer, not take the fund down."""
    run = Recorder(_completed("", returncode=1, stderr="error: unknown option '--tools'"),
                   _completed(_result("answered anyway")))
    monkeypatch.setattr(subprocess, "run", run)

    assert ClaudeCodeLLM().complete("s", "u") == "answered anyway"
    assert len(run.calls) == 2
    assert "--tools" in run.calls[0]["args"]
    assert "--tools" not in run.calls[1]["args"]


def test_the_flag_failure_is_remembered(monkeypatch):
    run = Recorder(_completed("", returncode=1, stderr="error: unknown option '--tools'"),
                   _completed(_result("ok")))
    monkeypatch.setattr(subprocess, "run", run)
    llm = ClaudeCodeLLM()
    llm.complete("s", "u")

    run2 = Recorder(_completed(_result("ok again")))
    monkeypatch.setattr(subprocess, "run", run2)
    llm.complete("s", "u")

    assert len(run2.calls) == 1               # no second attempt to re-learn it
    assert "--tools" not in run2.last["args"]


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

class FakePopen:
    """Line-delimited events, the way `--output-format stream-json` emits them."""

    def __init__(self, lines, returncode: int = 0, stderr: str = ""):
        self.stdin = _Sink()
        self.stdout = iter(lines)
        self.stderr = _Reader(stderr)
        self._returncode = returncode
        self.returncode = None

    def wait(self, timeout=None):
        self.returncode = self._returncode
        return self._returncode

    def poll(self):
        return self.returncode

    def kill(self):  # pragma: no cover - only on an abandoned process
        self.returncode = -9


class _Sink:
    def write(self, text):
        self.written = text

    def close(self):
        pass


class _Reader:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def test_streaming_feeds_the_listener_and_returns_the_whole_answer(monkeypatch):
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                      "delta": {"type": "text_delta", "text": '{"signal": '}}}),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                      "delta": {"type": "text_delta", "text": '"bullish"}'}}}),
        _result('{"signal": "bullish"}'),
    ]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakePopen(lines))
    seen: list[str] = []

    answer = ClaudeCodeLLM(on_token=seen.append).complete("s", "u")

    assert seen == ['{"signal": ', '"bullish"}']
    assert answer == '{"signal": "bullish"}'


def test_streaming_ignores_thinking_and_tool_events(monkeypatch):
    lines = [
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                      "delta": {"type": "thinking_delta", "thinking": "hmm"}}}),
        "not json at all",
        _result("the answer"),
    ]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakePopen(lines))
    seen: list[str] = []

    assert ClaudeCodeLLM(on_token=seen.append).complete("s", "u") == "the answer"
    assert seen == []


def test_streaming_without_a_result_event_still_answers(monkeypatch):
    """An older CLI that emits assistant messages and no result line."""
    lines = [json.dumps({"type": "assistant",
                         "message": {"content": [{"type": "text", "text": "partial answer"}]}})]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakePopen(lines))

    assert ClaudeCodeLLM(on_token=lambda _: None).complete("s", "u") == "partial answer"


def test_a_stream_that_says_nothing_is_a_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: FakePopen([], returncode=1, stderr="boom"))

    with pytest.raises(ClaudeCodeError, match="boom"):
        ClaudeCodeLLM(on_token=lambda _: None).complete("s", "u")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_make_llm_routes_the_namespace_to_the_cli(monkeypatch):
    llm = make_llm("claude-code:sonnet")
    assert isinstance(llm, ClaudeCodeLLM)
    assert isinstance(llm, LLMClient)
    assert llm.model == "claude-code:sonnet"


def test_make_llm_needs_no_key_for_the_cli(monkeypatch):
    """The whole point: no ANTHROPIC_API_KEY anywhere and it still builds."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(make_llm("claude-code:opus"), ClaudeCodeLLM)


def test_an_unlisted_cli_model_still_routes():
    """A model newer than api_models.json should not need a code change."""
    assert provider_for("claude-code:some-future-model") == "Claude Code"
    assert isinstance(make_llm("claude-code:some-future-model"), ClaudeCodeLLM)


def test_cli_model_strips_the_namespace():
    assert cli_model("claude-code:opus") == "opus"
    assert cli_model("claude-code:claude-opus-5-5") == "claude-opus-5-5"
    assert cli_model("opus") == "opus"


def test_the_cache_key_separates_account_from_api():
    """The same prompt answered by the subscription and by the API are two
    records: replaying one as the other misreports which account paid."""
    cli = ClaudeCodeLLM(model="claude-code:opus")
    assert cli.cache_key("buffett", "s", "u") != cli.cache_key("buffett", "s", "u2")
    assert cli.cache_key("buffett", "s", "u") != ClaudeCodeLLM(model="claude-code:sonnet").cache_key("buffett", "s", "u")


def test_resolve_binary_prefers_the_explicit_one(monkeypatch):
    assert resolve_binary() == BINARY
    monkeypatch.delenv("HEDGE_FUND_CLAUDE_BIN")
    monkeypatch.setattr(claude_code.shutil, "which", lambda _: "/usr/bin/claude")
    assert resolve_binary() == "/usr/bin/claude"


def test_resolve_binary_says_what_to_install(monkeypatch):
    monkeypatch.delenv("HEDGE_FUND_CLAUDE_BIN")
    monkeypatch.setattr(claude_code.shutil, "which", lambda _: None)
    monkeypatch.setattr(claude_code.Path, "exists", lambda _: False)

    with pytest.raises(ClaudeCodeError, match="no `claude` CLI"):
        resolve_binary()
