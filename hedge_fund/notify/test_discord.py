"""The transport: what it posts, what it retries, and what it refuses.

Nothing here touches the network. What is actually under test is the
promise the engine leans on — that a notifier is incapable of taking a fund
down — plus the one thing that is checked loudly instead: a webhook URL
that does not belong to Discord.
"""

from __future__ import annotations

import pytest
import requests

from hedge_fund.notify import (
    DiscordError,
    DiscordWebhook,
    Notifier,
    from_env,
    parse_channels,
    redact,
    validate_url,
)
from hedge_fund.notify import messages
from hedge_fund.tui.fixtures import make_session

URL = "https://discord.com/api/webhooks/123456789/a-very-secret-token"


class FakeResponse:
    def __init__(self, status_code=204, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = str(body or "")

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    """requests, with a queue of answers and a record of what was posted."""

    def __init__(self, *responses):
        self._responses = list(responses) or [FakeResponse()]
        self.posts: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        response = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(response, Exception):
            raise response
        return response


class Clock:
    def __init__(self):
        self.slept: list[float] = []

    def __call__(self, seconds):
        self.slept.append(seconds)


def _hook(*responses, sleep=None):
    return DiscordWebhook(URL, session=FakeSession(*responses), sleep=sleep or Clock())


# ---------------------------------------------------------------------------
# The URL is a credential pointed at a host
# ---------------------------------------------------------------------------

def test_only_discords_own_hosts_are_accepted():
    assert validate_url(URL) == URL
    assert validate_url("https://canary.discord.com/api/webhooks/1/t")

    for bad in ("https://evil.example.com/api/webhooks/1/t",
                "http://discord.com/api/webhooks/1/t",
                "https://discord.com/api/channels/1/messages",
                "https://discord.com/api/webhooks/",
                ""):
        with pytest.raises(DiscordError):
            validate_url(bad)


def test_a_logged_url_keeps_the_channel_and_drops_the_token():
    line = redact(URL)
    assert "123456789" in line
    assert "a-very-secret-token" not in line


def test_the_repr_does_not_carry_the_token():
    assert "a-very-secret-token" not in repr(_hook())


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def test_an_embed_is_posted_as_discord_expects():
    hook = _hook()
    assert hook.send(embed={"title": "hi", "color": 1}) is True

    posted = hook._session.posts[0]["json"]
    assert posted["embeds"] == [{"title": "hi", "color": 1}]


def test_an_oversized_embed_is_cut_down_before_it_is_posted():
    hook = _hook()
    hook.send(embed={"title": "t", "description": "x" * 9000, "color": 1})

    description = hook._session.posts[0]["json"]["embeds"][0]["description"]
    assert len(description) <= messages.EMBED_TOTAL_MAX


def test_nothing_to_say_is_not_a_post():
    hook = _hook()
    assert hook.send() is False
    assert hook._session.posts == []


def test_a_rate_limit_waits_exactly_as_long_as_discord_asked():
    clock = Clock()
    hook = DiscordWebhook(
        URL, sleep=clock,
        session=FakeSession(FakeResponse(429, {"retry_after": 2.5}), FakeResponse(204)))

    assert hook.send(embed={"title": "t", "color": 1}) is True
    assert clock.slept == [2.5]


def test_a_long_rate_limit_drops_the_message_instead_of_blocking():
    """A runner waiting ten minutes on a notification is a stalled fund."""
    clock = Clock()
    hook = DiscordWebhook(URL, sleep=clock,
                          session=FakeSession(FakeResponse(429, {"retry_after": 600})))

    assert hook.send(embed={"title": "t", "color": 1}) is False
    assert clock.slept == []


def test_a_server_error_is_retried_once():
    hook = _hook(FakeResponse(503), FakeResponse(204))
    assert hook.send(embed={"title": "t", "color": 1}) is True
    assert len(hook._session.posts) == 2


def test_a_rejected_message_is_not_retried():
    hook = _hook(FakeResponse(400, {"message": "Invalid Form Body"}))
    assert hook.send(embed={"title": "t", "color": 1}) is False
    assert len(hook._session.posts) == 1


def test_discord_being_unreachable_is_never_an_exception():
    hook = _hook(requests.ConnectionError("no route to host"))
    assert hook.send(embed={"title": "t", "color": 1}) is False


# ---------------------------------------------------------------------------
# The notifier
# ---------------------------------------------------------------------------

def test_sessions_and_alerts_go_to_their_own_channels():
    main, alerts = _hook(), _hook()
    notifier = Notifier(main, alerts=alerts)

    notifier.session("alpha", make_session("2026-03-02"))
    notifier.event("alpha", "gap", {"to": "2026-03-02"})

    assert len(main._session.posts) == 1
    assert len(alerts._session.posts) == 1
    assert "gap" in alerts._session.posts[0]["json"]["embeds"][0]["title"]


def test_with_one_webhook_everything_goes_there():
    hook = _hook()
    notifier = Notifier(hook)

    notifier.session("alpha", make_session("2026-03-02"))
    notifier.halted("alpha", "feed is lying")

    assert len(hook._session.posts) == 2


def test_disabled_channels_say_nothing():
    hook = _hook()
    notifier = Notifier(hook, enabled=("alerts",))

    assert notifier.session("alpha", make_session("2026-03-02")) is False
    assert notifier.halted("alpha", "because") is True
    assert len(hook._session.posts) == 1


def test_a_broken_message_is_swallowed_not_raised():
    """The one promise the engine leans on: a notifier cannot take a fund down."""
    hook = _hook()
    notifier = Notifier(hook)

    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError("this record is not a record")

    assert notifier.session("alpha", Exploding()) is False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_no_webhook_means_no_notifier():
    assert from_env({}) is None
    assert from_env({"DISCORD_WEBHOOK_URL": "   "}) is None


def test_a_bad_webhook_is_loud_at_configuration():
    with pytest.raises(DiscordError):
        from_env({"DISCORD_WEBHOOK_URL": "https://example.com/hook"})


def test_the_alert_webhook_is_optional():
    notifier = from_env({"DISCORD_WEBHOOK_URL": URL})
    assert notifier._alerts is notifier._webhook

    separate = from_env({"DISCORD_WEBHOOK_URL": URL,
                         "DISCORD_ALERT_WEBHOOK_URL": URL.replace("123456789", "987")})
    assert separate._alerts is not separate._webhook


def test_which_channels_are_enabled_reads_from_the_environment():
    assert parse_channels(None) == ("sessions", "alerts", "backtests")
    assert parse_channels("") == ("sessions", "alerts", "backtests")
    assert parse_channels("alerts") == ("alerts",)
    assert parse_channels("alerts, backtests") == ("alerts", "backtests")
    assert parse_channels("none") == ()
    assert parse_channels("nonsense") == ()
