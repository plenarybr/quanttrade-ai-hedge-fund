"""Telling a channel what the fund did — Discord, and only Discord so far.

    notify/messages.py   a record -> an embed. Pure, no network.
    notify/discord.py    the webhook transport and the Notifier the engine holds.
    notify/bot.py        the other direction: a bot that answers commands.

The engine already exposes the seams this needs — `on_record` and
`on_event` on the paper runner, the result a backtest returns — so nothing
here is wired into the pipeline itself. `run.py` builds a notifier from the
environment and hands those callbacks over; with no webhook configured,
`from_env()` returns None and the fund runs exactly as it did before.
"""

from hedge_fund.notify.discord import (
    CHANNELS,
    DiscordError,
    DiscordWebhook,
    Notifier,
    from_env,
    parse_channels,
    redact,
    validate_url,
)

__all__ = [
    "CHANNELS",
    "DiscordError",
    "DiscordWebhook",
    "Notifier",
    "from_env",
    "parse_channels",
    "redact",
    "validate_url",
]
