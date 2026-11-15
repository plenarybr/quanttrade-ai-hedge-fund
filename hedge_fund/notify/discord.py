"""Getting a message into a Discord channel, and never getting in the way.

A webhook URL is the whole configuration: Discord mints one per channel,
it needs no bot, no gateway and no intents, and posting to it is one POST.
That is the right shape for a fund that runs unattended — the process that
advances the book should not also be maintaining a websocket.

The contract the rest of the package relies on:

* **A notifier never raises.** Discord being down, rate-limiting, or
  misconfigured is not a reason to halt a fund or fail a backtest. Every
  send returns True/False and logs; the engine is told nothing.
* **The URL is checked, once, at configuration.** A webhook URL is a
  bearer credential pointed at a host, and a typo in `.env` would post the
  book to whatever that host is. Only Discord's own hosts are accepted,
  and that failure is loud, because it happens before anything runs.
* **Secrets stay out of logs.** The token is the back half of the URL, so
  the URL is never logged whole.
"""

from __future__ import annotations

import logging
import os
import time
from urllib.parse import urlparse

import requests

from hedge_fund.notify import messages

logger = logging.getLogger(__name__)

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"
ALERT_WEBHOOK_ENV = "DISCORD_ALERT_WEBHOOK_URL"
NOTIFY_ENV = "DISCORD_NOTIFY"

# Discord's own webhook hosts. Anything else is a misconfiguration, not a
# destination — see the module docstring.
_HOSTS = frozenset({"discord.com", "discordapp.com", "canary.discord.com",
                    "ptb.discord.com", "www.discord.com"})
_PATH = "/api/webhooks/"

# What a notifier is allowed to send, by name, for DISCORD_NOTIFY.
CHANNELS = ("sessions", "alerts", "backtests")

_RETRY_STATUSES = frozenset({500, 502, 503, 504})
_MAX_SLEEP = 30.0  # a rate limit longer than this is waited out by the next message


class DiscordError(RuntimeError):
    """The webhook is not usable — raised at configuration, never at send."""


class DiscordWebhook:
    """One channel, reached by URL. `send` is best-effort and never raises."""

    def __init__(self, url: str, *, timeout: float = 10.0, session=None,
                 sleep=time.sleep, username: str | None = None) -> None:
        self.url = validate_url(url)
        self._timeout = timeout
        self._session = session or requests
        self._sleep = sleep
        self._username = username

    def __repr__(self) -> str:  # pragma: no cover - debugging comfort only
        return f"DiscordWebhook({redact(self.url)})"

    def send(self, *, embed: dict | None = None, content: str | None = None) -> bool:
        """Post one message. True when Discord accepted it.

        Retries once: a 429 after the delay Discord asks for, a 5xx after a
        second. Anything else is reported and dropped — a message the
        channel never sees must not become a failed session.
        """
        payload: dict = {}
        if content:
            payload["content"] = messages._clip(content, messages.CONTENT_MAX)
        if embed is not None:
            payload["embeds"] = [messages.fit(dict(embed))]
        if self._username:
            payload["username"] = self._username
        if not payload:
            return False

        for attempt in (1, 2):
            try:
                response = self._session.post(self.url, json=payload, timeout=self._timeout)
            except requests.RequestException as exc:
                logger.warning("discord: %s unreachable (%s)", redact(self.url), exc.__class__.__name__)
                return False

            status = response.status_code
            if 200 <= status < 300:
                return True
            if attempt == 2:
                break
            if status == 429:
                delay = _retry_after(response)
                if delay > _MAX_SLEEP:
                    logger.warning("discord: rate limited for %.0fs; dropping this message", delay)
                    return False
                self._sleep(delay)
                continue
            if status in _RETRY_STATUSES:
                self._sleep(1.0)
                continue
            break

        logger.warning("discord: rejected with HTTP %s (%s)", status, _why(response))
        return False


