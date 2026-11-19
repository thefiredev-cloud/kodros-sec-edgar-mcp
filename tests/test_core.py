"""Offline tests: pure math and XBRL collapse. Run: uv run pytest"""
import pytest
from fin_sets import core


def test_dcf_matches_hand_calc():
    r = core.dcf(100, [0.0, 0.0], 0.0, 0.10, 0, 10)
    # flows 100,100 ; TV=100/0.10=1000 ; PV = 90.909+82.645+826.446
    assert r["enterprise_value"] == pytest.approx(1000.0, abs=0.01)
    assert r["value_per_share"] == pytest.approx(100.0, abs=0.01)


def test_dcf_rejects_bad_rates():
    with pytest.raises(ValueError):
        core.dcf(100, [0.05], 0.10, 0.08)


def test_cagr_and_none_safety():
    assert core.cagr(100, 200, 1) == pytest.approx(1.0)
    assert core.cagr(None, 200, 1) is None
    assert core.cagr(-5, 10, 2) is None
    assert core.safe_div(1, 0) is None and core.safe_div(None, 2) is None


def test_collapse_latest_filed_wins_and_flags_restatement():
    e = [
        {"start": "2024-01-01", "end": "2024-12-31", "val": 100, "filed": "2025-02-01", "accn": "a", "form": "10-K", "fy": 2024, "fp": "FY"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 110, "filed": "2026-02-01", "accn": "b", "form": "10-K", "fy": 2025, "fp": "FY"},
    ]
    rows = core._collapse(e, "duration", "annual")
    assert rows[0]["value"] == 110 and rows[0]["restated"] and rows[0]["first_reported_value"] == 100


def test_duration_filter_separates_quarter_and_year():
    e = [{"start": "2024-01-01", "end": "2024-03-31", "val": 1, "filed": "x", "accn": "q"},
         {"start": "2024-01-01", "end": "2024-12-31", "val": 4, "filed": "x", "accn": "y"}]
    assert [r["value"] for r in core._collapse(e, "duration", "annual")] == [4]
    assert [r["value"] for r in core._collapse(e, "duration", "quarterly")] == [1]


def test_q4_derivation():
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"start": "2024-01-01", "end": "2024-12-31", "val": 400, "filed": "2025-02-01", "accn": "fy", "form": "10-K", "fy": 2024, "fp": "FY"},
        {"start": "2024-01-01", "end": "2024-09-30", "val": 280, "filed": "2024-11-01", "accn": "q3", "form": "10-Q", "fy": 2024, "fp": "Q3"},
    ]}}}}}
    rows = core._series(facts, "revenue", "quarterly", "0000000001")
    rows = core._add_q4(facts, "revenue", rows, "0000000001")
    q4 = [r for r in rows if r["derived"]]
    assert q4 and q4[0]["value"] == 120 and "fy" in q4[0]["derivation"]


def test_extract_item_picks_body_not_toc():
    text = "Item 1A. Risk Factors\nItem 1B. Unresolved\n\nBody\nItem 1A. Risk Factors\n" + "risk sentence. " * 50 + "\nItem 1B. Unresolved Staff Comments\nnone"
    body, _ = core.extract_item(text, "1A")
    assert "risk sentence" in body and "Unresolved" not in body


