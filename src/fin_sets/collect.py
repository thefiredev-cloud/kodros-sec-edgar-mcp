"""sec-edgar-mcp collector CLI. Fills the signal store; run it by hand or from a scheduler.

  sec-edgar-collect sec   [--days N] [--limit N]     EDGAR daily index sweep (Form 4, 144, 8-K, 13D/G, NT, 424B, 25-NSE)
  sec-edgar-collect prices [--limit N] [--start N]   daily bars + technical signals for the SEC ticker universe
  sec-edgar-collect pm                               Polymarket + Kalshi
  sec-edgar-collect all                              everything, then cluster detection
  sec-edgar-collect outcomes                         control index, 1/5/10/20 day returns, grade every kind
  sec-edgar-collect replay [--step N] [--tickers N]  walk-forward replay of the price detectors on stored bars
  sec-edgar-collect stats
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import date, timedelta

from . import core
from . import outcomes as O
from . import signals_mkt as M
from . import signals_sec as S
from . import store


def _run(conn, module: str, fn):
    rid = conn.execute("INSERT INTO runs(module,started,status) VALUES(?,?,?)", (module, store.now_iso(), "running")).lastrowid
    conn.commit()
    try:
        res = fn()
        conn.execute("UPDATE runs SET finished=?,status=?,n_new=?,detail=? WHERE id=?",
                     (store.now_iso(), "ok", int(res.get("signals_new", 0)) if isinstance(res, dict) else 0, json.dumps(res, default=str), rid))
        conn.commit()
        print(module, json.dumps(res, default=str))
        return res
    except Exception as e:  # noqa: BLE001
        conn.execute("UPDATE runs SET finished=?,status=?,error=? WHERE id=?", (store.now_iso(), "error", f"{type(e).__name__}: {e}", rid))
        conn.commit()
        print(module, "ERROR", e, file=sys.stderr)
        return {"error": str(e)}


def do_sec(conn, days: int, limit: int | None):
    def go():
        agg = {"days": [], "signals_new": 0}
        for i in range(days):
            d = date.today() - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            st = S.collect_day(conn, d, limit=limit)
            agg["days"].append(st)
            agg["signals_new"] += st["signals_new"]
            print(f"sec day {st['date']} index={st['index_rows']} todo={st['todo']} new={st['signals_new']} errors={st['errors']} cumulative={agg['signals_new']}", flush=True)
        return agg
    return _run(conn, "sec", go)


def do_prices(conn, limit: int | None, start: int):
    def go():
        u = M.universe()
        chunk = u[start:start + limit] if limit else u[start:]
        return M.collect_prices(conn, chunk)
    return _run(conn, "prices", go)


def do_outcomes(conn):
    def go():
        c = O.build_control(conn)
        o = O.resolve_outcomes(conn)
        g = O.grade(conn)
        by = {}
        for r in g:
            if r["h"] == 5:
                by[r["status"]] = by.get(r["status"], 0) + 1
        return {"control": c, "outcomes": o, "kinds_graded_h5": by, "signals_new": 0}
    return _run(conn, "outcomes", go)


def do_cluster(conn):
    return _run(conn, "cluster", lambda: {"signals_new": store.add_signals(conn, S.cluster_buys(conn))})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sec-edgar-collect")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("sec"); a.add_argument("--days", type=int, default=1); a.add_argument("--limit", type=int)
    b = sub.add_parser("prices"); b.add_argument("--limit", type=int); b.add_argument("--start", type=int, default=0)
    sub.add_parser("pm")
    c = sub.add_parser("all"); c.add_argument("--days", type=int, default=2); c.add_argument("--price-limit", type=int)
    sub.add_parser("outcomes")
    r = sub.add_parser("replay"); r.add_argument("--step", type=int, default=5); r.add_argument("--tickers", type=int)
    sub.add_parser("stats")
    args = ap.parse_args(argv)
    if args.cmd != "stats":
        try:
            core.user_agent()
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        # one collector at a time: two would double the SEC request rate (fair-access limit is 10 req/s)
        lock_path = os.path.join(os.path.dirname(store.DB_PATH), "collector.lock")
        lf = open(lock_path, "w")
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another sec-edgar-collect process holds the lock; skipping this run", flush=True)
            return
    conn = store.connect()
    if args.cmd == "sec":
        do_sec(conn, args.days, args.limit); do_cluster(conn)
    elif args.cmd == "prices":
        do_prices(conn, args.limit, args.start)
    elif args.cmd == "pm":
        _run(conn, "pm", lambda: M.collect_pm(conn))
    elif args.cmd == "outcomes":
        do_outcomes(conn)
    elif args.cmd == "replay":
        _run(conn, "replay", lambda: O.replay_price_history(conn, step=args.step, tickers=args.tickers))
    elif args.cmd == "all":
        do_sec(conn, args.days, None); _run(conn, "pm", lambda: M.collect_pm(conn)); do_prices(conn, args.price_limit, 0); do_cluster(conn); do_outcomes(conn)
    if args.cmd == "stats" or True:
        print(json.dumps(stats(conn), indent=1))


def stats(conn) -> dict:
    by_kind = {r["kind"]: r["n"] for r in conn.execute("SELECT kind,count(*) n FROM signals GROUP BY kind ORDER BY n DESC")}
    by_day = {r["asof"]: r["n"] for r in conn.execute("SELECT asof,count(*) n FROM signals GROUP BY asof ORDER BY asof DESC LIMIT 7")}
    return {"total": sum(by_kind.values()), "by_kind": by_kind, "last_7_asof_days": by_day}


if __name__ == "__main__":
    main()
