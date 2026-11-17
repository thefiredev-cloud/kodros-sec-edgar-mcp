"""SEC-driven signal detectors. Pure parse functions (testable offline) + collectors that sweep the EDGAR daily index.

Every signal is computed from the filing itself. Nothing here uses an LLM. Strength formulas are in CATALOG and surfaced by fs_signal_catalog.
"""

from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from defusedxml import ElementTree as ET  # XXE-safe parser for untrusted filing XML

from . import core, store

CATALOG: dict[str, dict] = {
    "insider_buy": {"category": "insider", "direction": "bullish", "formula": "30 + 12*log10(value/10k), +15 CEO/CFO/President, +8 other officer, +6 director, +10 if >=10% of post-trade holdings, -15 if 10b5-1; min value $10k; cap 100"},
    "insider_sell": {"category": "insider", "direction": "bearish", "formula": "15 + 8*log10(value/250k), +10 if >=25% of post-trade holdings and not 10b5-1, x0.5 if 10b5-1; min value $250k; cap 80 (sales are weak evidence)"},
    "insider_cluster_buy": {"category": "insider", "direction": "bullish", "formula": ">=3 distinct insiders with open-market buys totalling >=$100k in 30d at one issuer: 70 + 5*(extra insiders) + 5 if total >=$1M; cap 100"},
    "insider_planned_sale": {"category": "insider", "direction": "bearish", "formula": "Form 144 proposed sale: 10 + 8*log10(value/1M); min value $1M; cap 50"},
    "event_8k": {"category": "filing_event", "direction": "varies", "formula": "fixed severity per 8-K item: 1.03 bankruptcy 95 | 4.02 non-reliance 90 | 3.01 delisting notice 80 | 2.04 acceleration 70 | 4.01 auditor change 60 | 5.01 control change 60 | 2.06 impairment 55 | 5.02 officer/director change 45 | 2.05 exit costs 40 | 2.02 earnings 40 | 1.01/2.01/3.02 35"},
    "late_filing": {"category": "filing_event", "direction": "risk", "formula": "NT 10-K / NT 10-Q late-filing notice: 60"},
    "activist_13d": {"category": "ownership", "direction": "bullish", "formula": "Schedule 13D (intent to influence): 65; 13D/A amendment 40"},
    "passive_13g": {"category": "ownership", "direction": "neutral", "formula": "Schedule 13G passive >5% stake: 25"},
    "tender_offer": {"category": "ownership", "direction": "bullish", "formula": "SC TO-T third-party tender offer for the issuer: 75"},
    "equity_offering": {"category": "filing_event", "direction": "bearish", "formula": "424B4/424B5 prospectus for a listed issuer (dilution): 35"},
    "exchange_delisting": {"category": "filing_event", "direction": "risk", "formula": "Form 25-NSE exchange delisting notice: 70"},
}

ITEM_MAP = [  # (description prefix lowercased, code, severity, direction)
    ("bankruptcy", "1.03", 95, "bearish"), ("non-reliance", "4.02", 90, "risk"), ("notice of delisting", "3.01", 80, "risk"),
    ("triggering events", "2.04", 70, "bearish"), ("changes in registrant's certifying", "4.01", 60, "risk"),
    ("changes in control", "5.01", 60, "neutral"), ("material impairments", "2.06", 55, "bearish"),
    ("departure of directors", "5.02", 45, "risk"), ("costs associated with exit", "2.05", 40, "bearish"),
    ("results of operations", "2.02", 40, "neutral"), ("entry into a material definitive", "1.01", 35, "neutral"),
    ("completion of acquisition", "2.01", 35, "neutral"), ("unregistered sales of equity", "3.02", 35, "bearish"),
    ("termination of a material definitive", "1.02", 35, "risk"),
]


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ------------------------------------------------------------------ daily index

