"""What a Discord command actually does — with no Discord in sight.

Every command is a function from strings to an embed dict. The bot in
`bot.py` is then a thin adapter: parse the interaction, call one of these,
post what comes back. That split is the only reason this layer can be
tested at all — discord.py is an optional dependency, and the rules about
who may halt a fund are far too important to live inside a decorator.

Two kinds of command, and the difference matters:

* **Reads** (`funds`, `status`, `scorecard`) answer from the ledger.
* **Writes** (`halt`, `resume`, `run`, `stop`) change what the fund does
  next. They are refused unless the caller is on the allowlist — see
  `authorize`. A channel is a public place; a kill switch is not.
"""

from __future__ import annotations

from pathlib import Path

from hedge_fund.notify import messages
from hedge_fund.paths import PAPER_DIR

READ_COMMANDS = ("funds", "status", "scorecard")
WRITE_COMMANDS = ("halt", "resume", "run", "stop")

USERS_ENV = "DISCORD_ALLOWED_USER_IDS"
CHANNELS_ENV = "DISCORD_ALLOWED_CHANNEL_IDS"


class Denied(PermissionError):
    """The caller may not do this here. Carries what to say in the channel."""


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------

def parse_ids(value: str | None) -> frozenset[int]:
    """A comma- or space-separated list of Discord snowflakes. Junk is dropped
    rather than guessed at — a malformed id must not widen the allowlist."""
    if not value:
        return frozenset()
    out = set()
    for part in value.replace(",", " ").split():
        try:
            out.add(int(part.strip()))
        except ValueError:
            continue
    return frozenset(out)


def authorize(command: str, *, user_id: int | None, channel_id: int | None,
              env: dict) -> None:
    """Raise Denied unless this caller may run this command here.

    An empty channel allowlist means every channel the bot can see. An empty
    *user* allowlist means nobody may write: a bot invited to a server with
    no configuration should answer questions, not take a fund down. That is
    the deliberate asymmetry here.
    """
    channels = parse_ids(env.get(CHANNELS_ENV))
    if channels and channel_id not in channels:
        raise Denied("This channel is not on the fund's allowlist.")
    if command in READ_COMMANDS:
        return
    users = parse_ids(env.get(USERS_ENV))
    if not users:
        raise Denied(
            f"`/{command}` changes what the fund does, so it needs an allowlist: "
            f"set {USERS_ENV} to your Discord user id and restart the bot."
        )
    if user_id not in users:
        raise Denied(f"You are not on {USERS_ENV}.")


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------

def funds(root: Path | None = None) -> dict:
    """Every deployed paper fund, one line each."""
    from hedge_fund.paper import list_deployed

    root = root or PAPER_DIR
    lines = [_status_line(directory) for directory in list_deployed(root)]
    if not lines:
        return messages._embed(
            title="No paper funds",
            description="Deploy one with `aihf paper create <name> --mandate … --universe …`.",
            color=messages.GREY)
    return messages._embed(title=f"{len(lines)} paper funds",
                           description="```\n" + "\n".join(lines)[:3800] + "\n```",
                           color=messages.BLURPLE)


def status(name: str, root: Path | None = None) -> dict:
    """NAV, last session, the pending decision and the book."""
    from hedge_fund.paper import Ledger, load_deployed

    directory = _fund_dir(name, root)
    deployed = load_deployed(directory)
    ledger = Ledger(directory)
    latest = ledger.latest()
    halted = ledger.halted()
    if latest is None:
        # A fund can be halted before it has ever traded, and that is exactly
        # when someone asks why nothing is happening.
        opening = f"**HALTED:** {halted}\n\n" if halted else ""
        return messages._embed(
            title=f"{deployed.name} · not started",
            description=f"{opening}Deployed over {', '.join(deployed.universe)} with "
                        f"{messages._money(deployed.spec.capital)}. No session recorded yet.",
            color=messages.RED if halted else messages.GREY)

    embed = messages.session_embed(deployed.name, latest, capital=deployed.spec.capital)
    embed["title"] = f"{deployed.name} · status · last session {latest.session}"
    if halted:
        embed["color"] = messages.RED
        embed["description"] = f"**HALTED:** {halted}"
    return messages.fit(embed)


