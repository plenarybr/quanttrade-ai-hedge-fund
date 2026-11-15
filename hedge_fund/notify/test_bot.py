"""The bot's edges: a missing dependency, a missing token, and the tree.

discord.py is optional, so most of this file is about behaving well without
it. The one test that needs it is skipped rather than failed — a checkout
with no extras installed must still have a green suite.
"""

from __future__ import annotations

import pytest

from hedge_fund.notify import bot
from hedge_fund.notify.bot import BotUnavailable, token_from_env

discord_installed = pytest.importorskip


def test_no_token_says_where_to_get_one():
    with pytest.raises(BotUnavailable, match="DISCORD_BOT_TOKEN"):
        token_from_env({})


def test_a_blank_token_is_no_token():
    with pytest.raises(BotUnavailable):
        token_from_env({"DISCORD_BOT_TOKEN": "   "})


def test_a_token_is_stripped():
    assert token_from_env({"DISCORD_BOT_TOKEN": " abc.def \n"}) == "abc.def"


def test_without_discordpy_the_failure_says_how_to_install_it(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "discord":
            raise ImportError("no module named discord")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    with pytest.raises(BotUnavailable, match=r"aihf\[discord\]"):
        bot.require_discord()


def test_the_tree_carries_every_command():
    """Skipped where discord.py is not installed; the webhook never needs it."""
    pytest.importorskip("discord", reason="the bot is an optional extra")

    client = bot.build_client(env={}, root=None)
    names = {command.name for command in client.aihf_tree.get_commands()}

    assert names == {"funds", "status", "scorecard", "halt", "resume", "run", "stop"}
