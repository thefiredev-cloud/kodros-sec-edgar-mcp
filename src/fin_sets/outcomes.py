"""Self-evaluation layer: stores bars, builds a whole-universe control, resolves forward outcomes for every signal, and grades each signal kind.

Grading is deliberately strict. A kind is "proven" only when its date-clustered t-stat clears 3 over >= 20 distinct entry dates.
Everything else is "unproven" or "thin". Strength scores are heuristics until this layer says otherwise.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date

from . import signals_mkt as M
from . import store

HORIZONS = (1, 5, 10, 20)
DIRECTIONAL = ("bullish", "bearish")
MIN_DATES_PROVEN = 20
T_PROVEN = 3.0

SCHEMA_EXTRA = """
CREATE TABLE IF NOT EXISTS outcomes(signal_id INTEGER, h INTEGER, entry_date TEXT, entry_px REAL, ret REAL, ctrl REAL, excess REAL, PRIMARY KEY(signal_id,h));
CREATE TABLE IF NOT EXISTS control(date TEXT PRIMARY KEY, ret REAL, n INTEGER);
CREATE TABLE IF NOT EXISTS edge(kind TEXT, h INTEGER, n INTEGER, n_dates INTEGER, mean_excess REAL, t_date REAL, share_positive REAL, status TEXT,
                                window_start TEXT, window_end TEXT, computed_at TEXT, PRIMARY KEY(kind,h));
