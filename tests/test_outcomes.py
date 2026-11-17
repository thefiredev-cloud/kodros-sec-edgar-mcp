"""Deterministic offline checks for outcome resolution and date-clustered grades."""

import math
from datetime import date, timedelta

import pytest

from fin_sets import outcomes, store


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "outcomes.db")
    connection = store.connect()
    outcomes.ensure(connection)
    try:
        yield connection
    finally:
        connection.close()


def add_signal(conn, key, direction="bullish", asof="2026-01-01"):
    store.add_signals(conn, [{
        "dedupe_key": key, "asof": asof, "category": "price",
        "kind": "test_edge", "ticker": "TEST", "direction": direction,
        "strength": 60, "headline": "Synthetic signal",
    }])
    return conn.execute("SELECT id FROM signals WHERE dedupe_key=?", (key,)).fetchone()[0]


def add_outcome(conn, key, entry_date, excess, direction="bullish"):
    sid = add_signal(conn, key, direction)
    conn.execute("INSERT INTO outcomes VALUES(?,?,?,?,?,?,?)",
                 (sid, 5, entry_date, 100, excess, 0, excess))


def test_grade_weights_entry_dates_not_signal_counts(conn):
    for i in range(5):
        add_outcome(conn, str(i), "2026-01-02", 0.1)
    add_outcome(conn, "bearish", "2026-01-03", 0.1, "bearish")
    add_outcome(conn, "neutral", "2026-01-04", 5, "neutral")

    grade, = outcomes.grade(conn)

    assert grade["n"] == 6
    assert grade["n_dates"] == 2
    assert grade["mean_excess"] == pytest.approx(0)
    assert grade["share_positive"] == pytest.approx(5 / 6)
    assert grade["t_date"] is None
    assert grade["status"] == "thin"
    assert (grade["window_start"], grade["window_end"]) == ("2026-01-02", "2026-01-03")
    assert dict(conn.execute("SELECT * FROM edge").fetchone()) == grade


@pytest.mark.parametrize("n_dates, mean, expected", [
    (19, 0.04, "thin"),
    (20, 0.04, "proven_edge"),
    (20, -0.04, "anti_predictive"),
    (20, 0.01 * 2.5 / math.sqrt(19), "promising"),
    (20, 0, "unproven"),
])
def test_grade_evidence_thresholds(conn, n_dates, mean, expected):
    for i in range(n_dates):
        entry_date = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        add_outcome(conn, str(i), entry_date, mean + (0.01 if i % 2 else -0.01))

    grade, = outcomes.grade(conn)

    assert grade["status"] == expected
    assert grade["n_dates"] == n_dates
    if n_dates == 20:
        assert grade["t_date"] == pytest.approx(mean * math.sqrt(19) / 0.01)


def test_grade_constant_returns_do_not_claim_statistical_proof(conn):
    for i in range(20):
        add_outcome(conn, str(i), (date(2026, 1, 1) + timedelta(days=i)).isoformat(), 0.02)

    grade, = outcomes.grade(conn)

    assert grade["mean_excess"] == pytest.approx(0.02)
    assert grade["t_date"] is None
    assert grade["status"] == "unproven"


@pytest.fixture
def price_history(conn):
    dates = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(30)]
    # Raw prices stay flat; adjusted prices capture the actual forward return.
    adjusted = [90, 100, 110] + [121] * 27
    conn.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)", [
        ("TEST", day, 50, 50, 50, 50, px, 1_000_000)
        for day, px in zip(dates, adjusted)
    ])
    conn.executemany("INSERT INTO control VALUES(?,?,?)", [
        (dates[1], 0.5, 200), (dates[2], 0.02, 200), (dates[3], 0.03, 200),
    ])
    conn.commit()
    return dates


def test_resolve_compounds_control_after_entry_and_is_idempotent(conn, price_history):
    sid = add_signal(conn, "early", asof=price_history[0])
    add_signal(conn, "neutral", "neutral", price_history[0])

    assert outcomes.resolve_outcomes(conn, horizons=(2,)) == {"outcomes_new": 1, "tickers": 1}
    row, = conn.execute("SELECT * FROM outcomes").fetchall()
    assert (row["signal_id"], row["h"], row["entry_date"], row["entry_px"]) == (
        sid, 2, price_history[1], 100,
    )
    assert row["ret"] == pytest.approx(0.21)
    assert row["ctrl"] == pytest.approx(1.02 * 1.03 - 1)
    assert row["excess"] == pytest.approx(0.21 - (1.02 * 1.03 - 1))
    assert outcomes.resolve_outcomes(conn, horizons=(2,)) == {"outcomes_new": 0, "tickers": 0}
    assert conn.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 1


@pytest.mark.parametrize("reason", ["unmatured", "missing_control", "zero_entry", "artefact"])
def test_resolve_skips_unusable_forward_returns(conn, price_history, reason):
    asof = price_history[-2] if reason == "unmatured" else price_history[0]
    add_signal(conn, reason, asof=asof)
    if reason == "missing_control":
        conn.execute("DELETE FROM control WHERE date=?", (price_history[3],))
    elif reason == "zero_entry":
        conn.execute("UPDATE bars SET adj=0 WHERE date=?", (price_history[1],))
    elif reason == "artefact":
        conn.execute("UPDATE bars SET adj=601 WHERE date=?", (price_history[3],))
    conn.commit()

    assert outcomes.resolve_outcomes(conn, horizons=(2,))["outcomes_new"] == 0
    assert conn.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 0
