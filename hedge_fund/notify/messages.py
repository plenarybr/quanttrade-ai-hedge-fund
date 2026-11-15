"""What the fund says in a channel, and nothing about how it gets there.

Pure formatting: a SessionRecord, a runner event or a backtest result in,
a Discord embed (a plain dict) out. No network, no env, no I/O — so every
message in this package is testable against the real records the engine
writes, and the transport in `discord.py` stays a dumb pipe.

Two rules hold throughout:

* **Every embed is clipped before it leaves.** Discord rejects an embed
  over its limits outright, and a fund with forty positions would write
  one. A clipped message is worth more than a 400.
* **Numbers keep their sign.** `+2.14%` and `-2.14%` have to be legible on
  a phone, at a glance, with no colour. Colour is a hint, never the fact.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Discord's documented limits. Exceeding any one of them is a 400, so the
# formatting clips to them rather than hoping.
TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FIELD_NAME_MAX = 256
FIELD_VALUE_MAX = 1024
FIELDS_MAX = 25
EMBED_TOTAL_MAX = 6000
CONTENT_MAX = 2000

GREEN = 0x2ECC71     # the session went fine
RED = 0xE74C3C       # halted, stopped out, failed
AMBER = 0xF1C40F     # a gap, a clamp worth seeing, a degraded run
BLURPLE = 0x5865F2   # neutral: a backtest, a status reply
GREY = 0x95A5A6

# How many rows of a list (orders, weights, positions) a message carries
# before it says "+N more". A channel is a glance, not a ledger.
ROWS = 8


# ---------------------------------------------------------------------------
# Paper sessions
# ---------------------------------------------------------------------------

def session_embed(fund: str, record, *, capital: float | None = None) -> dict:
    """One advanced session: the book now, what was executed, what is queued.

    *capital* is the mandate's starting capital when the caller has it, which
    is the only way a NAV becomes a return.
    """
    stopped = _is_stopped_out(record)
    executed = record.executed
    fields: list[dict] = []

    nav_line = _money(record.nav)
    if capital:
        nav_line += f"  ({_pct(record.nav / capital - 1)})"
    fields.append(_field("NAV", nav_line, inline=True))
    fields.append(_field("Cash", _money(record.cash), inline=True))
    fields.append(_field("Benchmark", f"{record.benchmark} {_money(record.benchmark_close)}",
                         inline=True))

    if executed is not None and executed.orders:
        cost = getattr(executed, "trading_cost", 0.0) or 0.0
        label = f"Executed · {len(executed.orders)} orders"
        if cost:
            label += f" · {_money(cost)} costs"
        fields.append(_field(label, _orders(executed.orders)))
    elif executed is not None:
        fields.append(_field("Executed", "nothing to trade: the book already matched the target"))

    if record.positions:
        fields.append(_field("Book", _positions(record.positions, record.marks)))

    decision = record.decision
    if decision is not None and not stopped:
        held = {t: w for t, w in decision.final_weights.items() if abs(w) > 1e-9}
        fields.append(_field(f"Decided · executes next session ({len(held)} names)",
                             _weights(held) if held else "flat: no name cleared the bar"))
        if decision.clamps:
            fields.append(_field("Risk clamps", _clamps(decision.clamps)))

    if record.executed is None and record.decision is None:
        fields.append(_field("No rebalance due", "the book was marked at this session's prices"))

    title = f"{fund} · {record.session}"
    if stopped:
        title += " · STOPPED OUT"
    return _embed(
        title=title,
        description=_stop_line(record) if stopped else None,
        color=RED if stopped else GREEN,
        fields=fields,
        footer=_footer(record),
    )


def event_embed(fund: str, kind: str, fields: dict) -> dict:
    """A runner event — a gap, a halt, a resume, a checkpoint.

    The engine's events are already named and already carry their reason;
    this only decides how loud each one looks.
    """
    reason = str(fields.get("reason") or "").strip()
    span = f"{fields.get('from') or 'start'} → {fields.get('to') or '?'}"
    detail = [f"**{span}**"]
    if "bars_skipped" in fields:
        detail.append(f"{fields['bars_skipped']} bars skipped")
    if reason:
        detail.append(reason)
    colour = {"halt": RED, "failure": RED, "gap": AMBER, "resume": GREEN}.get(kind, GREY)
    return _embed(title=f"{fund} · {kind}", description="\n".join(detail), color=colour)


def halt_embed(fund: str, reason: str) -> dict:
    """The kill switch, set. The loudest thing this fund can say."""
    return _embed(
        title=f"{fund} · HALTED",
        description=f"{reason}\n\nTicks refuse to trade until `aihf paper resume {fund}`.",
        color=RED,
    )


def resume_embed(fund: str) -> dict:
    return _embed(title=f"{fund} · resumed", description="The kill switch is clear.", color=GREEN)


def failure_embed(fund: str, exc: BaseException, *, context: str = "") -> dict:
    """A session that did not finish. The exception's words, not a stack."""
    where = f" during {context}" if context else ""
    return _embed(
        title=f"{fund} · failed{where}",
        description=f"```\n{_clip(str(exc) or exc.__class__.__name__, 1500)}\n```",
        color=RED,
    )


# ---------------------------------------------------------------------------
# Backtests
# ---------------------------------------------------------------------------