CREATE INDEX IF NOT EXISTS ix_out_sig ON outcomes(signal_id);
"""


def ensure(conn) -> None:
    conn.executescript(SCHEMA_EXTRA)


# ------------------------------------------------------------------ bars

def is_liquid(bars: list[dict]) -> bool:
    if len(bars) < 60 or bars[-1]["close"] is None or bars[-1]["close"] < 1:
        return False
    tail = [b for b in bars[-20:] if b["close"] is not None and b["volume"] is not None]
    return len(tail) >= 15 and sum(b["close"] * b["volume"] for b in tail) / len(tail) >= 1_000_000


def store_bars(conn, ticker: str, bars: list[dict], keep: int = 520) -> None:
    rows = [(ticker, b["date"], b["open"], b["high"], b["low"], b["close"], b["adj_close"] or b["close"], b["volume"])
            for b in bars[-keep:] if b["close"] is not None]
    conn.executemany("INSERT OR REPLACE INTO bars(ticker,date,open,high,low,close,adj,volume) VALUES(?,?,?,?,?,?,?,?)", rows)


def load_bars(conn, ticker: str) -> list[dict]:
    return [{"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "adj_close": r[5], "volume": r[6]}
            for r in conn.execute("SELECT date,open,high,low,close,adj,volume FROM bars WHERE ticker=? ORDER BY date", (ticker,))]


# ------------------------------------------------------------------ control index

def build_control(conn) -> dict:
    """Equal-weight daily return of every stored liquid ticker. The fair benchmark for small-cap signals (SPY is not)."""
    ensure(conn)
    sums: dict[str, list] = defaultdict(lambda: [0.0, 0])
    prev_t, prev_px = None, None
    for t, d, px in conn.execute("SELECT ticker,date,adj FROM bars WHERE adj IS NOT NULL ORDER BY ticker,date"):
        if t == prev_t and prev_px and prev_px > 0:
            r = px / prev_px - 1
            if -0.5 <= r <= 0.5:
                s = sums[d]
                s[0] += r
                s[1] += 1
        prev_t, prev_px = t, px
    rows = [(d, s[0] / s[1], s[1]) for d, s in sums.items() if s[1] >= 200]
    conn.execute("DELETE FROM control")
    conn.executemany("INSERT INTO control(date,ret,n) VALUES(?,?,?)", rows)
    conn.commit()
    return {"control_days": len(rows), "min_names": min((r[2] for r in rows), default=0), "max_names": max((r[2] for r in rows), default=0)}


def _control_index(conn):
    rows = conn.execute("SELECT date,ret FROM control ORDER BY date").fetchall()
    dates = [r[0] for r in rows]
    cum = [0.0]
    for r in rows:
        cum.append(cum[-1] + math.log1p(r[1]))
    pos = {d: i + 1 for i, d in enumerate(dates)}  # cum[pos[d]] = log index at close of d
    return dates, cum, pos


# ------------------------------------------------------------------ outcomes

def resolve_outcomes(conn, horizons=HORIZONS) -> dict:
    ensure(conn)
    dates, cum, pos = _control_index(conn)
    if not dates:
        return {"error": "control index empty: run `sec-edgar-collect prices` then `outcomes`"}
    done = {(r[0], r[1]) for r in conn.execute("SELECT signal_id,h FROM outcomes")}
    by_ticker: dict[str, list] = defaultdict(list)
    for r in conn.execute("SELECT id,ticker,asof FROM signals WHERE ticker IS NOT NULL AND direction IN ('bullish','bearish')"):
        by_ticker[r["ticker"]].append((r["id"], r["asof"]))
    new = tickers_seen = 0
    batch = []
    for t, sigs in by_ticker.items():
        todo = [s for s in sigs if any((s[0], h) not in done for h in horizons)]
        if not todo:
            continue
        rows = conn.execute("SELECT date,adj FROM bars WHERE ticker=? AND adj IS NOT NULL ORDER BY date", (t,)).fetchall()
        if len(rows) < 30:
            continue
        tickers_seen += 1
        ds = [r[0] for r in rows]
        px = [r[1] for r in rows]
        import bisect
        for sid, asof in todo:
            i0 = bisect.bisect_right(ds, asof)  # first bar strictly after the signal date
            if i0 >= len(ds):
                continue
            for h in horizons:
                if (sid, h) in done or i0 + h >= len(ds):
                    continue
                e, x = ds[i0], ds[i0 + h]
                if e not in pos or x not in pos or px[i0] <= 0:
                    continue
                ret = px[i0 + h] / px[i0] - 1
                ctrl = math.expm1(cum[pos[x]] - cum[pos[e]])
                if abs(ret) > 5:  # corporate-action artefacts
                    continue
                batch.append((sid, h, e, px[i0], ret, ctrl, ret - ctrl))
                new += 1
        if len(batch) >= 5000:
            conn.executemany("INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?,?,?,?)", batch)
            conn.commit()
            batch = []
    if batch:
        conn.executemany("INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?,?,?,?)", batch)
    conn.commit()
    return {"outcomes_new": new, "tickers": tickers_seen}


# ------------------------------------------------------------------ grading

def _t(xs: list[float]) -> float | None:
    if len(xs) < 5:
        return None
    sd = statistics.stdev(xs)
    return None if sd == 0 else statistics.mean(xs) / (sd / math.sqrt(len(xs)))


def grade(conn, horizons=HORIZONS) -> list[dict]:
    """Per (kind, horizon): date-clustered t-stat of direction-signed excess return vs the universe control."""
    ensure(conn)
    agg: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    q = ("SELECT s.kind,s.direction,o.h,o.entry_date,o.excess FROM outcomes o JOIN signals s ON s.id=o.signal_id "
         "WHERE s.direction IN ('bullish','bearish')")
    for kind, direction, h, ed, ex in conn.execute(q):
        agg[(kind, h)][ed].append(ex if direction == "bullish" else -ex)
    out = []
    now = store.now_iso()
    for (kind, h), by_date in agg.items():
        per_date = [statistics.mean(v) for v in by_date.values()]
        allv = [x for v in by_date.values() for x in v]
        k = len(per_date)
        t = _t(per_date)
        mean = statistics.mean(per_date)
        if k < MIN_DATES_PROVEN:
            status = "thin"
        elif t is not None and t >= T_PROVEN and mean > 0:
            status = "proven_edge"
        elif t is not None and t <= -T_PROVEN:
            status = "anti_predictive"
        elif t is not None and t >= 2:
            status = "promising"
        else:
            status = "unproven"
        ds = sorted(by_date)
        out.append({"kind": kind, "h": h, "n": len(allv), "n_dates": k, "mean_excess": mean, "t_date": t,
                    "share_positive": sum(x > 0 for x in allv) / len(allv), "status": status,
                    "window_start": ds[0], "window_end": ds[-1], "computed_at": now})
    conn.execute("DELETE FROM edge")
    conn.executemany("INSERT INTO edge VALUES(:kind,:h,:n,:n_dates,:mean_excess,:t_date,:share_positive,:status,:window_start,:window_end,:computed_at)", out)
    conn.commit()
    return out


def explore(conn, kind: str, h: int = 5, min_seg: int = 40) -> dict:
    """Split-sample segment search. Segments are terciles of numeric features. Fit on the earlier half of entry dates, verify on the later half.
    A segment only 'replicates' when both halves share a sign and the out-of-sample clustered t clears 2. Expect ~5% false positives."""
    import json
    q = ("SELECT s.direction,s.strength,s.value,s.detail,o.entry_date,o.entry_px,o.excess FROM outcomes o JOIN signals s ON s.id=o.signal_id "
         "WHERE s.kind=? AND o.h=? AND s.direction IN ('bullish','bearish')")
    rows = []
    for r in conn.execute(q, (kind, h)):
        d = json.loads(r["detail"] or "{}")
        rows.append({"date": r["entry_date"], "x": r["excess"] if r["direction"] == "bullish" else -r["excess"],
                     "strength": r["strength"], "value": r["value"], "entry_px": r["entry_px"],
                     "pct_of_holdings": d.get("pct_of_holdings"), "volume_ratio": d.get("volume_ratio_20d"), "ret_1d": d.get("ret_1d_pct")})
    if len(rows) < 120:
        return {"kind": kind, "h": h, "n": len(rows), "note": "too few resolved outcomes to explore (need >= 120)"}
    dates = sorted({r["date"] for r in rows})
    split = dates[len(dates) // 2]

    def stats(sub):
        by = defaultdict(list)
        for r in sub:
            by[r["date"]].append(r["x"])
        pdv = [statistics.mean(v) for v in by.values()]
        return {"n": len(sub), "n_dates": len(pdv), "mean_excess_pct": round(statistics.mean(pdv) * 100, 2) if pdv else None,
                "t": None if _t(pdv) is None else round(_t(pdv), 2)}

    segs = []
    for feat in ("strength", "value", "entry_px", "pct_of_holdings", "volume_ratio", "ret_1d"):
        vals = sorted(r[feat] for r in rows if r[feat] is not None)
        if len(vals) < 3 * min_seg:
            continue
        lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
        for name, f in (("low", lambda v: v <= lo), ("mid", lambda v: lo < v <= hi), ("high", lambda v: v > hi)):
            sub = [r for r in rows if r[feat] is not None and f(r[feat])]
            ins, oos = stats([r for r in sub if r["date"] < split]), stats([r for r in sub if r["date"] >= split])
            if ins["n"] < min_seg // 2 or oos["n"] < min_seg // 2:
                continue
            rep = (ins["mean_excess_pct"] or 0) * (oos["mean_excess_pct"] or 0) > 0 and oos["t"] is not None and abs(oos["t"]) >= 2
            segs.append({"feature": feat, "bucket": name, "range": [None if name == "low" else round(lo, 3), None if name == "high" else round(hi, 3)],
                         "in_sample": ins, "out_of_sample": oos, "replicates": bool(rep)})
    segs.sort(key=lambda s: -abs(s["out_of_sample"]["t"] or 0))
    return {"kind": kind, "h": h, "n": len(rows), "split_date": split, "segments_tested": len(segs),
            "expected_false_positives": round(len(segs) * 0.05, 1), "replicating": [s for s in segs if s["replicates"]], "top_by_oos_t": segs[:8],
            "warning": "exploratory. A replicating segment is a lead to re-test on new data, not an edge."}


# ------------------------------------------------------------------ historical replay of price signals

def replay_price_history(conn, step: int = 5, start_idx: int = 252, tickers: int | None = None) -> dict:
    """Walk-forward replay: run the price detectors as of past dates using only bars up to that date. Signals are marked backfill=1."""
    ensure(conn)
    names = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM bars")]
    meta = {u["ticker"]: (u["name"], u["cik"]) for u in M.universe()}
    total = n_calls = 0
    batch = []
    tick_list = names[:tickers] if tickers else names
    for t in tick_list:
        bars = load_bars(conn, t)
        for i in range(start_idx, len(bars) - 1, step):
            sigs = M.price_signals(t, bars[: i + 1], *meta.get(t, (None, None)))
            n_calls += 1
            for s in sigs:
                s["backfill"] = 1
            batch.extend(sigs)
        if len(batch) >= 2000:
            total += store.add_signals(conn, batch)
            batch = []
    if batch:
        total += store.add_signals(conn, batch)
    return {"tickers": len(tick_list), "detector_runs": n_calls, "signals_new": total}


def status_map(conn, h: int = 5) -> dict:
    try:
        return {r["kind"]: dict(r) for r in conn.execute("SELECT * FROM edge WHERE h=?", (h,))}
    except Exception:  # noqa: BLE001  edge table not built yet
        return {}
