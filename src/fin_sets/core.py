"""sec-edgar-mcp core: HTTP + cache + SEC EDGAR normalization. No MCP imports here, so it tests offline.

Rules:
  * Every datum carries provenance: source, accession, form, filed date, XBRL tag, URL.
  * Deterministic math only. Missing data is None, never zero.
  * Restatement aware: for a period, the latest filed value wins and a `restated` flag notes drift.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

UA_ENV = "SEC_EDGAR_USER_AGENT"
CACHE_DIR = Path(os.environ.get("SEC_EDGAR_CACHE", str(Path.home() / ".cache" / "sec-edgar-mcp")))
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def user_agent() -> str:
    """User-Agent for outbound requests. SEC fair-access rules require a contact address, so there is no default."""
    ua = os.environ.get(UA_ENV, "").strip()
    if not ua:
        raise RuntimeError(
            f"{UA_ENV} is not set. The SEC rejects requests without a contact User-Agent. "
            f'Set it to your app name and email, for example {UA_ENV}="my-app you@example.com".'
        )
    return ua


_lock = threading.Lock()
_limiters: dict[str, list] = {}  # host key -> [lock, last_call]
GAPS = {"sec": 0.115, "yahoo": 0.04, "other": 0.02}  # SEC fair-access cap is 10 req/s; stay near 8.


def _host_key(url: str) -> str:
    if "sec.gov" in url:
        return "sec"
    if "yahoo.com" in url:
        return "yahoo"
    return "other"


def _throttle(key: str) -> None:
    with _lock:
        lim = _limiters.setdefault(key, [threading.Lock(), 0.0])
    with lim[0]:
        wait = GAPS[key] - (time.time() - lim[1])
        if wait > 0:
            time.sleep(wait)
        lim[1] = time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache_path(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")


def fetch(url: str, ttl: int = 3600, as_text: bool = False, headers: dict | None = None, cache: bool = True,
          retries: int = 3) -> dict:
    """GET with optional disk cache, per-host rate limit and 429/5xx backoff. Serves a stale cache entry (flagged) if the network fails.
    cache=False skips the disk cache entirely (bulk filing sweeps)."""
    p = _cache_path(url)
    cached = None
    if cache and p.exists():
        try:
            cached = json.loads(p.read_text())
        except Exception:
            cached = None
    if cached and time.time() - cached["ts"] < ttl:
        return {"body": cached["body"], "fetched_at": cached["fetched_at"], "cached": True, "stale": False, "url": url}
    hdrs = {"Accept-Encoding": "gzip, deflate"}
    if not (headers and "User-Agent" in headers):
        hdrs["User-Agent"] = user_agent()
    if headers:
        hdrs.update(headers)
    key = _host_key(url)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            _throttle(key)
            r = httpx.get(url, headers=hdrs, timeout=45, follow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                last = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            body = r.text if as_text else r.json()
            rec = {"ts": time.time(), "fetched_at": _now_iso(), "body": body}
            if cache:
                p.write_text(json.dumps(rec))
            return {"body": body, "fetched_at": rec["fetched_at"], "cached": False, "stale": False, "url": url}
        except httpx.HTTPStatusError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.8 * (attempt + 1))
    if cached:
        return {"body": cached["body"], "fetched_at": cached["fetched_at"], "cached": True, "stale": True,
                "url": url, "error": f"{type(last).__name__}: {last}"}
    raise last if last else RuntimeError("fetch failed")


# ---------------------------------------------------------------- company resolution

def _tickers() -> dict:
    return fetch("https://www.sec.gov/files/company_tickers.json", ttl=86400)["body"]


def resolve(query: str) -> dict | None:
    """Ticker, CIK (digits) or name fragment -> {cik, ticker, name}. None if no match."""
    q = query.strip()
    rows = list(_tickers().values())
    if q.isdigit():
        n = int(q)
        for r in rows:
            if r["cik_str"] == n:
                return _row(r)
        return {"cik": f"{n:010d}", "ticker": None, "name": None}
    qu = q.upper().replace(".", "-")
    for r in rows:
        if r["ticker"].upper() == qu:
            return _row(r)
    ql = q.lower()
    hits = [r for r in rows if ql in r["title"].lower()]
    hits.sort(key=lambda r: (not r["title"].lower().startswith(ql), len(r["title"])))
    return _row(hits[0]) if hits else None


def search_companies(query: str, limit: int = 10) -> list[dict]:
    ql = query.lower()
    rows = [r for r in _tickers().values() if ql in r["title"].lower() or ql == r["ticker"].lower()]
    rows.sort(key=lambda r: (not r["title"].lower().startswith(ql), len(r["title"])))
    return [_row(r) for r in rows[:limit]]


def _row(r: dict) -> dict:
    return {"cik": f"{r['cik_str']:010d}", "ticker": r["ticker"], "name": r["title"]}


def must_resolve(q: str) -> dict:
    c = resolve(q)
    if not c:
        raise ValueError(f"No SEC-registered company matches {q!r}. Try a ticker, CIK or name fragment.")
    return c


# ---------------------------------------------------------------- filings

def submissions(cik: str) -> dict:
    return fetch(f"https://data.sec.gov/submissions/CIK{cik}.json", ttl=1800)


def filings(cik: str, forms: list[str] | None = None, limit: int = 20, since: str | None = None) -> dict:
    sub = submissions(cik)
    rec = sub["body"]["filings"]["recent"]
    out = []
    for i in range(len(rec["accessionNumber"])):
        form = rec["form"][i]
        if forms and form not in forms:
            continue
        fd = rec["filingDate"][i]
        if since and fd < since:
            continue
        acc = rec["accessionNumber"][i]
        doc = rec["primaryDocument"][i]
        out.append({
            "form": form, "filed": fd, "period": rec["reportDate"][i] or None, "accession": acc,
            "description": rec["primaryDocDescription"][i] or None,
            "url": doc_url(cik, acc, doc),
            "index_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/",
        })
        if len(out) >= limit:
            break
    return {"company": sub["body"]["name"], "cik": cik, "filings": out,
            "source": "SEC EDGAR submissions API", "fetched_at": sub["fetched_at"], "cached": sub["cached"]}


def doc_url(cik: str, acc: str, doc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html.unescape(raw).replace("\xa0", " ")
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n\n", raw)
    return raw.strip()


_ITEM_RE = {
    "10-K": ["1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8", "9", "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16"],
    "10-Q": ["1", "2", "3", "4"],
}


def filing_text(url: str) -> dict:
    r = fetch(url, ttl=30 * 86400, as_text=True)
    return {"text": html_to_text(r["body"]), "fetched_at": r["fetched_at"], "cached": r["cached"]}


def extract_item(text: str, item: str) -> tuple[str, int] | None:
    """Return (section text, start offset) for `Item <item>.` picking the longest candidate
    (the table of contents entry is short, the body section is long)."""
    item = item.upper().replace("ITEM", "").strip(" .")
    pat = re.compile(rf"(?im)^\s*item\s*{re.escape(item)}\s*[\.\:\-\u2014\u2013]?\s+\S")
    starts = [m.start() for m in pat.finditer(text)]
    if not starts:
        return None
    nxt = re.compile(r"(?im)^\s*item\s*\d{1,2}[A-C]?\s*[\.\:\-\u2014\u2013]?\s+\S")
    best = None
    for s in starts:
        m = nxt.search(text, s + 12)
        # skip the same item header matching itself
        while m and re.match(rf"(?im)^\s*item\s*{re.escape(item)}\b", text[m.start():m.start() + 20]):
            m = nxt.search(text, m.end())
        e = m.start() if m else len(text)
        if best is None or (e - s) > (best[1] - best[0]):
            best = (s, e)
    return text[best[0]:best[1]].strip(), best[0]


def search_text(text: str, query: str, width: int = 400, limit: int = 8) -> list[dict]:
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        return []
    pat = re.compile("|".join(re.escape(t) for t in terms), re.I)
    # rank paragraphs by distinct term coverage, then hit count
    paras = [(m.start(), m.group(0)) for m in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", text)]
    scored = []
    for off, p in paras:
        hits = pat.findall(p)
        if not hits:
            continue
        distinct = len({h.lower() for h in hits})
        scored.append((distinct, len(hits), off, p))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    out = []
    for d, n, off, p in scored[:limit]:
        out.append({"offset": off, "terms_matched": d, "hits": n, "passage": p[:width * 3]})
    return out


# ---------------------------------------------------------------- XBRL facts

# metric -> (kind, [(taxonomy, tag, unit)...] in preference order)
METRICS: dict[str, tuple[str, list[tuple[str, str, str]]]] = {
    "revenue": ("duration", [("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"), ("us-gaap", "Revenues", "USD"),
                             ("us-gaap", "SalesRevenueNet", "USD"), ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax", "USD")]),
    "cost_of_revenue": ("duration", [("us-gaap", "CostOfRevenue", "USD"), ("us-gaap", "CostOfGoodsAndServicesSold", "USD")]),
    "gross_profit": ("duration", [("us-gaap", "GrossProfit", "USD")]),
    "rd_expense": ("duration", [("us-gaap", "ResearchAndDevelopmentExpense", "USD")]),
    "sga_expense": ("duration", [("us-gaap", "SellingGeneralAndAdministrativeExpense", "USD")]),
    "operating_income": ("duration", [("us-gaap", "OperatingIncomeLoss", "USD")]),
    "interest_expense": ("duration", [("us-gaap", "InterestExpense", "USD"), ("us-gaap", "InterestExpenseNonoperating", "USD")]),
    "pretax_income": ("duration", [("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", "USD"),
                                   ("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments", "USD")]),
    "income_tax": ("duration", [("us-gaap", "IncomeTaxExpenseBenefit", "USD")]),
    "net_income": ("duration", [("us-gaap", "NetIncomeLoss", "USD"), ("us-gaap", "ProfitLoss", "USD")]),
    "eps_diluted": ("duration", [("us-gaap", "EarningsPerShareDiluted", "USD/shares")]),
    "shares_diluted": ("duration", [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares")]),
    "depreciation_amortization": ("duration", [("us-gaap", "DepreciationDepletionAndAmortization", "USD"), ("us-gaap", "DepreciationAndAmortization", "USD"),
                                               ("us-gaap", "DepreciationAmortizationAndAccretionNet", "USD")]),
    "stock_comp": ("duration", [("us-gaap", "ShareBasedCompensation", "USD")]),
    "operating_cash_flow": ("duration", [("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD")]),
    "capex": ("duration", [("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", "USD"), ("us-gaap", "PaymentsToAcquireProductiveAssets", "USD")]),
    "buybacks": ("duration", [("us-gaap", "PaymentsForRepurchaseOfCommonStock", "USD")]),
    "dividends_paid": ("duration", [("us-gaap", "PaymentsOfDividends", "USD"), ("us-gaap", "PaymentsOfDividendsCommonStock", "USD")]),
    "cash": ("instant", [("us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD"),
                         ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "USD")]),
    "current_assets": ("instant", [("us-gaap", "AssetsCurrent", "USD")]),
    "total_assets": ("instant", [("us-gaap", "Assets", "USD")]),
    "current_liabilities": ("instant", [("us-gaap", "LiabilitiesCurrent", "USD")]),
    "total_liabilities": ("instant", [("us-gaap", "Liabilities", "USD")]),
    "long_term_debt": ("instant", [("us-gaap", "LongTermDebtNoncurrent", "USD"), ("us-gaap", "LongTermDebt", "USD")]),
    "short_term_debt": ("instant", [("us-gaap", "DebtCurrent", "USD"), ("us-gaap", "LongTermDebtCurrent", "USD"), ("us-gaap", "ShortTermBorrowings", "USD")]),
    "equity": ("instant", [("us-gaap", "StockholdersEquity", "USD"),
                           ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD")]),
    "shares_outstanding": ("instant", [("dei", "EntityCommonStockSharesOutstanding", "shares"), ("us-gaap", "CommonStockSharesOutstanding", "shares")]),
}

FLOW_ALIASES = {"sales": "revenue", "ocf": "operating_cash_flow", "eps": "eps_diluted", "debt": "long_term_debt"}


def company_facts(cik: str) -> dict:
    return fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", ttl=12 * 3600)


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _series(facts: dict, metric: str, frequency: str, cik: str) -> list[dict]:
    """Return period rows for one metric, newest first. frequency: annual | quarterly."""
    metric = FLOW_ALIASES.get(metric, metric)
    if metric not in METRICS:
        raise ValueError(f"Unknown metric {metric!r}. Known: {', '.join(sorted(METRICS))}")
    kind, candidates = METRICS[metric]
    best = None  # prefer the tag with the freshest period; ties keep declared preference order
    for tax, tag, unit in candidates:
        entries = facts.get("facts", {}).get(tax, {}).get(tag, {}).get("units", {}).get(unit)
        if not entries:
            continue
        rows = _collapse(entries, kind, frequency)
        if rows and (best is None or rows[0]["period_end"] > best[0][0]["period_end"]):
            best = (rows, f"{tax}:{tag}", unit)
    if best:
        rows, tagname, unit = best
        for r in rows:
            r["tag"] = tagname
            r["unit"] = unit
            r["url"] = _acc_url(cik, r["accession"])
        return rows
    return []


def _acc_url(cik: str, acc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"


def _collapse(entries: list[dict], kind: str, frequency: str) -> list[dict]:
    by_end: dict[str, list[dict]] = {}
    for e in entries:
        end = e.get("end")
        if not end:
            continue
        if kind == "duration":
            st = e.get("start")
            if not st:
                continue
            d = _days(st, end)
            if frequency == "annual" and not (340 <= d <= 380):
                continue
            if frequency == "quarterly" and not (75 <= d <= 105):
                continue
            if frequency == "ytd9" and not (255 <= d <= 285):
                continue
        elif frequency == "annual" and not (e.get("fp") == "FY" or str(e.get("form", "")).startswith(("10-K", "20-F", "40-F"))):
            continue  # balance sheet: annual means fiscal year end, not the latest interim date
        by_end.setdefault(end, []).append(e)
    rows = []
    for end, lst in by_end.items():
        lst.sort(key=lambda x: (x.get("filed", ""), x.get("accn", "")))
        latest = lst[-1]
        first = lst[0]
        rows.append({
            "period_end": end, "value": latest["val"], "form": latest.get("form"), "fp": latest.get("fp"),
            "fy": latest.get("fy"), "filed": latest.get("filed"), "accession": latest.get("accn"),
            "restated": first["val"] != latest["val"], "first_reported_value": first["val"] if first["val"] != latest["val"] else None,
            "derived": False,
        })
    rows.sort(key=lambda r: r["period_end"], reverse=True)
    return rows


def series(cik: str, metric: str, frequency: str = "annual", periods: int = 8) -> dict:
    """Public series with Q4 derivation for quarterly flow metrics (FY - 9M YTD)."""
    facts_r = company_facts(cik)
    facts = facts_r["body"]
    m = FLOW_ALIASES.get(metric, metric)
    kind = METRICS[m][0] if m in METRICS else None
    rows = _series(facts, m, frequency, cik)
    if frequency == "quarterly" and kind == "duration":
        rows = _add_q4(facts, m, rows, cik)
    return {"metric": m, "frequency": frequency, "rows": rows[:periods], "entity": facts.get("entityName"),
            "source": "SEC EDGAR XBRL companyfacts", "fetched_at": facts_r["fetched_at"], "cached": facts_r["cached"],
            "stale": facts_r.get("stale", False)}


def _add_q4(facts: dict, metric: str, qrows: list[dict], cik: str) -> list[dict]:
    if metric in ("eps_diluted", "shares_diluted"):
        return qrows  # not additive
    annual = _series(facts, metric, "annual", cik)
    ytd9 = _series(facts, metric, "ytd9", cik)
    have = {r["period_end"] for r in qrows}
    y9 = {}
    for r in ytd9:
        y9.setdefault(r["fy"], r)
    out = list(qrows)
    for a in annual:
        if a["period_end"] in have:
            continue
        # match the 9M YTD whose end is ~ 91 days before FY end
        match = None
        for r in ytd9:
            gap = _days(r["period_end"], a["period_end"])
            if 80 <= gap <= 105:
                match = r
                break
        if match:
            out.append({
                "period_end": a["period_end"], "value": a["value"] - match["value"], "form": "derived", "fp": "Q4", "fy": a["fy"],
                "filed": a["filed"], "accession": a["accession"], "restated": False, "first_reported_value": None,
                "derived": True, "derivation": f"FY {a['value']} - 9M YTD {match['value']} (accessions {a['accession']}, {match['accession']})",
                "tag": a.get("tag"), "unit": a.get("unit"), "url": a.get("url"),
            })
    out.sort(key=lambda r: r["period_end"], reverse=True)
    return out


# ---------------------------------------------------------------- math helpers

def safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0):
        return None
    return a / b


def pct(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x * 100, nd)


def cagr(first: float | None, last: float | None, years: float) -> float | None:
    if first is None or last is None or first <= 0 or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def dcf(fcf0: float, growth: list[float], terminal_growth: float, discount: float, net_debt: float = 0.0,
        shares: float | None = None) -> dict:
    if discount <= terminal_growth:
        raise ValueError("discount rate must exceed terminal growth")
    pv, flows, f = 0.0, [], fcf0
    for i, g in enumerate(growth, 1):
        f = f * (1 + g)
        d = f / (1 + discount) ** i
        pv += d
        flows.append({"year": i, "fcf": round(f, 2), "pv": round(d, 2)})
    tv = f * (1 + terminal_growth) / (discount - terminal_growth)
    pv_tv = tv / (1 + discount) ** len(growth)
    ev = pv + pv_tv
    eq = ev - net_debt
    out = {"flows": flows, "pv_explicit": round(pv, 2), "terminal_value": round(tv, 2), "pv_terminal": round(pv_tv, 2),
           "enterprise_value": round(ev, 2), "equity_value": round(eq, 2),
           "terminal_share_of_ev_pct": round(pv_tv / ev * 100, 1) if ev else None}
    if shares:
        out["value_per_share"] = round(eq / shares, 4)
    return out


# ---------------------------------------------------------------- Form 4

def parse_form4(xml_text: str) -> list[dict]:
    from defusedxml import ElementTree as ET  # XXE-safe parser for untrusted filing XML
    root = ET.fromstring(xml_text)

    def t(node, path):
        n = node.find(path)
        return n.text.strip() if n is not None and n.text else None

    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        roles = []
        if rel is not None:
            if t(rel, "isDirector") in ("1", "true"):
                roles.append("director")
            if t(rel, "isOfficer") in ("1", "true"):
                roles.append(t(rel, "officerTitle") or "officer")
            if t(rel, "isTenPercentOwner") in ("1", "true"):
                roles.append("10% owner")
        owners.append({"name": t(ro, "reportingOwnerId/rptOwnerName"), "roles": roles})
    rows = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        shares = t(tx, "transactionAmounts/transactionShares/value")
        price = t(tx, "transactionAmounts/transactionPricePerShare/value")
        rows.append({
            "owner": owners[0]["name"] if owners else None, "roles": owners[0]["roles"] if owners else [],
            "security": t(tx, "securityTitle/value"), "date": t(tx, "transactionDate/value"),
            "code": t(tx, "transactionCoding/transactionCode"),
            "acquired_disposed": t(tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
            "shares": float(shares) if shares else None, "price": float(price) if price else None,
            "value_usd": round(float(shares) * float(price), 2) if shares and price else None,
            "owned_after": t(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
        })
    return rows


FORM4_CODES = {"P": "open-market purchase", "S": "open-market sale", "A": "grant/award", "M": "option exercise",
               "F": "tax withholding", "G": "gift", "D": "disposition to issuer", "C": "conversion", "X": "option exercise (in-the-money)"}


# ---------------------------------------------------------------- prices (Yahoo chart, unofficial)

def price_history(symbol: str, rng: str = "1y", interval: str = "1d") -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}?range={rng}&interval={interval}"
    r = fetch(url, ttl=300, headers={"User-Agent": "Mozilla/5.0"})
    res = r["body"]["chart"]["result"]
    if not res:
        raise ValueError(f"No price data for {symbol}")
    res = res[0]
    meta = res["meta"]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    bars = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        bars.append({"date": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), "open": q["open"][i], "high": q["high"][i],
                     "low": q["low"][i], "close": c, "adj_close": adj[i] if adj else None, "volume": q["volume"][i]})
    return {"symbol": meta.get("symbol"), "currency": meta.get("currency"), "exchange": meta.get("fullExchangeName"),
            "last_price": meta.get("regularMarketPrice"),
            "last_price_time_utc": datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc).isoformat(timespec="seconds") if meta.get("regularMarketTime") else None,
            "bars": bars, "source": "Yahoo Finance chart endpoint (unofficial, may be delayed, not exchange-verified)",
            "fetched_at": r["fetched_at"], "cached": r["cached"]}


def price_stats(bars: list[dict]) -> dict:
    closes = [b["adj_close"] or b["close"] for b in bars if (b["adj_close"] or b["close"]) is not None]
    if len(closes) < 3:
        return {}
    import math
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1)
    return {"bars": len(closes), "total_return_pct": pct(closes[-1] / closes[0] - 1),
            "annualized_vol_pct": pct(math.sqrt(var) * math.sqrt(252)), "max_drawdown_pct": pct(mdd),
            "high": max(closes), "low": min(closes), "basis": "adjusted close where provided"}
