"""sec-edgar-mcp: read-only finance MCP server. Provenance on every number, deterministic math.

No LLM sits in the data path. Every figure traces to an SEC accession or a named endpoint with a
fetch time. Missing data is null, never zero.
Not investment advice. No order, transfer or account tools exist here by design.
"""

from __future__ import annotations

import re
import sys
from typing import Any, Literal

from fastmcp import FastMCP

from . import core

mcp = FastMCP(
    "sec-edgar-mcp",
    instructions=(
        "Finance research over SEC EDGAR XBRL, filings text, insider Form 4 and price history. "
        "Every row carries source, accession, filed date and fetch time. Null means unavailable, not zero. "
        "Prefer fs_company_snapshot first, then drill down. Read-only."
    ),
)

DISCLAIMER = "Research data only. Not investment advice. Verify material figures against the linked filing."


def _err(e: Exception) -> dict:
    return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool
def fs_find_company(query: str, limit: int = 10) -> dict:
    """Resolve a ticker, CIK or name fragment to SEC-registered companies (ticker, 10-digit CIK, name)."""
    try:
        return {"matches": core.search_companies(query, limit), "source": "SEC company_tickers.json"}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_list_filings(company: str, forms: list[str] | None = None, limit: int = 15, since: str | None = None) -> dict:
    """List recent SEC filings for a company. forms e.g. ["10-K","10-Q","8-K"]; since as YYYY-MM-DD. Returns accession, dates and document URLs."""
    try:
        c = core.must_resolve(company)
        return core.filings(c["cik"], forms, limit, since) | {"ticker": c["ticker"]}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_financials(company: str, metric: str, frequency: Literal["annual", "quarterly"] = "annual", periods: int = 8) -> dict:
    """Reported XBRL series for one metric with provenance per period (accession, form, filed date, tag).
    Restated periods are flagged and show the first reported value. Quarterly Q4 is derived (FY minus 9M YTD) and labelled derived=true.
    Metrics: revenue, cost_of_revenue, gross_profit, rd_expense, sga_expense, operating_income, interest_expense, pretax_income, income_tax,
    net_income, eps_diluted, shares_diluted, depreciation_amortization, stock_comp, operating_cash_flow, capex, buybacks, dividends_paid,
    cash, current_assets, total_assets, current_liabilities, total_liabilities, long_term_debt, short_term_debt, equity, shares_outstanding."""
    try:
        c = core.must_resolve(company)
        return core.series(c["cik"], metric, frequency, periods) | {"ticker": c["ticker"], "cik": c["cik"]}
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _latest(cik: str, metric: str, freq: str = "annual", n: int = 1) -> list[dict]:
    return core.series(cik, metric, freq, n)["rows"]