class Notifier:
    """What the fund says, routed to the right channel.

    Alerts (halts, failures, gaps, stop-outs) go to the alert webhook when
    one is configured, so a fund that posts a session every minute can still
    have a quiet channel that only speaks when something is wrong.
    """

    def __init__(self, webhook: DiscordWebhook, *, alerts: DiscordWebhook | None = None,
                 enabled: tuple[str, ...] = CHANNELS) -> None:
        self._webhook = webhook
        self._alerts = alerts or webhook
        self._enabled = frozenset(enabled)

    # -- paper sessions ------------------------------------------------

    def session(self, fund: str, record, *, capital: float | None = None) -> bool:
        if "sessions" not in self._enabled:
            return False
        # A stop-out is an alert wherever ordinary sessions go.
        return self._send(lambda: (
            self._alerts if messages._is_stopped_out(record) else self._webhook,
            messages.session_embed(fund, record, capital=capital),
        ))

    def event(self, fund: str, kind: str, fields: dict) -> bool:
        if "alerts" not in self._enabled:
            return False
        return self._send(lambda: (self._alerts, messages.event_embed(fund, kind, fields)))

    def halted(self, fund: str, reason: str) -> bool:
        if "alerts" not in self._enabled:
            return False
        return self._send(lambda: (self._alerts, messages.halt_embed(fund, reason)))

    def resumed(self, fund: str) -> bool:
        if "alerts" not in self._enabled:
            return False
        return self._send(lambda: (self._alerts, messages.resume_embed(fund)))

    def failed(self, fund: str, exc: BaseException, *, context: str = "") -> bool:
        if "alerts" not in self._enabled:
            return False
        return self._send(lambda: (self._alerts,
                                   messages.failure_embed(fund, exc, context=context)))

    # -- research ------------------------------------------------------

    def backtest(self, result, *, universe: list[str] | None = None,
                 data_calls: int | None = None) -> bool:
        if "backtests" not in self._enabled:
            return False
        return self._send(lambda: (self._webhook, messages.backtest_embed(
            result, universe=universe, data_calls=data_calls)))

    def ping(self, text: str = "aihf is wired up to this channel.") -> bool:
        """A deliberate test message — `aihf discord test` sends this."""
        return self._send(lambda: (self._webhook, messages._embed(
            title="aihf", description=text, color=messages.BLURPLE)))

    # -- the one place a notifier is allowed to swallow ----------------

    def _send(self, build) -> bool:
        """*build* returns (webhook, embed), and is called in here on purpose.

        Formatting and routing run inside this try, not outside it: a record
        shaped in a way a message did not expect is a bug worth a log line,
        never a halted fund. This is the only broad except in the package.
        """
        try:
            webhook, embed = build()
            return webhook.send(embed=embed)
        except Exception:  # noqa: BLE001 - see above
            logger.exception("discord: could not send a message")
            return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def from_env(env: dict | None = None, **kwargs) -> Notifier | None:
    """The notifier the environment describes, or None when none is wanted.

    None is the normal, quiet case: no webhook set means Discord is off, and
    nothing about that is an error. A webhook that *is* set and unusable
    raises — a fund told to report that cannot is worth stopping for.
    """
    env = os.environ if env is None else env
    url = (env.get(WEBHOOK_ENV) or "").strip()
    if not url:
        return None
    alert_url = (env.get(ALERT_WEBHOOK_ENV) or "").strip()
    return Notifier(
        DiscordWebhook(url, **kwargs),
        alerts=DiscordWebhook(alert_url, **kwargs) if alert_url else None,
        enabled=parse_channels(env.get(NOTIFY_ENV)),
    )


def parse_channels(value: str | None) -> tuple[str, ...]:
    """DISCORD_NOTIFY as a tuple. Unset means everything; an unknown name is
    ignored rather than silently turning the whole notifier off."""
    if value is None or not value.strip():
        return CHANNELS
    wanted = {part.strip().lower() for part in value.replace(",", " ").split()}
    if "none" in wanted or "off" in wanted:
        return ()
    if "all" in wanted:
        return CHANNELS
    return tuple(name for name in CHANNELS if name in wanted)


def validate_url(url: str) -> str:
    """A Discord webhook URL, or a failure that says what is wrong with it."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _HOSTS:
        raise DiscordError(
            f"{WEBHOOK_ENV} must be an https webhook URL on discord.com "
            f"(got {parsed.scheme or '?'}://{parsed.hostname or '?'})"
        )
    if not parsed.path.startswith(_PATH) or len(parsed.path) <= len(_PATH):
        raise DiscordError(
            f"{WEBHOOK_ENV} does not look like a webhook URL — copy it from "
            "Channel → Edit Channel → Integrations → Webhooks"
        )
    return url


def redact(url: str) -> str:
    """A URL safe to log: the channel's id kept, the token dropped."""
    parsed = urlparse(url)
    parts = parsed.path.rstrip("/").split("/")
    channel = parts[-2] if len(parts) >= 2 else "?"
    return f"https://{parsed.hostname}{_PATH}{channel}/…"


def _retry_after(response) -> float:
    """How long Discord asked us to wait. Body first, then the header."""
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("retry_after") is not None:
            return max(0.0, float(body["retry_after"]))
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        return max(0.0, float(response.headers.get("Retry-After", 1.0)))
    except (TypeError, ValueError):
        return 1.0


def _why(response) -> str:
    """Discord's own error message, clipped, never the request we sent."""
    try:
        return messages._clip(str(response.json()), 300)
    except (ValueError, AttributeError):
        return messages._clip(getattr(response, "text", "") or "", 300)
