"""The bot: the channel asking the fund questions, instead of the other way.

The webhook in `discord.py` is one-way and needs nothing running. This is
the other half — a gateway connection, so `/status`, `/scorecard`,
`/halt`, `/resume`, `/run` and `/stop` work from a phone. It is optional in
every sense: an extra dependency (`pip install 'aihf[discord]'`), its own
process (`aihf discord bot`), and a fund runs perfectly without it.

Everything a command *does* lives in `commands.py`, including who is
allowed to do it. What is left here is adaptation: take an interaction,
answer within Discord's three-second window, run the real work off the
event loop, and post the embed that comes back. Keeping it this thin is
what makes the rules testable without a gateway.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from hedge_fund.notify import commands, messages

logger = logging.getLogger(__name__)

TOKEN_ENV = "DISCORD_BOT_TOKEN"
GUILD_ENV = "DISCORD_GUILD_ID"

_INSTALL_HINT = (
    "the Discord bot needs discord.py — install it with "
    "`pip install 'aihf[discord]'` (the webhook notifier does not)"
)


class BotUnavailable(RuntimeError):
    """discord.py is not installed, or no bot token is configured."""


def require_discord():
    """The discord module, or a failure that says how to get it."""
    try:
        import discord  # noqa: PLC0415 - optional dependency, imported on use
    except ImportError as exc:
        raise BotUnavailable(_INSTALL_HINT) from exc
    return discord


def token_from_env(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    token = (env.get(TOKEN_ENV) or "").strip()
    if not token:
        raise BotUnavailable(
            f"{TOKEN_ENV} is not set — create an application at "
            "https://discord.com/developers/applications, copy its bot token, "
            "and put it in ~/.hedge-fund/.env"
        )
    return token


def build_client(*, env: dict | None = None, root: Path | None = None):
    """A discord.Client with the fund's commands registered on its tree.

    Returned rather than run, so a caller can set up logging, or a test can
    inspect the registered commands without a gateway.
    """
    discord = require_discord()
    from discord import app_commands

    env = os.environ if env is None else env
    guild_id = (env.get(GUILD_ENV) or "").strip()
    guild = discord.Object(id=int(guild_id)) if guild_id.isdigit() else None

    # Intents.none() is enough for slash commands: the bot reads no message
    # content, no member list and no presence. Nothing here needs a
    # privileged intent, and asking for one would be the wrong default.
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)

    async def answer(interaction, command: str, work, *args) -> None:
        """One command, start to finish: authorize, defer, run, reply.

        The defer matters — Discord drops an interaction that is not
        acknowledged within three seconds, and reading a ledger or starting
        a runner can take longer than that.
        """
        try:
            commands.authorize(
                command,
                user_id=getattr(interaction.user, "id", None),
                channel_id=getattr(interaction.channel, "id", None),
                env=env,
            )
        except commands.Denied as exc:
            await interaction.response.send_message(f"⛔ {exc}", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            embed = await asyncio.to_thread(work, *args)
        except commands.Denied as exc:
            await interaction.followup.send(f"⛔ {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 - a crash must not kill the gateway
            logger.exception("discord bot: /%s failed", command)
            await interaction.followup.send(
                embed=discord.Embed.from_dict(
                    messages.failure_embed(command, exc, context=f"/{command}")))
            return
        await interaction.followup.send(embed=discord.Embed.from_dict(embed))

    @tree.command(name="funds", description="Every deployed paper fund")
    async def _funds(interaction):
        await answer(interaction, "funds", lambda: commands.funds(root))

    @tree.command(name="status", description="NAV, last session, pending decision and book")
    @app_commands.describe(fund="the paper fund's name")
    async def _status(interaction, fund: str):
        await answer(interaction, "status", commands.status, fund, root)

    @tree.command(name="scorecard", description="Grade every analyst and strategy on the ledger")
    @app_commands.describe(fund="the paper fund's name")
    async def _scorecard(interaction, fund: str):
        await answer(interaction, "scorecard", commands.scorecard, fund, root)

    @tree.command(name="halt", description="Set the kill switch: ticks refuse to trade")
    @app_commands.describe(fund="the paper fund's name", reason="why — the next person reads this")
    async def _halt(interaction, fund: str, reason: str):
        await answer(interaction, "halt", commands.halt, fund, reason, root)

    @tree.command(name="resume", description="Clear the kill switch")
    @app_commands.describe(fund="the paper fund's name")
    async def _resume(interaction, fund: str):
        await answer(interaction, "resume", commands.resume, fund, root)

    @tree.command(name="run", description="Start the fund's runner")
    @app_commands.describe(fund="the paper fund's name")
    async def _run(interaction, fund: str):
        await answer(interaction, "run", commands.run, fund, root)

    @tree.command(name="stop", description="Stop the fund's runner after the session in flight")
    @app_commands.describe(fund="the paper fund's name")
    async def _stop(interaction, fund: str):
        await answer(interaction, "stop", commands.stop, fund, root)

    @client.event
    async def on_ready():
        # A guild-scoped sync lands immediately; a global one can take an
        # hour to propagate. DISCORD_GUILD_ID is worth setting for that alone.
        if guild is not None:
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        else:
            await tree.sync()
        logger.info("discord bot: connected as %s (%s commands)", client.user,
                    len(tree.get_commands()))

    client.aihf_tree = tree  # the tree is what a test wants to look at
    return client


def serve(*, token: str | None = None, env: dict | None = None,
          root: Path | None = None) -> None:
    """Connect and stay connected. Blocks until the process is stopped."""
    env = os.environ if env is None else env
    client = build_client(env=env, root=root)
    client.run(token or token_from_env(env), log_handler=None)