@mcp.tool
def fs_company_snapshot(company: str) -> dict:
    """One-call fundamental snapshot from the latest annual filing: scale, margins, returns, leverage, liquidity, cash conversion,
    5-year CAGRs. Each ratio lists its inputs and filing so it can be audited. Nulls mean the company did not report the input."""
    try:
        c = core.must_resolve(company)
        cik = c["cik"]
        ref_end = None

        def g(m: str):
            nonlocal ref_end
            rows = _latest(cik, m, "annual", 1)
            r = rows[0] if rows else None
            if r is None:
                return None
            if ref_end is None:
                ref_end = r["period_end"]  # first metric fetched is revenue: anchors the fiscal year
            # never mix fiscal years inside one ratio: a stale or newer input becomes null
            return r if abs(core._days(min(ref_end, r["period_end"]), max(ref_end, r["period_end"]))) <= 10 else None
        v = lambda r: None if r is None else r["value"]  # noqa: E731
        rev, cogs, gp = g("revenue"), g("cost_of_revenue"), g("gross_profit")
        opi, ni, pretax, tax = g("operating_income"), g("net_income"), g("pretax_income"), g("income_tax")
        ocf, capex = g("operating_cash_flow"), g("capex")
        assets, eq, ltd, std, cash = g("total_assets"), g("equity"), g("long_term_debt"), g("short_term_debt"), g("cash")
        ca, cl, intexp, da = g("current_assets"), g("current_liabilities"), g("interest_expense"), g("depreciation_amortization")
        gross = v(gp) if gp else (v(rev) - v(cogs) if rev and cogs else None)
        debt = None if (ltd is None and std is None) else (v(ltd) or 0) + (v(std) or 0)
        fcf = None if (ocf is None or capex is None) else v(ocf) - v(capex)
        ebitda = None if (opi is None or da is None) else v(opi) + v(da)
        rev_rows = _latest(cik, "revenue", "annual", 6)
        ni_rows = _latest(cik, "net_income", "annual", 6)
        rc = core.cagr(rev_rows[-1]["value"], rev_rows[0]["value"], len(rev_rows) - 1) if len(rev_rows) >= 3 else None
        nc = core.cagr(ni_rows[-1]["value"], ni_rows[0]["value"], len(ni_rows) - 1) if len(ni_rows) >= 3 else None
        ratios = {
            "gross_margin_pct": core.pct(core.safe_div(gross, v(rev))),
            "operating_margin_pct": core.pct(core.safe_div(v(opi), v(rev))),
            "net_margin_pct": core.pct(core.safe_div(v(ni), v(rev))),
            "effective_tax_rate_pct": core.pct(core.safe_div(v(tax), v(pretax))),
            "roe_pct_year_end_equity": core.pct(core.safe_div(v(ni), v(eq))),
            "roa_pct_year_end_assets": core.pct(core.safe_div(v(ni), v(assets))),
            "current_ratio": None if core.safe_div(v(ca), v(cl)) is None else round(core.safe_div(v(ca), v(cl)), 2),
            "debt_to_equity": None if core.safe_div(debt, v(eq)) is None else round(core.safe_div(debt, v(eq)), 2),
            "interest_coverage_x": None if core.safe_div(v(opi), v(intexp)) is None else round(core.safe_div(v(opi), v(intexp)), 2),
            "cash_conversion_ocf_to_net_income": None if core.safe_div(v(ocf), v(ni)) is None else round(core.safe_div(v(ocf), v(ni)), 2),
            "fcf_margin_pct": core.pct(core.safe_div(fcf, v(rev))),
            "revenue_cagr_pct": core.pct(rc), "net_income_cagr_pct": core.pct(nc),
            "cagr_span_years": len(rev_rows) - 1 if len(rev_rows) >= 3 else None,
        }
        levels = {"revenue": v(rev), "net_income": v(ni), "operating_cash_flow": v(ocf), "capex": v(capex), "free_cash_flow": fcf,
                  "ebitda_reported_da": ebitda, "total_debt": debt, "cash": v(cash),
                  "net_debt": None if debt is None or cash is None else debt - v(cash), "equity": v(eq), "total_assets": v(assets)}
        ref = rev or ni or ocf
        return {"company": c, "fiscal_period_end": ref["period_end"] if ref else None,
                "filing": {"form": ref["form"], "filed": ref["filed"], "accession": ref["accession"], "url": ref["url"]} if ref else None,
                "levels_usd": levels, "ratios": ratios,
                "restated_inputs": [m for m, r in [("revenue", rev), ("net_income", ni), ("operating_income", opi)] if r and r["restated"]],
                "notes": "ROE/ROA use year-end balances, not averages. Debt excludes leases unless tagged in the debt lines. " + DISCLAIMER,
                "source": "SEC EDGAR XBRL companyfacts"}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_compare(companies: list[str], metrics: list[str] | None = None) -> dict:
    """Side by side latest annual ratios for up to 8 companies. Default metrics: margins, ROE, leverage, growth. Same ratio definitions as fs_company_snapshot."""
    want = metrics or ["gross_margin_pct", "operating_margin_pct", "net_margin_pct", "roe_pct_year_end_equity", "debt_to_equity",
                       "fcf_margin_pct", "revenue_cagr_pct"]
    rows = []
    for q in companies[:8]:
        s = fs_company_snapshot(q)
        if "error" in s:
            rows.append({"company": q, "error": s["error"]})
            continue
        rows.append({"ticker": s["company"]["ticker"], "name": s["company"]["name"], "fiscal_period_end": s["fiscal_period_end"],
                     **{m: s["ratios"].get(m) for m in want}, "revenue_usd": s["levels_usd"]["revenue"]})
    return {"rows": rows, "note": "Fiscal year ends differ across companies. " + DISCLAIMER}