def scorecard(name: str, root: Path | None = None, limit: int = 8) -> dict:
    """Who has actually been right: analysts by IC, strategies by P&L."""
    from hedge_fund.paper import Ledger
    from hedge_fund.scorecard import score

    directory = _fund_dir(name, root)
    records = Ledger(directory).records()
    if not records:
        return messages._embed(title=f"{name} · scorecard",
                               description="No sessions recorded yet.", color=messages.GREY)
    card = score(records)
    analysts = sorted(card.analysts, key=lambda a: (a.ic_t_stat is None, -(a.ic_t_stat or 0)))
    strategies = sorted(card.strategies, key=lambda s: -s.pnl)
    fields = [
        messages._field("Analysts (IC t-stat · hit rate · views)",
                        _rows([f"**{a.name}** {_num(a.ic_t_stat)} · {_pct_or_dash(a.hit_rate)} · {a.n_views}"
                               for a in analysts[:limit]])),
        messages._field("Strategies (P&L · return · t-stat)",
                        _rows([f"**{s.name}** {messages._money(s.pnl)} · {messages._pct(s.return_pct)} · {_num(s.t_stat)}"
                               for s in strategies[:limit]])),
    ]
    return messages._embed(
        title=f"{card.fund} · scorecard",
        description=f"{card.first_session} → {card.last_session} · "
                    f"fund P&L {messages._money(card.fund_pnl)} · "
                    f"costs {messages._money(card.trading_cost)}",
        color=messages.BLURPLE, fields=fields)


def halt(name: str, reason: str, root: Path | None = None) -> dict:
    """Set the kill switch. Ticks refuse to trade until it is cleared."""
    from hedge_fund.paper import Ledger

    reason = (reason or "").strip()
    if not reason:
        raise Denied("A halt needs a reason — it is what the next person reads.")
    Ledger(_fund_dir(name, root)).halt(reason)
    return messages.halt_embed(name, reason)


def resume(name: str, root: Path | None = None) -> dict:
    from hedge_fund.paper import Ledger

    Ledger(_fund_dir(name, root)).resume()
    return messages.resume_embed(name)


def run(name: str, root: Path | None = None) -> dict:
    """Start the fund's runner as its own process, as the app's `space` does."""
    from hedge_fund.paper import is_running, spawn_runner

    directory = _fund_dir(name, root)
    if is_running(directory):
        return messages._embed(title=f"{name} · already running",
                               description="Its runner holds the fund; `/stop` first.",
                               color=messages.AMBER)
    pid = spawn_runner(directory)
    return messages._embed(title=f"{name} · running",
                           description=f"Runner started (pid {pid}). It advances the fund whenever "
                                       "a session is due, and posts each one here.",
                           color=messages.GREEN)


def stop(name: str, root: Path | None = None) -> dict:
    from hedge_fund.paper import is_running, stop_runner

    directory = _fund_dir(name, root)
    if not is_running(directory):
        return messages._embed(title=f"{name} · not running", description="Nothing to stop.",
                               color=messages.GREY)
    stop_runner(directory)
    return messages._embed(title=f"{name} · stopping",
                           description="It finishes the session in flight first.",
                           color=messages.AMBER)


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

def _fund_dir(name: str, root: Path | None) -> Path:
    """The fund's directory, or Denied with the names that do exist.

    `validate_fund_name` is what keeps a command from walking out of the
    paper directory — the name arrives from a chat message.
    """
    from hedge_fund.paper import list_deployed, validate_fund_name

    root = root or PAPER_DIR
    try:
        validate_fund_name(name)
    except ValueError as exc:
        raise Denied(str(exc)) from None
    directory = root / name
    if not directory.is_dir():
        known = ", ".join(d.name for d in list_deployed(root)) or "none deployed"
        raise Denied(f"No paper fund named `{name}` ({known}).")
    return directory


def _status_line(directory: Path) -> str:
    from hedge_fund.paper import is_running, Ledger, load_deployed

    try:
        deployed = load_deployed(directory)
    except ValueError as exc:
        return f"{directory.name:<16} unavailable: {exc}"
    ledger = Ledger(directory)
    latest = ledger.latest()
    halted = ledger.halted()
    if latest is None:
        state = "not started"
    else:
        state = (f"{latest.session}  NAV {latest.nav:>12,.2f}  "
                 f"{latest.nav / deployed.spec.capital - 1:+.2%}")
    flags = ("  RUNNING" if is_running(directory) else "") + (f"  HALTED: {halted}" if halted else "")
    return f"{deployed.name:<16} {state}{flags}"


def _rows(rows: list[str]) -> str:
    return "\n".join(rows) or "—"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def _pct_or_dash(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"
