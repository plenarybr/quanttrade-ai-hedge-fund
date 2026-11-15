"""Every message, against the records the engine actually writes.

The formatting is pure, so these are the cheap tests: real SessionRecords
from the shared fixtures in, embeds out. What they pin down is the part a
person reads at 4am — the sign on a number, the word for a stop-out, and
the fact that nothing ever exceeds a limit Discord will reject.
"""

from __future__ import annotations

import pytest

from hedge_fund.brokers.models import Fill, Order
from hedge_fund.notify import messages
from hedge_fund.notify.messages import (
    backtest_embed,
    DESCRIPTION_MAX,
    EMBED_TOTAL_MAX,
    event_embed,
    failure_embed,
    FIELD_VALUE_MAX,
    halt_embed,
    session_embed,
)
from hedge_fund.pipeline import DecisionRecord
from hedge_fund.risk.limits import ClampEvent
from hedge_fund.tui.fixtures import make_cycle, make_session, make_signal


@pytest.fixture
def signal():
    return make_signal()


def _with_orders(signal, n=3, cost_cash=200.0):
    """A cycle that really traded: orders, their fills, and cash moved."""
    orders = [Order(ticker=f"T{i}", side="buy", quantity=10 + i, price=100.0 + i) for i in range(n)]
    fills = [Fill(ticker=o.ticker, side=o.side, quantity=o.quantity, price=o.price + 0.05)
             for o in orders]
    spent = sum(f.quantity * f.price for f in fills)
    return make_cycle(signal).model_copy(update={
        "orders": orders, "fills": fills, "cash": 100000 - spent - cost_cash,
    })


def _decision(signal, **update):
    """The assessment made at a session, executed at the next one."""
    cycle = make_cycle(signal)
    return DecisionRecord(
        fund=cycle.fund, as_of=cycle.as_of, spec=cycle.spec, universe=cycle.universe,
        marks=cycle.marks, skipped=cycle.skipped, strategies=cycle.strategies,
        target_weights=cycle.target_weights, clamps=cycle.clamps,
        final_weights=cycle.final_weights,
    ).model_copy(update=update)


def _stopped(signal):
    """A decision whose drawdown stop fired: everything sold next session."""
    return _decision(signal,
                     clamps=[ClampEvent(limit="max_drawdown_pct", before=-0.21, after=0.0)],
                     final_weights={})


# ---------------------------------------------------------------------------
# A session
# ---------------------------------------------------------------------------

def test_a_session_leads_with_nav_and_its_return():
    record = make_session("2026-03-02", nav=112_500.0)

    embed = session_embed("alpha", record, capital=100_000.0)

    assert embed["title"].startswith("alpha · 2026-03-02")
    nav = next(f for f in embed["fields"] if f["name"] == "NAV")
    assert "$112,500.00" in nav["value"]
    assert "+12.50%" in nav["value"]


def test_a_session_without_capital_still_reports_nav():
    """A caller that does not know the mandate's capital gets no return, not
    a wrong one."""
    embed = session_embed("alpha", make_session("2026-03-02", nav=112_500.0))

    nav = next(f for f in embed["fields"] if f["name"] == "NAV")
    assert nav["value"] == "$112,500.00"


def test_executed_orders_are_listed_with_their_side(signal):
    record = make_session("2026-03-02", executed=_with_orders(signal))

    embed = session_embed("alpha", record)

    executed = next(f for f in embed["fields"] if f["name"].startswith("Executed"))
    assert "3 orders" in executed["name"]
    assert "BUY" in executed["value"] and "**T0**" in executed["value"]


def test_a_long_order_list_says_how_many_it_left_out(signal):
    record = make_session("2026-03-02", executed=_with_orders(signal, n=20))

    embed = session_embed("alpha", record)

    executed = next(f for f in embed["fields"] if f["name"].startswith("Executed"))
    assert "and 12 more" in executed["value"]
    assert len(executed["value"]) <= FIELD_VALUE_MAX


def test_a_pending_decision_says_it_executes_next_session(signal):
    decision = _decision(signal, final_weights={"AAPL": 0.3, "MSFT": -0.1})
    record = make_session("2026-03-02", decision=decision)

    embed = session_embed("alpha", record)

    decided = next(f for f in embed["fields"] if f["name"].startswith("Decided"))
    assert "2 names" in decided["name"]
    assert "+30.00%" in decided["value"] and "-10.00%" in decided["value"]


def test_a_stop_out_is_red_and_says_so(signal):
    record = make_session("2026-03-02", decision=_stopped(signal))

    embed = session_embed("alpha", record)

    assert embed["color"] == messages.RED
    assert "STOPPED OUT" in embed["title"]
    assert "-21.00%" in embed["description"]
    assert "sold next session" in embed["description"]