@mcp.tool
def fs_read_filing_section(company: str, form: str = "10-K", item: str = "1A", accession: str | None = None, max_chars: int = 12000, offset: int = 0) -> dict:
    """Read one Item from a filing as plain text, e.g. item "1A" risk factors, "7" MD&A, "1" business. Defaults to the latest filing of that form;
    pass accession for a specific one. Use offset to page. Returns URL and filed date."""
    try:
        c = core.must_resolve(company)
        fl = core.filings(c["cik"], [form], 25)["filings"]
        if accession:
            fl = [f for f in fl if f["accession"] == accession]
        if not fl:
            return {"error": f"No {form} filing found for {c['ticker'] or c['name']}" + (f" with accession {accession}" if accession else "")}
        f = fl[0]
        t = core.filing_text(f["url"])
        sec = core.extract_item(t["text"], item)
        if not sec:
            return {"error": f"Item {item} not located in {f['url']}. Use fs_search_filing for keyword search.", "filing": f}
        body, start = sec
        return {"filing": f, "item": item, "total_chars": len(body), "offset": offset, "text": body[offset:offset + max_chars],
                "truncated": offset + max_chars < len(body), "next_offset": offset + max_chars if offset + max_chars < len(body) else None,
                "fetched_at": t["fetched_at"]}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_search_filing(company: str, query: str, form: str = "10-K", accession: str | None = None, limit: int = 6) -> dict:
    """Keyword passage search inside a filing (latest of form, or a given accession). Passages are ranked by distinct term coverage. Quotes are verbatim."""
    try:
        c = core.must_resolve(company)
        fl = core.filings(c["cik"], [form], 25)["filings"]
        if accession:
            fl = [f for f in fl if f["accession"] == accession]
        if not fl:
            return {"error": f"No {form} filing found"}
        f = fl[0]
        t = core.filing_text(f["url"])
        return {"filing": f, "query": query, "passages": core.search_text(t["text"], query, limit=limit)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_insider_trades(company: str, days: int = 180, limit: int = 12) -> dict:
    """Recent Form 4 insider transactions parsed from the filings themselves: owner, role, code, shares, price, value, holdings after.
    Code P is an open-market buy, S a sale; A/M/F are grants, exercises and tax withholding and are not conviction signals."""
    try:
        from datetime import date, timedelta
        c = core.must_resolve(company)
        since = (date.today() - timedelta(days=days)).isoformat()
        fl = core.filings(c["cik"], ["4"], limit, since)["filings"]
        out = []
        for f in fl:
            try:
                # primaryDocument points at the XSL-rendered html; the raw xml lives beside it
                raw = re.sub(r"/xsl[^/]*/", "/", f["url"])
                xml = core.fetch(raw, ttl=30 * 86400, as_text=True)["body"]
                for tx in core.parse_form4(xml):
                    out.append(tx | {"filed": f["filed"], "accession": f["accession"], "url": f["url"],
                                     "meaning": core.FORM4_CODES.get(tx["code"], tx["code"])})
            except Exception as e:  # noqa: BLE001
                out.append({"accession": f["accession"], "filed": f["filed"], "error": f"{type(e).__name__}: {e}"})
        buys = sum(t.get("value_usd") or 0 for t in out if t.get("code") == "P")
        sells = sum(t.get("value_usd") or 0 for t in out if t.get("code") == "S")
        return {"company": c, "since": since, "transactions": out, "open_market_buys_usd": round(buys, 2), "open_market_sells_usd": round(sells, 2),
                "source": "SEC EDGAR Form 4"}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_price_history(symbol: str, range: Literal["1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "max"] = "1y", include_bars: bool = False) -> dict:
    """Price stats from daily bars: last price with timestamp, total return, annualized volatility, max drawdown (adjusted close).
    include_bars=true returns the OHLCV rows. Source is unofficial and may be delayed; the field `source` says so."""
    try:
        p = core.price_history(symbol, range)
        out = {k: v for k, v in p.items() if k != "bars"} | {"stats": core.price_stats(p["bars"]), "bar_count": len(p["bars"])}
        if include_bars:
            out["bars"] = p["bars"]
        return out
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_dcf(fcf_base: float, growth_rates: list[float], terminal_growth: float, discount_rate: float, net_debt: float = 0.0,
           shares_outstanding: float | None = None, sensitivity: bool = True) -> dict:
    """Deterministic discounted cash flow. growth_rates is a per-year list as decimals (0.08 = 8%). Returns flows, terminal value share of EV,
    and a discount-rate by terminal-growth sensitivity grid. All inputs are yours; nothing is fetched. Label outputs as assumptions, not forecasts."""
    try:
        base = core.dcf(fcf_base, growth_rates, terminal_growth, discount_rate, net_debt, shares_outstanding)
        if sensitivity:
            grid = []
            for dr in (discount_rate - 0.01, discount_rate, discount_rate + 0.01):
                row = {"discount_rate": round(dr, 4)}
                for tg in (terminal_growth - 0.005, terminal_growth, terminal_growth + 0.005):
                    try:
                        r = core.dcf(fcf_base, growth_rates, tg, dr, net_debt, shares_outstanding)
                        row[f"tg_{tg:.4f}"] = r.get("value_per_share", r["equity_value"])
                    except ValueError:
                        row[f"tg_{tg:.4f}"] = None
                grid.append(row)
            base["sensitivity"] = {"metric": "value_per_share" if shares_outstanding else "equity_value", "grid": grid}
        return base | {"inputs": {"fcf_base": fcf_base, "growth_rates": growth_rates, "terminal_growth": terminal_growth,
                                  "discount_rate": discount_rate, "net_debt": net_debt, "shares_outstanding": shares_outstanding},
                       "warning": "Output is an assumption model. Terminal value dominates; see terminal_share_of_ev_pct."}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_signals(category: str | None = None, kind: str | None = None, direction: str | None = None, ticker: str | None = None,
               min_strength: float = 50, since: str | None = None, limit: int = 50, include_backfill: bool = False,
               only_graded: Literal["any", "not_anti_predictive", "proven_edge"] = "any") -> dict:
    """Query the scored signal feed (the collector writes thousands per day). Filters: category (insider, filing_event, ownership, price,
    prediction_market), kind, direction (bullish, bearish, neutral, risk), ticker, min_strength 0-100, since YYYY-MM-DD.
    Each row has headline, strength, direction, evidence_url, accession and evidence_grade (see fs_edge_report). only_graded filters by that grade.
    Strength formulas: fs_signal_catalog. Research leads, not advice."""
    from . import outcomes as O, store
    try:
        conn = store.connect(readonly=True)
        grades = O.status_map(conn)
        q, a = ["strength>=?"], [min_strength]
        for col, v in (("category", category), ("kind", kind), ("direction", direction), ("ticker", ticker.upper() if ticker else None)):
            if v:
                q.append(f"{col}=?"); a.append(v)
        if since:
            q.append("asof>=?"); a.append(since)
        if not include_backfill:
            q.append("backfill=0")
        rows = conn.execute(f"SELECT * FROM signals WHERE {' AND '.join(q)} ORDER BY asof DESC, strength DESC LIMIT ?", (*a, min(limit, 500) * (8 if only_graded != "any" else 1))).fetchall()
        import json as _j
        res = []
        for r in rows:
            g = (grades.get(r["kind"]) or {}).get("status", "ungraded")
            if only_graded == "proven_edge" and g != "proven_edge":
                continue
            if only_graded == "not_anti_predictive" and g == "anti_predictive":
                continue
            d = {k: (_j.loads(r[k]) if k == "detail" else r[k]) for k in r.keys() if k not in ("id", "dedupe_key", "backfill")}
            d["evidence_grade"] = g
            res.append(d)
        res = res[:min(limit, 500)]
        return {"count": len(res), "signals": res, "disclaimer": DISCLAIMER}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_signal_digest(days: int = 1, top: int = 15) -> dict:
    """Daily digest: signal counts by kind/category and the top signals per direction for the last N days. Start here each morning."""
    from . import store
    try:
        conn = store.connect(readonly=True)
        from datetime import date, timedelta
        since = (date.today() - timedelta(days=days)).isoformat()
        counts = {r["kind"]: r["n"] for r in conn.execute("SELECT kind,count(*) n FROM signals WHERE asof>=? AND backfill=0 GROUP BY kind ORDER BY n DESC", (since,))}
        tops = {}
        for d in ("bullish", "bearish", "risk"):
            tops[d] = [dict(r) for r in conn.execute("SELECT ticker,kind,strength,headline,evidence_url FROM signals WHERE asof>=? AND direction=? AND backfill=0 ORDER BY strength DESC LIMIT ?", (since, d, top))]
        multi = [dict(r) for r in conn.execute(
            "SELECT ticker,count(DISTINCT kind) kinds,count(*) n,round(max(strength),1) best,group_concat(DISTINCT kind) k FROM signals "
            "WHERE asof>=? AND ticker IS NOT NULL AND backfill=0 AND strength>=40 GROUP BY ticker HAVING kinds>=3 ORDER BY kinds DESC, best DESC LIMIT ?", (since, top))]
        return {"since": since, "total": sum(counts.values()), "by_kind": counts, "top": tops, "confluence_3plus_kinds": multi, "disclaimer": DISCLAIMER}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_ticker_signals(ticker: str, days: int = 30) -> dict:
    """Everything the engine has seen on one ticker in the last N days, newest first, with net bullish-minus-bearish strength."""
    from . import store
    try:
        conn = store.connect(readonly=True)
        from datetime import date, timedelta
        since = (date.today() - timedelta(days=days)).isoformat()
        rows = [dict(r) for r in conn.execute("SELECT asof,kind,direction,strength,headline,evidence_url FROM signals WHERE ticker=? AND asof>=? ORDER BY asof DESC, strength DESC", (ticker.upper(), since))]
        net = sum(r["strength"] * (1 if r["direction"] == "bullish" else -1 if r["direction"] == "bearish" else 0) for r in rows)
        return {"ticker": ticker.upper(), "days": days, "count": len(rows), "net_directional_strength": round(net, 1), "signals": rows, "disclaimer": DISCLAIMER}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_signal_catalog() -> dict:
    """Every signal kind with its category, direction and exact strength formula, plus live row counts and collector run health."""
    from . import signals_mkt as M, signals_sec as S, store
    try:
        cat = {**S.CATALOG, **M.CATALOG_PRICE, **M.CATALOG_PM}
        conn = store.connect(readonly=True)
        counts = {r["kind"]: r["n"] for r in conn.execute("SELECT kind,count(*) n FROM signals GROUP BY kind")}
        runs = [dict(r) for r in conn.execute("SELECT module,started,finished,status,n_new,error FROM runs ORDER BY id DESC LIMIT 8")]
        return {"kinds": {k: v | {"rows": counts.get(k, 0)} for k, v in cat.items()}, "recent_runs": runs}
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _control_returns(entry_dates: set[str], horizon: int, n: int = 120, seed: int = 7) -> tuple[dict, int]:
    """Mean forward return of a seeded random sample of liquid tickers, for each entry date. The fair benchmark for small-cap signals."""
    import random
    uni = [r["ticker"] for r in core._tickers().values()]
    random.Random(seed).shuffle(uni)
    series = []
    for t in uni:
        if len(series) >= n:
            break
        try:
            bars = core.price_history(t, "5y")["bars"]
            if len(bars) < 300 or bars[-1]["close"] < 1 or sum(b["close"] * (b["volume"] or 0) for b in bars[-20:]) / 20 < 1_000_000:
                continue
            d = {b["date"]: (b["adj_close"] or b["close"]) for b in bars}
            series.append((d, sorted(d)))
        except Exception:  # noqa: BLE001
            continue
    out = {}
    for ed in entry_dates:
        rs = []
        for d, ds in series:
            i0 = next((i for i, x in enumerate(ds) if x >= ed), None)
            if i0 is not None and i0 + horizon < len(ds):
                rs.append(d[ds[i0 + horizon]] / d[ds[i0]] - 1)
        if len(rs) >= 30:
            out[ed] = sum(rs) / len(rs)
    return out, len(series)


@mcp.tool
def fs_signal_backtest(kind: str, horizon_days: int = 20, min_strength: float = 50, max_signals: int = 400) -> dict:
    """Honest forward-return check for one signal kind. Entry is the first close after the signal date. Reports hit rate, mean and median return,
    and excess versus a same-dates control (seeded random sample of ~120 liquid stocks), with a t-stat. Bearish signals score a falling price as a win.
    Only signals old enough for a full horizon count. Small or overlapping samples are flagged. A strength floor that excludes every signal returns n=0."""
    import math
    import statistics
    from datetime import date, timedelta

    from . import store
    try:
        conn = store.connect(readonly=True)
        cutoff = (date.today() - timedelta(days=int(horizon_days * 1.6) + 2)).isoformat()
        sigs = conn.execute("SELECT ticker,asof,direction FROM signals WHERE kind=? AND strength>=? AND asof<=? AND ticker IS NOT NULL ORDER BY asof DESC LIMIT ?",
                            (kind, min_strength, cutoff, max_signals)).fetchall()
        if not sigs:
            total = conn.execute("SELECT count(*), max(strength) FROM signals WHERE kind=?", (kind,)).fetchone()
            return {"kind": kind, "n": 0, "rows_of_kind": total[0], "max_strength_of_kind": total[1],
                    "note": "no qualifying signals old enough; lower min_strength or wait for more history (sec-edgar-collect sec --days N)"}
        cache: dict[str, dict] = {}
        trades = []  # (ticker, entry_date, signed_return)
        for s in sigs:
            try:
                bars = cache.get(s["ticker"]) or {b["date"]: (b["adj_close"] or b["close"]) for b in core.price_history(s["ticker"], "5y")["bars"]}
                cache[s["ticker"]] = bars
                ds = sorted(bars)
                i0 = next((i for i, d in enumerate(ds) if d > s["asof"]), None)
                if i0 is None or i0 + horizon_days >= len(ds):
                    continue
                r = bars[ds[i0 + horizon_days]] / bars[ds[i0]] - 1
                trades.append((s["ticker"], ds[i0], (-1 if s["direction"] == "bearish" else 1) * r, -1 if s["direction"] == "bearish" else 1, r))
            except Exception:  # noqa: BLE001
                continue
        if not trades:
            return {"kind": kind, "n": 0, "note": "price history unavailable for these signals"}
        ctrl, n_ctrl = _control_returns({t[1] for t in trades}, horizon_days)
        rets = sorted(t[2] for t in trades)
        n = len(rets)
        excess = [t[3] * (t[4] - ctrl[t[1]]) for t in trades if t[1] in ctrl]
        res = {"kind": kind, "horizon_days": horizon_days, "n": n, "distinct_tickers": len({t[0] for t in trades}),
               "hit_rate_pct": round(sum(r > 0 for r in rets) / n * 100, 1), "mean_return_pct": round(sum(rets) / n * 100, 2),
               "median_return_pct": round(rets[n // 2] * 100, 2)}
        if len(excess) >= 10:
            sd = statistics.stdev(excess)
            res |= {"control_tickers": n_ctrl, "n_with_control": len(excess), "mean_excess_vs_control_pct": round(sum(excess) / len(excess) * 100, 2),
                    "share_beating_control_pct": round(sum(e > 0 for e in excess) / len(excess) * 100, 1),
                    "excess_t_stat": round(sum(excess) / len(excess) / (sd / math.sqrt(len(excess))), 2) if sd > 0 else None}
        res["caveats"] = ["entry at first close after the signal date, no fees or slippage", "survivorship bias: universe is today's SEC ticker list",
                          "signals cluster in time and on the same tickers, so the t-stat overstates independence; treat |t| < 3 as no evidence",
                          f"history covers only {cutoff[:7]} back to the first collected day, not a full cycle", "n < 30 is anecdote" if n < 30 else "n >= 30 is still not proof"]
        return res
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_edge_report(horizon_days: int = 5) -> dict:
    """Which signal kinds have measurable predictive power. Each kind is graded against an equal-weight universe control using a date-clustered t-stat:
    proven_edge (t>=3 over >=20 entry dates), promising (t>=2), unproven, anti_predictive (t<=-3), thin (<20 dates). Read this before trusting any strength score.
    Horizons stored: 1, 5, 10, 20 trading days. Direction-signed: a bearish kind wins when the stock underperforms."""
    from . import store
    try:
        conn = store.connect(readonly=True)
        rows = [dict(r) for r in conn.execute("SELECT * FROM edge WHERE h=? ORDER BY t_date DESC", (horizon_days,))]
        if not rows:
            return {"note": "no grades yet: run `sec-edgar-collect outcomes` after prices and signals exist"}
        for r in rows:
            r["mean_excess_pct"] = round(r.pop("mean_excess") * 100, 2)
            r["share_positive_pct"] = round(r.pop("share_positive") * 100, 1)
            r["t_date"] = None if r["t_date"] is None else round(r["t_date"], 2)
        counts: dict = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return {"horizon_days": horizon_days, "status_counts": counts, "kinds": rows,
                "how_to_read": "Strength scores are heuristics. Only proven_edge kinds have evidence of forward predictive power, and only over the stated window.",
                "disclaimer": DISCLAIMER}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_explore_signal(kind: str, horizon_days: int = 5) -> dict:
    """Split-sample search for which slices of one signal kind work (size, price, holdings share, volume). Fits on the earlier half of entry dates and
    verifies on the later half; 'replicates' needs the same sign in both halves and out-of-sample t>=2. Reports the expected number of false positives.
    Output is a lead to retest on new data, never an edge by itself."""
    from . import outcomes as O, store
    try:
        return O.explore(store.connect(readonly=True), kind, horizon_days)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
def fs_ticker_brief(company: str, days: int = 30) -> dict:
    """One call per name: fundamentals snapshot, the last N days of signals with each kind's evidence grade, recent insider net buying, and price stats.
    Missing pieces come back null, never zero. The evidence_grade on each signal says whether that signal kind has shown predictive power."""
    from . import outcomes as O, store
    from datetime import date, timedelta
    try:
        c = core.must_resolve(company)
        out: dict = {"company": c}
        snap = fs_company_snapshot(c["cik"])
        out["fundamentals"] = None if "error" in snap else {k: snap[k] for k in ("fiscal_period_end", "levels_usd", "ratios", "filing")}
        try:
            p = core.price_history(c["ticker"], "1y")
            out["price"] = {"last": p["last_price"], "as_of": p["last_price_time_utc"], "stats_1y": core.price_stats(p["bars"])}
        except Exception as e:  # noqa: BLE001
            out["price"] = {"error": str(e)}
        conn = store.connect(readonly=True)
        grades = O.status_map(conn)
        since = (date.today() - timedelta(days=days)).isoformat()
        rows = [dict(r) for r in conn.execute("SELECT asof,kind,direction,strength,headline,evidence_url FROM signals WHERE ticker=? AND asof>=? AND backfill=0 ORDER BY asof DESC, strength DESC", (c["ticker"], since))]
        for r in rows:
            g = grades.get(r["kind"])
            r["evidence_grade"] = g["status"] if g else "ungraded"
        out["signals"] = rows
        buys = sum(r["strength"] for r in rows if r["direction"] == "bullish")
        sells = sum(r["strength"] for r in rows if r["direction"] == "bearish")
        out["signal_balance"] = {"bullish_strength_sum": round(buys, 1), "bearish_strength_sum": round(sells, 1), "count": len(rows),
                                 "proven_only_count": sum(1 for r in rows if r["evidence_grade"] == "proven_edge")}
        out["disclaimer"] = DISCLAIMER
        return out
    except Exception as e:  # noqa: BLE001
        return _err(e)


def main() -> None:
    try:
        core.user_agent()
    except RuntimeError as e:
        print(f"warning: {e} Tools that fetch from the SEC will return errors until it is set.", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
