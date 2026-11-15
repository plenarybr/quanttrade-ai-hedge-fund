"""Claude as the account runs it — the local `claude` CLI, no API key.

Every other provider in this package bills a key: a token in ``.env``, a
meter at the other end. This one speaks to the Claude Code CLI already
installed on the machine, which authenticates with the *account's own
session* — a Pro/Max subscription signed in with ``claude login``, or a
long-lived token from ``claude setup-token`` exported as
``CLAUDE_CODE_OAUTH_TOKEN`` (that is the one a server wants).

It is a plain :class:`~hedge_fund.llm.client.LLMClient`: one
``complete(system, user) -> str`` per agent call, a fresh non-interactive
process each time, no tools, no MCP servers, no session on disk. The agent
layer cannot tell it apart from ChatAnthropic — the prompt cache, the
parse, and the thesis stream all work unchanged.

Three things it does deliberately:

1. **The subscription, not the API.** ``ANTHROPIC_API_KEY`` is stripped from
   the child's environment, because the CLI would otherwise prefer it and
   quietly bill per token — the opposite of why this provider exists. Set
   ``HEDGE_FUND_CLAUDE_ALLOW_API_KEY=1`` if you really want the key to win.
2. **The prompt goes in on stdin.** Prompts carry a company's whole
   fundamental history; an argv full of that is visible in `ps` and runs
   into ARG_MAX. Only the system prompt is a flag (the CLI has no
   ``--system-prompt-file``), and personas are small.
3. **The hardening flags are optional.** ``--tools ""``,
   ``--strict-mcp-config`` and friends are what keep an investor agent from
   reading files or calling an MCP server. A CLI too old to know a flag
   rejects the whole call, so the first such failure retries once with the
   minimum set and remembers — a stale install degrades to a plain
   completion instead of dying.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from hedge_fund.llm.client import LLMCallError, TokenListener
from hedge_fund.llm.registry import CLAUDE_CODE as PROVIDER, CLI_MODEL_PREFIX as MODEL_PREFIX

# What follows the namespace is handed to `claude --model` as-is, so an alias
# ("opus") and a full model name ("claude-opus-5-5") both work.
DEFAULT_CLI_MODEL = "claude-code:opus"

# Everything past the required flags. Dropped wholesale if the installed CLI
# rejects one of them (see _HARDENING_OK).
_HARDENING = (
    "--tools", "",                 # no Bash, no Read, no WebFetch: this is a completion
    "--strict-mcp-config",         # the user's MCP servers are not the fund's business
    "--disable-slash-commands",
    "--no-session-persistence",    # an agent call is not a conversation to resume
    "--permission-prompts", "none",
    "--max-turns", "1",
)

# None until the first call proves one way or the other; False after a CLI
# has rejected a hardening flag, so the rest of the run skips the retry.
_HARDENING_OK: bool | None = None

_AUTH_HINT = (
    "the Claude CLI is not signed in — run `claude login` on this machine, "
    "or export CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token` (headless)"
)


class ClaudeCodeError(LLMCallError):
    """The CLI could not answer: missing, not signed in, or it failed."""


class ClaudeCodeLLM:
    """The `claude` CLI behind the LLMClient protocol.

    With an *on_token* listener the call streams, exactly as ChatLLM does:
    the listener sees text as it arrives and complete() still returns the
    whole response, so the TUI's thesis view works here too.
    """

    def __init__(
        self,
        model: str = DEFAULT_CLI_MODEL,
        timeout: float = 600.0,
        on_token: TokenListener = None,
        binary: str | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self.model = model
        self._timeout = timeout
        self._on_token = on_token
        self._binary = binary
        # A neutral directory: the CLI reads CLAUDE.md and settings from wherever
        # it is started, and an investor agent should not inherit a repo's.
        self._cwd = str(cwd) if cwd is not None else str(Path.home())

    # ------------------------------------------------------------------
    # LLMClient
    # ------------------------------------------------------------------

    def cache_key(self, agent: str, system: str, user: str) -> str:
        """Keyed by provider as well as model: the same prompt answered by
        the subscription and by the API are different records, and replaying
        one as the other would misreport which account paid."""
        identity = {
            "provider": PROVIDER,
            "agent": agent,
            "model": self.model,
            "system": system,
            "user": user,
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def complete(self, system: str, user: str) -> str:
        binary = self._binary or resolve_binary()
        streaming = self._on_token is not None
        for hardened in _attempts():
            args = _build_args(binary, self.model, system, streaming, hardened)
            try:
                if streaming:
                    text, failure = self._stream(args, user)
                else:
                    text, failure = self._once(args, user)
            except FileNotFoundError:
                raise ClaudeCodeError(
                    f"{binary} is not executable — set HEDGE_FUND_CLAUDE_BIN to the "
                    "Claude CLI, or install it from https://claude.com/claude-code"
                ) from None
            except subprocess.TimeoutExpired:
                raise ClaudeCodeError(
                    f"the Claude CLI did not answer within {self._timeout:.0f}s"
                ) from None

            if failure is None:
                return text
            if hardened and _is_flag_error(failure):
                _remember_flag_failure()
                continue
            raise ClaudeCodeError(_explain(failure), {"provider": PROVIDER,
                                                      "model": self.model,
                                                      "detail": failure})
        raise AssertionError("unreachable: every attempt returns or raises")

    # ------------------------------------------------------------------
    # The two transports
    # ------------------------------------------------------------------

    def _once(self, args: list[str], user: str) -> tuple[str, str | None]:
        """One shot, one JSON object. Returns (text, failure); exactly one
        side is meaningful."""
        proc = subprocess.run(
            args, input=user, capture_output=True, text=True,
            timeout=self._timeout, env=_child_env(), cwd=self._cwd,
        )
        if proc.returncode != 0 and not proc.stdout.strip():
            return "", (proc.stderr.strip() or f"the Claude CLI exited {proc.returncode}")
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            return "", f"the Claude CLI returned no JSON: {proc.stdout[:200] or proc.stderr[:200]!r}"
        return _result_text(payload)

    def _stream(self, args: list[str], user: str) -> tuple[str, str | None]:
        """Line-delimited events; text deltas go to the listener as they land.

        The final `result` event is still the answer of record — the deltas
        are a view, and a CLI that sends none (an older one, a cached turn)
        simply shows nothing and returns the same string.
        """
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=_child_env(), cwd=self._cwd,
        )
        streamed: list[str] = []
        final: tuple[str, str | None] | None = None
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(user)
            proc.stdin.close()
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # a log line among the events: not ours to interpret
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "result":
                    final = _result_text(event)
                    continue
                token = _delta_text(event)
                if token:
                    streamed.append(token)
                    self._on_token(token)  # type: ignore[misc]  # streaming implies a listener
            stderr = proc.stderr.read() if proc.stderr else ""
            code = proc.wait(timeout=self._timeout)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        if final is not None:
            return final
        if streamed:
            return "".join(streamed), None
        return "", (stderr.strip() or f"the Claude CLI exited {code} with no result")


# ---------------------------------------------------------------------------
# Building the call
# ---------------------------------------------------------------------------

def cli_model(model_id: str) -> str:
    """The id the CLI understands: `claude-code:opus` -> `opus`."""
    return model_id[len(MODEL_PREFIX):] if model_id.startswith(MODEL_PREFIX) else model_id


def _build_args(binary: str, model: str, system: str, streaming: bool, hardened: bool) -> list[str]:
    args = [binary, "-p", "--model", cli_model(model), "--system-prompt", system]
    if streaming:
        # stream-json needs --verbose to emit, and the partial chunks are
        # what makes a thesis type itself out rather than land whole.
        args += ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    else:
        args += ["--output-format", "json"]
    if hardened:
        args += list(_HARDENING)
    args += _extra_args()
    return args


def _extra_args() -> list[str]:
    """HEDGE_FUND_CLAUDE_ARGS, split like a shell would. An escape hatch for
    flags this module does not know about (--settings, --add-dir, a proxy)."""
    import shlex
    return shlex.split(os.environ.get("HEDGE_FUND_CLAUDE_ARGS", ""))


def _attempts() -> tuple[bool, ...]:
    """Hardened first, then bare — unless a CLI has already refused a flag."""
    return (True, False) if _HARDENING_OK is not False else (False,)


def _remember_flag_failure() -> None:
    global _HARDENING_OK
    _HARDENING_OK = False


def _child_env() -> dict[str, str]:
    """The environment the CLI runs in.

    The API key is removed on purpose: with it set, the CLI bills tokens to
    the key instead of using the signed-in account, which is the whole point
    of this provider. CLAUDE_CODE_OAUTH_TOKEN, if exported, stays — that is
    how a headless box signs in.
    """
    env = dict(os.environ)
    if os.environ.get("HEDGE_FUND_CLAUDE_ALLOW_API_KEY", "").strip() not in {"1", "true", "yes"}:
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def resolve_binary() -> str:
    """Where the CLI is: HEDGE_FUND_CLAUDE_BIN, the PATH, then the two places
    the installer puts it. Raises with what to do about it."""
    explicit = os.environ.get("HEDGE_FUND_CLAUDE_BIN")
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/claude", Path("/usr/local/bin/claude")):
        if candidate.exists():
            return str(candidate)
    raise ClaudeCodeError(
        "no `claude` CLI on this machine — install it (https://claude.com/claude-code) "
        "or point HEDGE_FUND_CLAUDE_BIN at it"
    )


def is_available() -> bool:
    """Whether a CLI exists at all. Says nothing about being signed in."""
    try:
        resolve_binary()
    except ClaudeCodeError:
        return False
    return True


def cli_version(timeout: float = 10.0) -> str | None:
    """The installed CLI's version string, or None if it cannot be asked."""
    try:
        proc = subprocess.run([resolve_binary(), "--version"], capture_output=True,
                              text=True, timeout=timeout)
    except (ClaudeCodeError, OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


def check_login(timeout: float = 60.0) -> str | None:
    """None when a trivial completion works; otherwise why it did not.

    Cheap enough to run before a long session so a stale OAuth session is
    found now rather than three agents in.
    """
    try:
        ClaudeCodeLLM(timeout=timeout).complete("Answer with one word.", "Say OK.")
    except LLMCallError as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# Reading what came back
# ---------------------------------------------------------------------------

def _result_text(payload: dict) -> tuple[str, str | None]:
    """The answer out of a `result` object, or the failure in it."""
    if not isinstance(payload, dict):
        return "", "the Claude CLI returned an unexpected payload"
    result = payload.get("result")
    if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
        return "", str(result or payload.get("error") or "the Claude CLI reported an error")
    if not isinstance(result, str) or not result.strip():
        return "", "the Claude CLI returned an empty result"
    return result, None


def _delta_text(event: dict) -> str:
    """Text out of one stream event, for the listener. Everything else — tool
    events, thinking blocks, init — is not the answer and is ignored."""
    if event.get("type") == "stream_event":
        inner = event.get("event")
        if isinstance(inner, dict) and inner.get("type") == "content_block_delta":
            delta = inner.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                return str(delta.get("text") or "")
        return ""
    if event.get("type") == "assistant":
        message = event.get("message")
        if isinstance(message, dict):
            blocks = message.get("content")
            if isinstance(blocks, list):
                return "".join(b.get("text", "") for b in blocks
                               if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_flag_error(failure: str) -> bool:
    """A CLI refusing an option it does not have, rather than a real failure."""
    lowered = failure.lower()
    return any(marker in lowered for marker in
               ("unknown option", "unknown argument", "unknown or unexpected option",
                "invalid option", "too many arguments"))


def _explain(failure: str) -> str:
    """The CLI's own words, plus the fix when we recognise the problem."""
    lowered = failure.lower()
    if any(marker in lowered for marker in
           ("oauth", "not logged in", "log in", "authenticate", "unauthorized", "401")):
        return f"{failure.strip()} — {_AUTH_HINT}"
    if "usage limit" in lowered or "rate limit" in lowered or "429" in lowered:
        return f"{failure.strip()} — the account's limit, not a key's; it resets on its own"
    return failure.strip()