def test_a_quiet_session_says_nothing_happened():
    embed = session_embed("alpha", make_session("2026-03-02"))

    assert any(f["name"] == "No rebalance due" for f in embed["fields"])
    assert embed["color"] == messages.GREEN


def test_a_session_never_exceeds_discords_budget(signal):
    """A fund with a hundred names must not produce a 400."""
    positions = {f"TICK{i}": i + 1 for i in range(100)}
    marks = {t: 100.0 + i for i, t in enumerate(positions)}
    decision = _decision(
        signal,
        final_weights={t: 0.01 for t in positions},
        clamps=[ClampEvent(limit="max_position_pct", ticker=t, before=0.4, after=0.25)
                for t in positions],
    )
    record = make_session("2026-03-02", positions=positions, marks=marks,
                          executed=_with_orders(signal, n=60), decision=decision)

    embed = session_embed("alpha", record, capital=100_000.0)

    assert messages._size(embed) <= EMBED_TOTAL_MAX
    assert all(len(f["value"]) <= FIELD_VALUE_MAX for f in embed["fields"])
    assert len(embed.get("fields", [])) <= messages.FIELDS_MAX


# ---------------------------------------------------------------------------
# Events and failures
# ---------------------------------------------------------------------------

def test_a_gap_carries_its_span_and_reason():
    embed = event_embed("alpha", "gap", {"from": "2026-03-02T15:30:00", "to": "2026-03-02T15:45:00",
                                         "reason": "no prints: upstream 503"})

    assert embed["color"] == messages.AMBER
    assert "2026-03-02T15:30:00 → 2026-03-02T15:45:00" in embed["description"]
    assert "upstream 503" in embed["description"]


def test_an_event_before_the_first_session_says_start():
    embed = event_embed("alpha", "gap", {"to": "2026-03-02", "bars_skipped": 7})

    assert "start → 2026-03-02" in embed["description"]
    assert "7 bars skipped" in embed["description"]


def test_a_halt_says_how_to_undo_it():
    embed = halt_embed("alpha", "data feed is lying about SPY")

    assert embed["color"] == messages.RED
    assert "aihf paper resume alpha" in embed["description"]


def test_a_failure_carries_the_message_not_a_stack():
    embed = failure_embed("alpha", RuntimeError("no close for NVDA on 2026-03-02"),
                          context="the runner")

    assert "failed during the runner" in embed["title"]
    assert "no close for NVDA" in embed["description"]
    assert "Traceback" not in embed["description"]


def test_a_very_long_failure_is_clipped():
    embed = failure_embed("alpha", RuntimeError("x" * 9000))

    assert len(embed["description"]) <= DESCRIPTION_MAX


# ---------------------------------------------------------------------------
# Backtests
# ---------------------------------------------------------------------------

class _Metrics:
    total_return_pct = 0.184
    annualized_return_pct = 0.12
    sharpe_ratio = 1.37
    max_drawdown_pct = 0.09
    benchmark_return_pct = 0.102
    excess_return_pct = 0.082
    n_cycles = 12
    n_orders = 48
    traded_notional = 412_000.0
    trading_cost = 318.44


class _Result:
    fund = "deep value"
    start = "2025-01-02"
    end = "2026-06-30"
    rebalance = "monthly"
    benchmark = "SPY"
    capital = 100_000.0
    dates = ["d"] * 377
    metrics = _Metrics()


def test_a_winning_backtest_says_it_beat_the_benchmark():
    embed = backtest_embed(_Result(), universe=["AAPL", "MSFT"], data_calls=1420)

    assert embed["color"] == messages.GREEN
    assert embed["description"] == "Beat the benchmark."
    assert "+18.40%" in embed["fields"][0]["value"]
    assert "SPY +10.20%" in embed["fields"][0]["value"]
    assert "1420 data calls" in embed["footer"]["text"]


def test_a_losing_backtest_is_amber_and_says_so():
    result = _Result()
    result.metrics = _Metrics()
    result.metrics.excess_return_pct = -0.03

    embed = backtest_embed(result)

    assert embed["color"] == messages.AMBER
    assert embed["description"] == "Lagged the benchmark."


def test_a_drawdown_reads_as_a_loss():
    """max_drawdown_pct is stored unsigned; a channel should not read it as a gain."""
    embed = backtest_embed(_Result())

    drawdown = next(f for f in embed["fields"] if f["name"] == "Max drawdown")
    assert drawdown["value"] == "-9.00%"
