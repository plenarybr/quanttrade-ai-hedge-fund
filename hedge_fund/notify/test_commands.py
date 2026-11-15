"""What a command does to a real fund directory, and who may ask for it.

The authorization tests are the point of this file. A Discord channel is a
public place by default, and `/halt` stops a fund trading — so the rule
that a write needs an allowlist is pinned down here, where it is readable,
rather than inside a decorator in `bot.py`.
"""

from __future__ import annotations

import pytest

from hedge_fund.fund import load_spec
from hedge_fund.notify import commands
from hedge_fund.notify.commands import (
    authorize,
    Denied,
    funds,
    halt,
    parse_ids,
    resume,
    scorecard,
    status,
    stop,
)
from hedge_fund.paper import deploy, Ledger
from hedge_fund.paths import EXAMPLE_MANDATE


@pytest.fixture
def root(tmp_path):
    """A paper directory with one deployed fund, nothing recorded yet."""
    deploy("alpha", load_spec(EXAMPLE_MANDATE), ["AAPL", "MSFT"], root=tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------

def test_reads_need_no_allowlist():
    authorize("status", user_id=1, channel_id=2, env={})
    authorize("funds", user_id=None, channel_id=None, env={})


def test_a_write_with_no_allowlist_is_refused_and_says_how_to_fix_it():
    """A bot invited to a server with no configuration answers questions —
    it does not take a fund down."""
    with pytest.raises(Denied, match="DISCORD_ALLOWED_USER_IDS"):
        authorize("halt", user_id=1, channel_id=2, env={})


def test_a_write_from_someone_else_is_refused():
    env = {"DISCORD_ALLOWED_USER_IDS": "4242"}
    authorize("halt", user_id=4242, channel_id=7, env=env)

    with pytest.raises(Denied):
        authorize("halt", user_id=9999, channel_id=7, env=env)


def test_a_channel_allowlist_covers_reads_too():
    env = {"DISCORD_ALLOWED_CHANNEL_IDS": "100 200"}
    authorize("status", user_id=1, channel_id=200, env=env)

    with pytest.raises(Denied, match="channel"):
        authorize("status", user_id=1, channel_id=300, env=env)


def test_a_malformed_id_never_widens_the_allowlist():
    assert parse_ids("12, 34  56") == frozenset({12, 34, 56})
    assert parse_ids("not-an-id") == frozenset()
    assert parse_ids(None) == frozenset()

    with pytest.raises(Denied):
        authorize("halt", user_id=1, channel_id=1, env={"DISCORD_ALLOWED_USER_IDS": "oops"})


# ---------------------------------------------------------------------------
# Reading a fund
# ---------------------------------------------------------------------------

def test_funds_lists_what_is_deployed(root):
    embed = funds(root)
    assert "alpha" in embed["description"]


def test_funds_with_nothing_deployed_says_how_to_start(tmp_path):
    assert "aihf paper create" in funds(tmp_path)["description"]


def test_status_of_a_fund_with_no_sessions(root):
    embed = status("alpha", root)
    assert "not started" in embed["title"]
    assert "AAPL" in embed["description"]


def test_status_shows_the_halt_reason(root):
    Ledger(root / "alpha").halt("the feed is lying about SPY")

    embed = status("alpha", root)

    assert "HALTED" in embed["description"]
    assert "feed is lying" in embed["description"]


def test_scorecard_with_no_sessions_says_so(root):
    assert "No sessions" in scorecard("alpha", root)["description"]


def test_an_unknown_fund_lists_the_ones_that_exist(root):
    with pytest.raises(Denied, match="alpha"):
        status("beta", root)


def test_a_name_cannot_walk_out_of_the_paper_directory(root):
    """The name arrives from a chat message. It is not a path."""
    for name in ("../../etc", "..", "/etc/passwd", "alpha/../../x"):
        with pytest.raises(Denied):
            status(name, root)


# ---------------------------------------------------------------------------
# Changing a fund
# ---------------------------------------------------------------------------

def test_halt_sets_the_kill_switch_and_resume_clears_it(root):
    embed = halt("alpha", "data feed is lying", root)
    assert "HALTED" in embed["title"]
    assert Ledger(root / "alpha").halted() == "data feed is lying"

    resume("alpha", root)
    assert Ledger(root / "alpha").halted() is None


def test_a_halt_without_a_reason_is_refused(root):
    with pytest.raises(Denied, match="reason"):
        halt("alpha", "   ", root)


def test_stopping_a_fund_that_is_not_running_says_so(root):
    assert "not running" in stop("alpha", root)["title"]


def test_run_starts_a_runner_and_refuses_to_start_a_second(root, monkeypatch):
    started = []
    monkeypatch.setattr(commands, "PAPER_DIR", root)
    import hedge_fund.paper as paper
    monkeypatch.setattr(paper, "spawn_runner", lambda directory: started.append(directory) or 4242)
    monkeypatch.setattr(paper, "is_running", lambda directory: bool(started))

    first = commands.run("alpha", root)
    assert "4242" in first["description"]

    second = commands.run("alpha", root)
    assert "already running" in second["title"]
    assert len(started) == 1