def test_form4_parse_uses_safe_parser_and_values():
    xml = """<ownershipDocument><reportingOwner><reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CFO</officerTitle></reportingOwnerRelationship></reportingOwner>
    <nonDerivativeTable><nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle><transactionDate><value>2026-01-02</value></transactionDate>
    <transactionCoding><transactionCode>P</transactionCode></transactionCoding><transactionAmounts><transactionShares><value>100</value></transactionShares>
    <transactionPricePerShare><value>10.5</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
    </nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""
    r = core.parse_form4(xml)[0]
    assert r["value_usd"] == 1050.0 and r["code"] == "P" and r["roles"] == ["CFO"]


def test_xxe_blocked():
    from defusedxml.common import EntitiesForbidden
    bad = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><ownershipDocument>&e;</ownershipDocument>'
    with pytest.raises(EntitiesForbidden):
        core.parse_form4(bad)


def test_price_stats_drawdown():
    bars = [{"close": c, "adj_close": None} for c in (100, 110, 99, 120)]
    s = core.price_stats(bars)
    assert s["max_drawdown_pct"] == -10.0 and s["total_return_pct"] == 20.0


def test_instant_annual_excludes_interim_balances():
    e = [{"end": "2025-12-31", "val": 10, "filed": "2026-02-01", "accn": "k", "form": "10-K", "fp": "FY"},
         {"end": "2026-06-30", "val": 99, "filed": "2026-07-30", "accn": "q", "form": "10-Q", "fp": "Q2"}]
    assert [r["value"] for r in core._collapse(e, "instant", "annual")] == [10]
    assert [r["value"] for r in core._collapse(e, "instant", "quarterly")] == [99, 10]


# ---- signal engine (offline) ----
from fin_sets import signals_mkt as M, signals_sec as S


def _bars(closes, vols=None, hi=None, lo=None):
    vols = vols or [2_000_000] * len(closes)
    return [{"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": c, "high": (hi or c) , "low": (lo or c), "close": c, "adj_close": c, "volume": v}
            for i, (c, v) in enumerate(zip(closes, vols))]


def test_rsi_extremes():
    assert M.rsi(list(range(1, 40))) == 100.0
    assert M.rsi(list(range(40, 1, -1))) < 1


def test_flat_series_has_no_rsi_and_no_false_overbought_signal():
    # A price that never moves has no gains and no losses, so RSI is undefined, not 100.
    assert M.rsi([50.0] * 40) is None
    # 300 flat, liquid bars used to emit rsi_overbought (bearish) off that bogus 100.
    kinds = {s["kind"] for s in M.price_signals("FLAT", _bars([25.0] * 300, [2_000_000] * 300))}
    assert "rsi_overbought" not in kinds and "rsi_oversold" not in kinds


def test_breakout_needs_history_and_fires():
    closes = [100 + (i % 5) for i in range(260)] + [130]
    vols = [1_000_000] * 260 + [4_000_000]
    kinds = {s["kind"] for s in M.price_signals("TST", _bars(closes, vols))}
    assert "breakout_52w_high" in kinds and "volume_spike" in kinds


def test_illiquid_and_penny_filtered():
    assert M.price_signals("PNY", _bars([0.5] * 300)) == []
    assert M.price_signals("LOW", _bars([10] * 300, [10] * 300)) == []


def test_insider_buy_scoring_and_10b5_haircut():
    doc = {"issuer": {"cik": "0000000001", "name": "Acme", "ticker": "ACME"}, "owner": {"name": "Jane Doe", "roles": ["Chief Executive Officer"], "is_ceo_cfo": True},
           "rows": [{"date": "2026-09-01", "code": "P", "shares": 10000.0, "price": 100.0, "after": 50000.0, "ad": "A", "planned": False}], "planned": False}
    s = S.form4_signals(doc, "0000000001-26-000001", "2026-09-02", "0000000001", {})[0]
    assert s["kind"] == "insider_buy" and s["strength"] > 70 and s["value"] == 1_000_000
    doc["rows"][0]["planned"] = True
    assert S.form4_signals(doc, "0000000001-26-000002", "2026-09-02", "0000000001", {})[0]["strength"] < s["strength"]


def test_small_trades_and_awards_ignored():
    doc = {"issuer": {"cik": "0000000001", "name": "Acme", "ticker": "ACME"}, "owner": {"name": "X", "roles": [], "is_ceo_cfo": False},
           "rows": [{"date": "d", "code": "P", "shares": 10.0, "price": 10.0, "after": 1.0, "ad": "A", "planned": False},
                    {"date": "d", "code": "A", "shares": 1e6, "price": 50.0, "after": 1.0, "ad": "A", "planned": False}], "planned": False}
    assert S.form4_signals(doc, "a", "2026-09-02", "1", {}) == []


def test_form144_namespace_and_threshold():
    xml = ('<?xml version="1.0"?><edgarSubmission xmlns="http://www.sec.gov/edgar/ownership" xmlns:com="http://www.sec.gov/edgar/common"><formData><issuerInfo>'
           '<issuerCik>0000318306</issuerCik><issuerName>ABEONA</issuerName><nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold>Doe</nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold>'
           '</issuerInfo><securitiesInformation><noOfUnitsSold>5</noOfUnitsSold><aggregateMarketValue>5000000.00</aggregateMarketValue></securitiesInformation></formData></edgarSubmission>')
    d = S.parse_form144("<XML>" + xml + "</XML>")
    assert d["value"] == 5_000_000 and d["cik"] == "0000318306"
    assert S.form144_signals(d, "acc", "2026-09-29", {"0000318306": "ABEO"})[0]["kind"] == "insider_planned_sale"


def test_8k_item_classification():
    c = S.classify_items(["Results of Operations and Financial Condition", "Bankruptcy or Receivership", "Financial Statements and Exhibits"])
    assert [x[0] for x in c] == ["2.02", "1.03"]


def test_store_dedupe(tmp_path, monkeypatch):
    from fin_sets import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    conn = store.connect()
    sig = {"dedupe_key": "k", "asof": "2026-01-01", "category": "price", "kind": "gap", "direction": "bullish", "strength": 50, "headline": "h"}
    assert store.add_signals(conn, [sig]) == 1 and store.add_signals(conn, [sig]) == 0


def test_polymarket_move_needs_liquidity():
    m = [{"id": "1", "question": "Q?", "slug": "q", "liquidityNum": 500, "oneDayPriceChange": 0.3, "lastTradePrice": 0.6, "volume24hr": 1}]
    assert M.pm_signals_polymarket(m, "2026-09-29") == []
    m[0]["liquidityNum"] = 50_000
    assert M.pm_signals_polymarket(m, "2026-09-29")[0]["kind"] == "pm_probability_move"


# ---- self-evaluation layer (offline, synthetic) ----
import random as _r
from fin_sets import outcomes as O


def _mk_conn(tmp_path, monkeypatch):
    from fin_sets import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "e.db")
    conn = store.connect()
    O.ensure(conn)
    return conn


def _seed_universe(conn, n_tickers=220, days=90, seed=1, boost=None):
    """Random-walk bars on business-like dates. boost={ticker: (day_index, extra_return_next_5d)} injects a known edge."""
    rng = _r.Random(seed)
    dates = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(days)]
    for k in range(n_tickers):
        t = f"T{k:03d}"
        p = 50.0
        rows = []
        bumps = {}
        if boost and t in boost:
            for di, extra in boost[t]:
                for j in range(1, 6):
                    bumps[di + j] = extra / 5
        for i, d in enumerate(dates):
            p *= 1 + rng.gauss(0, 0.01) + bumps.get(i, 0)
            rows.append((t, d, p, p, p, p, p, 2_000_000))
        conn.executemany("INSERT INTO bars(ticker,date,open,high,low,close,adj,volume) VALUES(?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return dates


def _sig(conn, i, ticker, asof, direction="bullish", kind="k_test"):
    from fin_sets import store
    store.add_signals(conn, [{"dedupe_key": f"{kind}:{i}", "asof": asof, "category": "insider", "kind": kind, "ticker": ticker,
                              "direction": direction, "strength": 60, "headline": "h"}])


def test_control_matches_equal_weight_mean(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path, monkeypatch)
    dates = _seed_universe(conn)
    O.build_control(conn)
    d = dates[40]
    got = conn.execute("SELECT ret FROM control WHERE date=?", (d,)).fetchone()[0]
    rs = [conn.execute("SELECT adj FROM bars WHERE ticker=? AND date=?", (t, d)).fetchone()[0] / conn.execute("SELECT adj FROM bars WHERE ticker=? AND date=?", (t, dates[39])).fetchone()[0] - 1
          for t in [f"T{k:03d}" for k in range(220)]]
    assert got == pytest.approx(sum(rs) / len(rs), abs=1e-9)


def test_no_edge_random_signals_are_not_proven(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path, monkeypatch)
    dates = _seed_universe(conn)
    O.build_control(conn)
    rng = _r.Random(9)
    for i in range(400):
        _sig(conn, i, f"T{rng.randrange(220):03d}", dates[rng.randrange(10, 70)])
    O.resolve_outcomes(conn)
    g = [x for x in O.grade(conn) if x["h"] == 5][0]
    assert g["status"] in ("unproven", "thin", "promising") and abs(g["t_date"]) < 3.5


def test_injected_edge_is_detected(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path, monkeypatch)
    rng = _r.Random(3)
    boost = {}
    picks = []
    for i in range(300):
        t, di = f"T{rng.randrange(220):03d}", rng.randrange(10, 70)
        boost.setdefault(t, []).append((di, 0.06))
        picks.append((i, t, di))
    dates = _seed_universe(conn, boost=boost)
    O.build_control(conn)
    for i, t, di in picks:
        _sig(conn, i, t, dates[di])  # asof = day di, entry next bar, then +6% spread over following 5 bars
    O.resolve_outcomes(conn)
    g = [x for x in O.grade(conn) if x["h"] == 5][0]
    assert g["status"] == "proven_edge" and g["mean_excess"] > 0.02


def test_bearish_sign_flips(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path, monkeypatch)
    rng = _r.Random(5)
    boost, picks = {}, []
    for i in range(300):
        t, di = f"T{rng.randrange(220):03d}", rng.randrange(10, 70)
        boost.setdefault(t, []).append((di, -0.06))
        picks.append((i, t, di))
    dates = _seed_universe(conn, boost=boost)
    O.build_control(conn)
    for i, t, di in picks:
        _sig(conn, i, t, dates[di], direction="bearish")
    O.resolve_outcomes(conn)
    g = [x for x in O.grade(conn) if x["h"] == 5][0]
    assert g["status"] == "proven_edge"  # bearish call on a falling stock is a win


def test_entry_is_strictly_after_signal_date(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path, monkeypatch)
    dates = _seed_universe(conn, n_tickers=210, days=60)
    O.build_control(conn)
    _sig(conn, 1, "T001", dates[20])
    O.resolve_outcomes(conn)
    assert conn.execute("SELECT entry_date FROM outcomes WHERE h=5").fetchone()[0] == dates[21]


def test_replay_uses_no_future_bars(tmp_path, monkeypatch):
    conn = _mk_conn(tmp_path, monkeypatch)
    dates = _seed_universe(conn, n_tickers=3, days=300)
    from fin_sets import store
    seen = []
    orig = M.price_signals
    monkeypatch.setattr(M, "price_signals", lambda t, bars, *a: (seen.append((len(bars), bars[-1]["date"])) or []))
    monkeypatch.setattr(M, "universe", lambda limit=None: [])
    O.replay_price_history(conn, step=50, start_idx=252, tickers=1)
    assert all(n == conn.execute("SELECT count(*) FROM bars WHERE ticker='T000' AND date<=?", (d,)).fetchone()[0] for n, d in seen) and seen


def test_user_agent_is_required(monkeypatch):
    monkeypatch.delenv(core.UA_ENV, raising=False)
    with pytest.raises(RuntimeError, match="SEC_EDGAR_USER_AGENT"):
        core.user_agent()
    with pytest.raises(RuntimeError):
        core.fetch("https://www.sec.gov/files/company_tickers.json", cache=False)


def test_user_agent_from_env(monkeypatch):
    monkeypatch.setenv(core.UA_ENV, "test-app test@example.com")
    assert core.user_agent() == "test-app test@example.com"
