"""Load .env for v2 tests so FINANCIAL_DATASETS_API_KEY is available."""

import pytest
from dotenv import load_dotenv

from hedge_fund.features import breakpoints
from hedge_fund.notify import discord as discord_module

load_dotenv()


@pytest.fixture(autouse=True)
def _no_real_channel(monkeypatch):
    """No test ever posts to a real Discord channel.

    `load_dotenv()` above is the reason this has to exist: whoever runs the
    suite has a working DISCORD_WEBHOOK_URL in ~/.hedge-fund/.env, and
    without this a `paper tick` test would publish the book to their channel.
    Tests that exercise Discord set the variables themselves.
    """
    for variable in (discord_module.WEBHOOK_ENV, discord_module.ALERT_WEBHOOK_ENV,
                     discord_module.NOTIFY_ENV, "DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID",
                     "DISCORD_ALLOWED_USER_IDS", "DISCORD_ALLOWED_CHANNEL_IDS"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture(autouse=True)
def _no_breakpoints_download(monkeypatch):
    """A blind agent built without an injected table must not download the
    breakpoints from a test. Tests that need a table pass their own."""
    def refuse():
        raise RuntimeError("tests must inject MEBreakpoints; no network download")
    monkeypatch.setattr(breakpoints, "load_breakpoints", refuse)
    monkeypatch.setattr(breakpoints, "_shared", None)
