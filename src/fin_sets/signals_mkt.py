"""Price-technical and prediction-market signal engines. Pure indicator functions (offline tested) + batch collectors.

Price indicators run on daily bars (adjusted close where available). Nothing here forecasts: each signal is a dated, reproducible fact about a series.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from . import core, store

CATALOG_PRICE = {
    "breakout_52w_high": {"category": "price", "direction": "bullish", "formula": "close >= max of prior 252 closes: 55 + min(25, volume_ratio*5) "},
    "breakdown_52w_low": {"category": "price", "direction": "bearish", "formula": "close <= min of prior 252 closes: 55 + min(25, volume_ratio*5)"},
    "volume_spike": {"category": "price", "direction": "neutral", "formula": "volume >= 3x trailing 20d average: 30 + 10*log2(ratio/3)... capped 90, direction follows the day's return"},
    "big_move_1d": {"category": "price", "direction": "varies", "formula": "abs(1d return) >= 3.5 trailing daily sigmas and >= 5%: 35 + 10*(z-3.5), cap 95"},
    "gap": {"category": "price", "direction": "varies", "formula": "open vs prior close >= 4% and gap not filled intraday: 30 + 3*gap%, cap 80"},
    "rsi_oversold": {"category": "price", "direction": "bullish", "formula": "RSI(14) <= 25: 30 + (25-RSI)*2"},
    "rsi_overbought": {"category": "price", "direction": "bearish", "formula": "RSI(14) >= 80: 30 + (RSI-80)*2"},
    "golden_cross": {"category": "price", "direction": "bullish", "formula": "SMA50 crosses above SMA200 today: 50"},
    "death_cross": {"category": "price", "direction": "bearish", "formula": "SMA50 crosses below SMA200 today: 50"},
    "drawdown_deep": {"category": "price", "direction": "risk", "formula": "close >= 40% below 252d peak: 30 + (dd%-40)"},
    "momentum_12_1": {"category": "price", "direction": "bullish", "formula": "12-1 month return >= +60%: 30 + (ret%-60)/4"},
    "squeeze_bb": {"category": "price", "direction": "neutral", "formula": "Bollinger bandwidth at its 120d minimum (volatility compression): 35"},
}


def sma(x: list[float], n: int, end: int | None = None) -> float | None:
    end = len(x) if end is None else end
    return None if end < n else sum(x[end - n:end]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        # 100 needs at least one gain. A series with no gains and no losses makes the
        # ratio undefined, so report null; 100 there would emit a false overbought signal.
        return None if ag == 0 else 100.0
    return 100 - 100 / (1 + ag / al)


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def price_signals(ticker: str, bars: list[dict], name: str | None = None, cik: str | None = None) -> list[dict]:
    bars = [b for b in bars if b.get("close") is not None and b.get("volume") is not None]
    if len(bars) < 60:
        return []
    last = bars[-1]
    asof = last["date"]
    px = [b["adj_close"] or b["close"] for b in bars]
    raw = [b["close"] for b in bars]
    vol = [b["volume"] for b in bars]
    c = px[-1]
    # liquidity gate: skip illiquid names so signals stay tradable
    dollar_vol = sum(raw[-20:]) / 20 * sum(vol[-20:]) / 20
    if raw[-1] < 1.0 or dollar_vol < 1_000_000:
        return []
    rets = [px[i] / px[i - 1] - 1 for i in range(1, len(px))]
    r1 = rets[-1]
    win = rets[-61:-1] if len(rets) > 61 else rets[:-1]
    mu = sum(win) / len(win)
    sd = math.sqrt(sum((r - mu) ** 2 for r in win) / max(1, len(win) - 1)) or 1e-9
    z = (r1 - mu) / sd
    avg_vol = sum(vol[-21:-1]) / 20 or 1
    vr = vol[-1] / avg_vol
    out: list[dict] = []

    def add(kind: str, direction: str, strength: float, headline: str, value: float | None = None, detail: dict | None = None):
        wk = date.fromisoformat(asof).isocalendar()
        period = f"{wk[0]}W{wk[1]}" if kind in ("momentum_12_1", "drawdown_deep", "squeeze_bb") else asof  # slow states: once per week
        out.append({"dedupe_key": f"{kind}:{ticker}:{period}", "asof": asof, "category": "price", "kind": kind, "ticker": ticker, "cik": cik, "entity": name,
                    "direction": direction, "strength": _clamp(strength), "value": value, "headline": headline,
                    "detail": {"close": raw[-1], "ret_1d_pct": round(r1 * 100, 2), "volume_ratio_20d": round(vr, 2), **(detail or {})}})

    prior = px[-253:-1]
    if len(prior) >= 200:
        if c >= max(prior):
            add("breakout_52w_high", "bullish", 55 + min(25, vr * 5), f"{ticker} closed at a 52-week high ${raw[-1]:,.2f} on {vr:.1f}x volume", c)
        elif c <= min(prior):
            add("breakdown_52w_low", "bearish", 55 + min(25, vr * 5), f"{ticker} closed at a 52-week low ${raw[-1]:,.2f} on {vr:.1f}x volume", c)
        peak = max(px[-252:])
        dd = (1 - c / peak) * 100
        if dd >= 40:
            add("drawdown_deep", "risk", 30 + (dd - 40), f"{ticker} is {dd:.0f}% below its 252-day peak", dd)
        if len(px) >= 252:
            mom = (px[-22] / px[-253] - 1) * 100 if len(px) >= 253 else None
            if mom is not None and mom >= 60:
                add("momentum_12_1", "bullish", 30 + (mom - 60) / 4, f"{ticker} 12-1 month momentum +{mom:.0f}%", mom)
    if vr >= 3 and vol[-1] * raw[-1] >= 2_000_000:
        add("volume_spike", "bullish" if r1 > 0.01 else ("bearish" if r1 < -0.01 else "neutral"), 30 + 10 * math.log2(vr / 3 + 1) * 2,
            f"{ticker} volume {vr:.1f}x its 20-day average, price {r1 * 100:+.1f}%", vr)
    if abs(z) >= 3.5 and abs(r1) >= 0.05:
        add("big_move_1d", "bullish" if r1 > 0 else "bearish", 35 + 10 * (abs(z) - 3.5), f"{ticker} moved {r1 * 100:+.1f}% ({abs(z):.1f} sigma)", r1 * 100, {"z": round(z, 2)})
    prev_close, o = raw[-2], bars[-1]["open"]
    if o and prev_close:
        gp = (o / prev_close - 1) * 100
        filled = (bars[-1]["low"] <= prev_close) if gp > 0 else (bars[-1]["high"] >= prev_close)
        if abs(gp) >= 4 and not filled:
            add("gap", "bullish" if gp > 0 else "bearish", 30 + 3 * abs(gp), f"{ticker} gapped {gp:+.1f}% and held the gap", gp)
    rs = rsi(px)
    if rs is not None:
        if rs <= 25:
            add("rsi_oversold", "bullish", 30 + (25 - rs) * 2, f"{ticker} RSI(14) {rs:.0f} (oversold)", rs)
        elif rs >= 80:
            add("rsi_overbought", "bearish", 30 + (rs - 80) * 2, f"{ticker} RSI(14) {rs:.0f} (overbought)", rs)
    if len(px) >= 201:
        s50n, s200n = sma(px, 50), sma(px, 200)
        s50p, s200p = sma(px, 50, len(px) - 1), sma(px, 200, len(px) - 1)
        if None not in (s50n, s200n, s50p, s200p):
            if s50p <= s200p and s50n > s200n:
                add("golden_cross", "bullish", 50, f"{ticker} SMA50 crossed above SMA200", s50n / s200n - 1)
            elif s50p >= s200p and s50n < s200n:
                add("death_cross", "bearish", 50, f"{ticker} SMA50 crossed below SMA200", s50n / s200n - 1)
    if len(px) >= 140:
        bw = []
        for end in range(len(px) - 120, len(px) + 1):
            w = px[end - 20:end]
            m = sum(w) / 20
            s = math.sqrt(sum((v - m) ** 2 for v in w) / 20)
            bw.append(4 * s / m if m else 9)
        if bw[-1] <= min(bw) + 1e-12:
            add("squeeze_bb", "neutral", 35, f"{ticker} Bollinger bandwidth at a 120-day low (volatility squeeze)", bw[-1] * 100)
    return out


def universe(limit: int | None = None) -> list[dict]:
    rows = []
    seen = set()
    for r in core._tickers().values():
        t = r["ticker"]
        if t in seen or "-" in t and len(t) > 5:
            continue
        seen.add(t)
        rows.append({"ticker": t, "cik": f"{r['cik_str']:010d}", "name": r["title"]})
    return rows[:limit] if limit else rows


def collect_prices(conn, tickers: list[dict], workers: int = 8, rng: str = "2y") -> dict:
    stats = {"tickers": len(tickers), "with_bars": 0, "signals_new": 0, "errors": 0}

    def work(t):
        try:
            p = core.price_history(t["ticker"], rng)
            return t, p, None
        except Exception as e:  # noqa: BLE001
            return t, None, str(e)

    batch = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for t, p, err in pool.map(work, tickers):
            if err or not p or not p["bars"]:
                stats["errors"] += 1
                continue
            stats["with_bars"] += 1
            from . import outcomes as _o
            if _o.is_liquid(p["bars"]):
                _o.ensure(conn)
                _o.store_bars(conn, t["ticker"], p["bars"])
                stats["bars_stored"] = stats.get("bars_stored", 0) + 1
            batch.extend(price_signals(t["ticker"], p["bars"], t["name"], t["cik"]))
            if len(batch) >= 300:
                stats["signals_new"] += store.add_signals(conn, batch)
                batch = []
    if batch:
        stats["signals_new"] += store.add_signals(conn, batch)
    return stats


# ------------------------------------------------------------------ prediction markets

CATALOG_PM = {
    "pm_probability_move": {"category": "prediction_market", "direction": "varies", "formula": "Polymarket 1d price change >= 8 points on liquidity >= $25k: 30 + 2*points, cap 90"},
    "pm_volume_surge": {"category": "prediction_market", "direction": "neutral", "formula": "24h volume >= 4x the market's 7d daily average and >= $50k: 35 + 5*log2(ratio)"},
    "pm_extreme_probability": {"category": "prediction_market", "direction": "neutral", "formula": "contested-to-decided: probability >= 92% or <= 8% with liquidity >= $100k: 25"},
    "pm_kalshi_mispricing": {"category": "prediction_market", "direction": "neutral", "formula": "Kalshi yes bid+no bid > $1.01 or yes ask+no ask < $0.99 at size: arbitrage-looking book, 40 (verify fees)"},
}


def pm_signals_polymarket(markets: list[dict], asof: str) -> list[dict]:
    out = []
    for m in markets:
        try:
            q = m.get("question") or ""
            slug = m.get("slug") or m.get("id")
            liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
            v24 = float(m.get("volume24hr") or 0)
            v7 = float(m.get("volume1wk") or 0)
            ch = m.get("oneDayPriceChange")
            last = m.get("lastTradePrice")
            url = f"https://polymarket.com/event/{slug}"
            base = {"asof": asof, "category": "prediction_market", "entity": q, "evidence_url": url, "cik": None, "ticker": None}
            if ch is not None and abs(float(ch)) * 100 >= 8 and liq >= 25_000:
                pts = abs(float(ch)) * 100
                out.append({**base, "dedupe_key": f"pm_probability_move:poly:{m['id']}:{asof}", "kind": "pm_probability_move", "direction": "neutral",
                            "strength": _clamp(30 + 2 * pts, 0, 90), "value": float(ch) * 100,
                            "headline": f"Polymarket: {q} moved {float(ch) * 100:+.0f} pts in 24h to {float(last or 0) * 100:.0f}%", "detail": {"liquidity": liq, "volume24h": v24, "source": "polymarket"}})
            base7 = v7 / 7 if v7 else 0
            if base7 > 0 and v24 >= 50_000 and v24 / base7 >= 4:
                out.append({**base, "dedupe_key": f"pm_volume_surge:poly:{m['id']}:{asof}", "kind": "pm_volume_surge", "direction": "neutral",
                            "strength": _clamp(35 + 5 * math.log2(v24 / base7)), "value": v24 / base7,
                            "headline": f"Polymarket: {q} 24h volume ${v24:,.0f} is {v24 / base7:.1f}x its weekly daily average", "detail": {"liquidity": liq, "source": "polymarket"}})
            if last is not None and liq >= 100_000 and (float(last) >= 0.92 or float(last) <= 0.08):
                out.append({**base, "dedupe_key": f"pm_extreme_probability:poly:{m['id']}:{asof}", "kind": "pm_extreme_probability", "direction": "neutral", "strength": 25,
                            "value": float(last) * 100, "headline": f"Polymarket: {q} at {float(last) * 100:.0f}%", "detail": {"liquidity": liq, "source": "polymarket"}})
        except (TypeError, ValueError, KeyError):
            continue
    return out


def pm_signals_kalshi(markets: list[dict], asof: str) -> list[dict]:
    out = []
    for m in markets:
        try:
            yb, ya, nb, na = (float(m[k]) for k in ("yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars", "no_ask_dollars"))
            ys, ns = float(m.get("yes_bid_size_fp") or 0), float(m.get("no_bid_size_fp") or 0)
            if min(yb, nb) > 0 and yb + nb > 1.01 and min(ys, ns) >= 100:
                out.append({"dedupe_key": f"pm_kalshi_mispricing:{m['ticker']}:{asof}", "asof": asof, "category": "prediction_market", "kind": "pm_kalshi_mispricing",
                            "entity": m.get("title"), "direction": "neutral", "strength": 40, "value": yb + nb - 1, "ticker": None, "cik": None,
                            "evidence_url": f"https://kalshi.com/markets/{m.get('event_ticker', '').lower()}",
                            "headline": f"Kalshi: {m.get('title')} bids sum to ${yb + nb:.2f} (crossed book)", "detail": {"yes_bid": yb, "no_bid": nb, "sizes": [ys, ns], "source": "kalshi"}})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def collect_pm(conn, poly_pages: int = 6) -> dict:
    asof = date.today().isoformat()
    stats = {"polymarket": 0, "kalshi": 0, "signals_new": 0}
    sigs = []
    for pg in range(poly_pages):
        try:
            r = core.fetch(f"https://gamma-api.polymarket.com/markets?limit=100&offset={pg * 100}&closed=false&active=true&order=volume24hr&ascending=false", ttl=0, cache=False)["body"]
        except Exception:  # noqa: BLE001
            break
        if not r:
            break
        stats["polymarket"] += len(r)
        sigs += pm_signals_polymarket(r, asof)
    cursor = ""
    for _ in range(6):
        try:
            u = "https://api.elections.kalshi.com/trade-api/v2/markets?limit=1000&status=open" + (f"&cursor={cursor}" if cursor else "")
            body = core.fetch(u, ttl=0, cache=False)["body"]
        except Exception:  # noqa: BLE001
            break
        stats["kalshi"] += len(body.get("markets", []))
        sigs += pm_signals_kalshi(body.get("markets", []), asof)
        cursor = body.get("cursor") or ""
        if not cursor:
            break
    stats["signals_new"] = store.add_signals(conn, sigs)
    return stats