def daily_index(d: date) -> list[dict]:
    q = (d.month - 1) // 3 + 1
    url = f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{q}/form.{d:%Y%m%d}.idx"
    try:
        body = core.fetch(url, ttl=600 if d >= date.today() - timedelta(days=1) else 7 * 86400, as_text=True, cache=False)["body"]
    except Exception:  # weekend, holiday or not yet published
        return []
    row = re.compile(r"^(?P<form>.+?)\s{2,}(?P<company>.+?)\s+(?P<cik>\d{1,10})\s+(?P<date>\d{8})\s+(?P<file>edgar/\S+)\s*$")
    out = []
    for l in body.splitlines():
        m = row.match(l)
        if not m:
            continue
        ds = m["date"]
        out.append({"form": m["form"].strip(), "company": m["company"].strip(), "cik": m["cik"].zfill(10),
                    "date": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}", "file": m["file"]})
    return out


def acc_from_file(file: str) -> str:
    return file.rsplit("/", 1)[-1].replace(".txt", "")


def folder_url(cik: str, acc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"


def ticker_map() -> dict[str, str]:
    m: dict[str, str] = {}
    for r in core._tickers().values():
        m.setdefault(f"{r['cik_str']:010d}", r["ticker"])
    return m


# ------------------------------------------------------------------ Form 4

def extract_xml(txt: str) -> str | None:
    m = re.search(r"<XML>\s*(<\?xml.*?)</XML>", txt, re.S | re.I)
    return m.group(1).strip() if m else None


def _t(node, path):
    n = node.find(path)
    return n.text.strip() if n is not None and n.text else None


def parse_form4_doc(xml: str) -> dict:
    root = ET.fromstring(xml)
    issuer = {"cik": (_t(root, "issuer/issuerCik") or "").zfill(10), "name": _t(root, "issuer/issuerName"),
              "ticker": (_t(root, "issuer/issuerTradingSymbol") or "").upper() or None}
    owner = {"name": None, "roles": [], "is_ceo_cfo": False}
    for ro in root.findall("reportingOwner"):
        owner["name"] = owner["name"] or _t(ro, "reportingOwnerId/rptOwnerName")
        rel = ro.find("reportingOwnerRelationship")
        if rel is not None:
            if _t(rel, "isDirector") in ("1", "true"):
                owner["roles"].append("director")
            if _t(rel, "isOfficer") in ("1", "true"):
                title = _t(rel, "officerTitle") or "officer"
                owner["roles"].append(title)
                if re.search(r"\b(chief executive|ceo|chief financial|cfo|president)\b", title, re.I):
                    owner["is_ceo_cfo"] = True
            if _t(rel, "isTenPercentOwner") in ("1", "true"):
                owner["roles"].append("10% owner")
    planned = (_t(root, "aff10b5One") or "") in ("1", "true")
    rows = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        sh, px = _t(tx, "transactionAmounts/transactionShares/value"), _t(tx, "transactionAmounts/transactionPricePerShare/value")
        try:
            sh_f = float(sh) if sh else None
            px_f = float(px) if px else None
        except ValueError:
            sh_f = px_f = None
        after = _t(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        rows.append({"date": _t(tx, "transactionDate/value"), "code": _t(tx, "transactionCoding/transactionCode"),
                     "shares": sh_f, "price": px_f, "after": float(after) if after else None,
                     "ad": _t(tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
                     "planned": planned or (_t(tx, "transactionCoding/equitySwapInvolved") is None and False)})
    return {"issuer": issuer, "owner": owner, "rows": rows, "planned": planned, "period": _t(root, "periodOfReport")}


def form4_signals(doc: dict, acc: str, filed: str, cik_hint: str, tmap: dict[str, str]) -> list[dict]:
    iss, own = doc["issuer"], doc["owner"]
    ticker = iss["ticker"] or tmap.get(iss["cik"])
    role = ", ".join(own["roles"]) or "insider"
    url = folder_url(iss["cik"] or cik_hint, acc)
    agg: dict[tuple, dict] = {}
    for r in doc["rows"]:
        if r["code"] not in ("P", "S") or not r["shares"] or not r["price"] or r["price"] <= 0:
            continue
        a = agg.setdefault((r["code"], r["date"]), {"sh": 0.0, "val": 0.0, "after": None, "planned": r["planned"]})
        a["sh"] += r["shares"]
        a["val"] += r["shares"] * r["price"]
        a["after"] = r["after"] if r["after"] is not None else a["after"]
    sigs = []
    for (code, tdate), a in agg.items():
        val, vwap = a["val"], a["val"] / a["sh"]
        after = a["after"]
        pct_hold = (a["sh"] / (after + a["sh"] if code == "S" else after)) * 100 if after and (after + a["sh"] if code == "S" else after) > 0 else None
        common = {"category": "insider", "ticker": ticker, "cik": iss["cik"], "entity": iss["name"], "accession": acc, "asof": filed,
                  "evidence_url": url, "value": round(val, 2)}
        det = {"owner": own["name"], "roles": own["roles"], "trade_date": tdate, "shares": a["sh"], "vwap": round(vwap, 4),
               "post_trade_shares": after, "pct_of_holdings": None if pct_hold is None else round(pct_hold, 2), "planned_10b5_1": a["planned"]}
        if code == "P" and val >= 10_000:
            s = 30 + 12 * math.log10(val / 10_000)
            s += 15 if own["is_ceo_cfo"] else (8 if any(x not in ("director", "10% owner") for x in own["roles"]) else (6 if "director" in own["roles"] else 0))
            s += 10 if pct_hold and pct_hold >= 10 else 0
            s -= 15 if a["planned"] else 0
            sigs.append({**common, "dedupe_key": f"insider_buy:{acc}:{own['name']}:{tdate}", "kind": "insider_buy", "direction": "bullish",
                         "strength": clamp(s), "headline": f"{own['name']} ({role}) bought ${val:,.0f} of {ticker or iss['name']} at ${vwap:,.2f}", "detail": det})
        elif code == "S" and val >= 250_000:
            s = 15 + 8 * math.log10(val / 250_000)
            s += 10 if pct_hold and pct_hold >= 25 and not a["planned"] else 0
            s = s * 0.5 if a["planned"] else s
            sigs.append({**common, "dedupe_key": f"insider_sell:{acc}:{own['name']}:{tdate}", "kind": "insider_sell", "direction": "bearish",
                         "strength": clamp(s, 0, 80), "headline": f"{own['name']} ({role}) sold ${val:,.0f} of {ticker or iss['name']} at ${vwap:,.2f}"
                         + (" (10b5-1 plan)" if a["planned"] else ""), "detail": det})
    return sigs


def parse_form144(txt: str) -> dict | None:
    xml = extract_xml(txt)
    if not xml:
        return None
    xml = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", xml)  # drop namespaces so plain paths match
    xml = re.sub(r"<(/?)\w+:", r"<\1", xml)
    root = ET.fromstring(xml)
    issuer = _t(root, ".//issuerInfo/issuerName") or _t(root, ".//issuerName")
    cik = _t(root, ".//issuerInfo/issuerCik") or _t(root, ".//issuerCik")
    val = None
    for p in ("securitiesInformation/aggregateMarketValue", ".//aggregateMarketValue"):
        v = _t(root, p)
        if v:
            try:
                val = float(v.replace(",", ""))
            except ValueError:
                pass
            break
    shares = _t(root, ".//noOfUnitsSold") or _t(root, ".//securitiesToBeSold/amountOfSecuritiesToBeSold")
    person = _t(root, ".//filerInfo/filer/filerCredentials/cik") and _t(root, ".//nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold")
    return {"issuer": issuer, "cik": (cik or "").zfill(10) if cik else None, "value": val, "shares": shares, "person": person}


def form144_signals(doc: dict, acc: str, filed: str, tmap: dict[str, str]) -> list[dict]:
    if not doc or not doc["value"] or doc["value"] < 1_000_000 or not doc["cik"]:
        return []
    s = clamp(10 + 8 * math.log10(doc["value"] / 1_000_000), 0, 50)
    t = tmap.get(doc["cik"])
    return [{"dedupe_key": f"insider_planned_sale:{acc}", "asof": filed, "category": "insider", "kind": "insider_planned_sale", "ticker": t, "cik": doc["cik"],
             "entity": doc["issuer"], "direction": "bearish", "strength": s, "value": doc["value"], "accession": acc,
             "headline": f"Form 144 proposed sale of ${doc['value']:,.0f} of {t or doc['issuer']}" + (f" by {doc['person']}" if doc.get("person") else ""),
             "detail": {"shares": doc["shares"], "person": doc["person"]}, "evidence_url": folder_url(doc["cik"], acc)}]


def cluster_buys(conn, days: int = 30) -> list[dict]:
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT cik,ticker,entity,json_extract(detail,'$.owner') o,value,asof FROM signals WHERE kind='insider_buy' AND asof>=?", (since,)).fetchall()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["cik"], []).append(r)
    out = []
    for cik, lst in by.items():
        owners = {r["o"] for r in lst}
        total = sum(r["value"] or 0 for r in lst)
        if len(owners) >= 3 and total >= 100_000:
            latest = max(r["asof"] for r in lst)
            s = 70 + 5 * (len(owners) - 3) + (5 if total >= 1_000_000 else 0)
            wk = date.fromisoformat(latest).isocalendar()
            out.append({"dedupe_key": f"insider_cluster_buy:{cik}:{wk[0]}W{wk[1]}", "asof": latest, "category": "insider", "kind": "insider_cluster_buy",
                        "ticker": lst[0]["ticker"], "cik": cik, "entity": lst[0]["entity"], "direction": "bullish", "strength": clamp(s), "value": round(total, 2),
                        "headline": f"{len(owners)} insiders bought ${total:,.0f} of {lst[0]['ticker'] or lst[0]['entity']} in {days}d",
                        "detail": {"owners": sorted(o for o in owners if o), "window_days": days}})
    return out


# ------------------------------------------------------------------ 8-K / 13D / header-based

def parse_header(html_text: str) -> dict:
    items = re.findall(r"ITEM INFORMATION:\s*([^\n<]+)", html_text)
    subj = re.search(r"SUBJECT COMPANY:.*?COMPANY CONFORMED NAME:\s*([^\n<]+).*?CENTRAL INDEX KEY:\s*(\d+)", html_text, re.S)
    filer = re.search(r"FILED BY:.*?COMPANY CONFORMED NAME:\s*([^\n<]+)", html_text, re.S)
    return {"items": [i.strip() for i in items], "subject_name": subj.group(1).strip() if subj else None,
            "subject_cik": subj.group(2).zfill(10) if subj else None, "filer_name": filer.group(1).strip() if filer else None}


def classify_items(items: list[str]) -> list[tuple[str, int, str, str]]:
    out = []
    for it in items:
        low = it.lower()
        for prefix, code, sev, d in ITEM_MAP:
            if low.startswith(prefix):
                out.append((code, sev, d, it))
                break
    return out


def event_signals(entry: dict, hdr: dict, tmap: dict[str, str]) -> list[dict]:
    form, acc, cik = entry["form"], acc_from_file(entry["file"]), entry["cik"]
    filed = entry["date"]
    sigs: list[dict] = []
    base = lambda kind, cik_, name, **kw: {"dedupe_key": f"{kind}:{acc}:{kw.pop('suffix', '')}", "asof": filed, "kind": kind, "category": CATALOG[kind]["category"],  # noqa: E731
                                           "ticker": tmap.get(cik_), "cik": cik_, "entity": name, "accession": acc, "evidence_url": folder_url(cik, acc), **kw}
    if form in ("8-K", "8-K/A"):
        for code, sev, d, desc in classify_items(hdr["items"]):
            sigs.append(base("event_8k", cik, entry["company"], suffix=code, direction=d, strength=sev if form == "8-K" else sev * 0.6,
                             headline=f"{entry['company']} 8-K item {code}: {desc}", detail={"item": code, "description": desc, "amendment": form != "8-K"}))
    elif form in ("NT 10-K", "NT 10-Q"):
        sigs.append(base("late_filing", cik, entry["company"], direction="risk", strength=60, headline=f"{entry['company']} filed {form} (late filing notice)", detail={"form": form}))
    elif form in ("SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A"):
        sc, name = hdr.get("subject_cik") or cik, hdr.get("subject_name") or entry["company"]
        amend = form.endswith("/A")
        sigs.append(base("activist_13d", sc, name, direction="bullish", strength=40 if amend else 65,
                         headline=f"{hdr.get('filer_name') or 'A holder'} filed {'amended ' if amend else ''}13D on {name}",
                         detail={"filer": hdr.get("filer_name"), "amendment": amend}) | {"ticker": tmap.get(sc)})
    elif form in ("SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A"):
        sc, name = hdr.get("subject_cik") or cik, hdr.get("subject_name") or entry["company"]
        sigs.append(base("passive_13g", sc, name, direction="neutral", strength=25,
                         headline=f"{hdr.get('filer_name') or 'A holder'} filed 13G on {name}", detail={"filer": hdr.get("filer_name")}) | {"ticker": tmap.get(sc)})
    elif form in ("SC TO-T", "SC TO-T/A"):
        sc, name = hdr.get("subject_cik") or cik, hdr.get("subject_name") or entry["company"]
        sigs.append(base("tender_offer", sc, name, direction="bullish", strength=75, headline=f"Tender offer for {name} by {hdr.get('filer_name') or 'a bidder'}",
                         detail={"bidder": hdr.get("filer_name")}) | {"ticker": tmap.get(sc)})
    elif form in ("424B4", "424B5"):
        sigs.append(base("equity_offering", cik, entry["company"], direction="bearish", strength=35, headline=f"{entry['company']} filed {form} prospectus (possible dilution)", detail={"form": form}))
    elif form == "25-NSE":
        sigs.append(base("exchange_delisting", cik, entry["company"], direction="risk", strength=70, headline=f"Exchange delisting notice (25-NSE) for {entry['company']}", detail={}))
    return [s for s in sigs if s.get("ticker")]  # keep only issuers we can trade/identify


HEADER_FORMS = {"8-K", "8-K/A", "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A", "SC TO-T", "SC TO-T/A"}
NO_FETCH_FORMS = {"NT 10-K", "NT 10-Q", "424B4", "424B5", "25-NSE"}


def _fetch_txt(entry: dict) -> str:
    return core.fetch("https://www.sec.gov/Archives/" + entry["file"], as_text=True, cache=False)["body"]


def _fetch_header(entry: dict) -> str:
    acc = acc_from_file(entry["file"])
    return core.fetch(f"{folder_url(entry['cik'], acc)}{acc}-index-headers.html", as_text=True, cache=False)["body"]


def collect_day(conn, d: date, workers: int = 6, limit: int | None = None) -> dict:
    """Process one EDGAR daily index. Idempotent: accessions already processed are skipped."""
    tmap = ticker_map()
    entries = daily_index(d)
    want = [e for e in entries if e["form"] in ("4", "144") or e["form"] in HEADER_FORMS or e["form"] in NO_FETCH_FORMS]
    want = [e for e in want if not store.is_processed(conn, f"sec:{acc_from_file(e['file'])}")]
    if limit:
        want = want[:limit]
    stats = {"date": d.isoformat(), "index_rows": len(entries), "todo": len(want), "signals_new": 0, "errors": 0, "by_form": {}}

    def work(e: dict):
        try:
            if e["form"] == "4":
                x = extract_xml(_fetch_txt(e))
                return e, (form4_signals(parse_form4_doc(x), acc_from_file(e["file"]), e["date"], e["cik"], tmap) if x else []), None
            if e["form"] == "144":
                return e, form144_signals(parse_form144(_fetch_txt(e)) or {}, acc_from_file(e["file"]), e["date"], tmap), None
            if e["form"] in HEADER_FORMS:
                return e, event_signals(e, parse_header(_fetch_header(e)), tmap), None
            return e, event_signals(e, {"items": []}, tmap), None
        except Exception as ex:  # noqa: BLE001
            return e, [], f"{type(ex).__name__}: {ex}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        batch, done = [], []
        for e, sigs, err in pool.map(work, want):
            stats["by_form"][e["form"]] = stats["by_form"].get(e["form"], 0) + 1
            if err:
                stats["errors"] += 1
                continue
            batch.extend(sigs)
            done.append(f"sec:{acc_from_file(e['file'])}")
            if len(batch) >= 200:
                stats["signals_new"] += store.add_signals(conn, batch)
                batch = []
        if batch:
            stats["signals_new"] += store.add_signals(conn, batch)
        store.mark_processed(conn, done)
    return stats