def backtest_embed(result, *, universe: list[str] | None = None, data_calls: int | None = None) -> dict:
    """A finished backtest: did it beat the thing it is measured against?"""
    m = result.metrics
    beat = m.excess_return_pct >= 0
    fields = [
        _field("Return", f"{_pct(m.total_return_pct)}  vs  {result.benchmark} {_pct(m.benchmark_return_pct)}"),
        _field("Excess", _pct(m.excess_return_pct), inline=True),
        _field("Sharpe", f"{m.sharpe_ratio:.2f}", inline=True),
        _field("Max drawdown", _pct(-abs(m.max_drawdown_pct)), inline=True),
        _field("Rebalances", f"{m.n_cycles}", inline=True),
        _field("Orders", f"{m.n_orders}", inline=True),
        _field("Costs", f"{_money(m.trading_cost)} on {_money(m.traded_notional)}", inline=True),
    ]
    if universe:
        fields.append(_field("Universe", ", ".join(universe)))
    footer = f"{len(result.dates)} sessions · {result.rebalance} rebalance · capital {_money(result.capital)}"
    if data_calls is not None:
        footer += f" · {data_calls} data calls"
    return _embed(
        title=f"{result.fund} · backtest {result.start} → {result.end}",
        description=("Beat the benchmark." if beat else "Lagged the benchmark."),
        color=GREEN if beat else AMBER,
        fields=fields,
        footer=footer,
    )


def status_embed(fund: str, line: str, *, halted: str | None = None) -> dict:
    """A status reply in a channel — what `aihf paper status` prints."""
    return _embed(
        title=f"{fund} · status",
        description=f"```\n{_clip(line, 1500)}\n```",
        color=RED if halted else BLURPLE,
    )


# ---------------------------------------------------------------------------
# Rendering pieces
# ---------------------------------------------------------------------------

def _orders(orders) -> str:
    rows = [f"`{o.side.upper():<4}` **{o.ticker}** {abs(o.quantity):,} @ {_money(o.price)}"
            for o in orders[:ROWS]]
    return _more(rows, len(orders))


def _positions(positions: dict, marks: dict) -> str:
    held = [(t, s) for t, s in sorted(positions.items()) if s]
    rows = [f"**{t}** {s:+,} @ {_money(marks.get(t, 0.0))}" for t, s in held[:ROWS]]
    return _more(rows, len(held)) if held else "flat"


def _weights(weights: dict) -> str:
    ordered = sorted(weights.items(), key=lambda kv: -abs(kv[1]))
    rows = [f"**{t}** {_pct(w)}" for t, w in ordered[:ROWS]]
    return _more(rows, len(ordered))


def _clamps(clamps) -> str:
    rows = []
    for clamp in clamps[:ROWS]:
        who = clamp.ticker or clamp.sector or "book"
        rows.append(f"`{clamp.limit}` {who}: {_pct(clamp.before)} → {_pct(clamp.after)}")
    return _more(rows, len(clamps))


def _more(rows: list[str], total: int) -> str:
    if total > len(rows):
        rows = rows + [f"…and {total - len(rows)} more"]
    return "\n".join(rows) or "—"


def _is_stopped_out(record) -> bool:
    decision = getattr(record, "decision", None)
    return bool(decision is not None and getattr(decision, "is_stopped_out", False))


def _stop_line(record) -> str:
    stop = next((c for c in record.decision.clamps if c.limit == "max_drawdown_pct"), None)
    if stop is None:  # pragma: no cover - a stop-out always records its clamp
        return "The drawdown stop fired: the book is sold next session."
    return (f"The drawdown stop fired at {_pct(stop.before)} from the NAV peak. "
            "Everything is sold next session.")


def _footer(record) -> str:
    parts = [f"{len(record.universe)} names"]
    if record.llm_model:
        parts.append(record.llm_model)
    if record.rebalance:
        parts.append("rebalance session")
    return " · ".join(parts)


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _pct(value: float) -> str:
    return f"{value:+.2%}"


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _field(name: str, value: str, *, inline: bool = False) -> dict:
    return {"name": _clip(name, FIELD_NAME_MAX),
            "value": _clip(value, FIELD_VALUE_MAX) or "—",
            "inline": inline}


def _embed(*, title: str, color: int, description: str | None = None,
           fields: list[dict] | None = None, footer: str | None = None) -> dict:
    embed = {
        "title": _clip(title, TITLE_MAX),
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if description:
        embed["description"] = _clip(description, DESCRIPTION_MAX)
    if fields:
        embed["fields"] = fields[:FIELDS_MAX]
    if footer:
        embed["footer"] = {"text": _clip(footer, 2048)}
    return fit(embed)


def fit(embed: dict) -> dict:
    """Drop trailing fields until the embed is under Discord's total budget.

    Characters count across title, description, field names and values, and
    the footer. Dropping from the end keeps the headline numbers, which are
    written first on purpose.
    """
    while _size(embed) > EMBED_TOTAL_MAX and embed.get("fields"):
        embed["fields"] = embed["fields"][:-1]
    if _size(embed) > EMBED_TOTAL_MAX and "description" in embed:
        room = EMBED_TOTAL_MAX - (_size(embed) - len(embed["description"]))
        embed["description"] = _clip(embed["description"], max(room, 0))
    return embed


def _size(embed: dict) -> int:
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    for field in embed.get("fields", []):
        total += len(field.get("name", "")) + len(field.get("value", ""))
    return total
