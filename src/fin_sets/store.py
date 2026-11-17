"""SQLite signal store. One table of typed, scored, evidence-linked signals plus a daily-bar table for backtests."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("SEC_EDGAR_DB", str(Path.home() / ".local" / "share" / "sec-edgar-mcp" / "signals.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY,
  dedupe_key TEXT UNIQUE NOT NULL,
  asof TEXT NOT NULL,            -- date the signal became knowable (filing date or bar date)
  detected_at TEXT NOT NULL,     -- when the collector wrote it
  category TEXT NOT NULL,        -- insider | filing_event | ownership | price | fundamental | macro | prediction_market
  kind TEXT NOT NULL,
  ticker TEXT, cik TEXT, entity TEXT,
  direction TEXT NOT NULL,       -- bullish | bearish | neutral | risk
  strength REAL NOT NULL,        -- 0..100, deterministic, formula per kind in fs_signal_catalog
  value REAL,
  headline TEXT NOT NULL,
  detail TEXT,                   -- JSON
  evidence_url TEXT, accession TEXT,
  backfill INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sig_asof ON signals(asof);
CREATE INDEX IF NOT EXISTS ix_sig_ticker ON signals(ticker, asof);
CREATE INDEX IF NOT EXISTS ix_sig_kind ON signals(kind, asof);
CREATE TABLE IF NOT EXISTS bars(ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, adj REAL, volume REAL, PRIMARY KEY(ticker, date));
CREATE TABLE IF NOT EXISTS processed(key TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, module TEXT, started TEXT, finished TEXT, status TEXT, n_seen INTEGER, n_new INTEGER, error TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, val TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(readonly: bool = False) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if readonly and DB_PATH.exists():
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def add_signals(conn: sqlite3.Connection, sigs: list[dict]) -> int:
    """Insert-or-ignore on dedupe_key. Returns number of new rows."""
    before = conn.total_changes
    t = now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO signals(dedupe_key,asof,detected_at,category,kind,ticker,cik,entity,direction,strength,value,headline,detail,evidence_url,accession,backfill)"
        " VALUES(:dedupe_key,:asof,:detected_at,:category,:kind,:ticker,:cik,:entity,:direction,:strength,:value,:headline,:detail,:evidence_url,:accession,:backfill)",
        [{"detected_at": t, "ticker": None, "cik": None, "entity": None, "value": None, "evidence_url": None, "accession": None,
          "backfill": 0, **s, "strength": round(float(s["strength"]), 1),
          "detail": json.dumps(s.get("detail") or {}, default=str)} for s in sigs],
    )
    conn.commit()
    return conn.total_changes - before


def is_processed(conn, key: str) -> bool:
    return conn.execute("SELECT 1 FROM processed WHERE key=?", (key,)).fetchone() is not None


def mark_processed(conn, keys: list[str]) -> None:
    conn.executemany("INSERT OR IGNORE INTO processed(key,at) VALUES(?,?)", [(k, now_iso()) for k in keys])
    conn.commit()


def kv_get(conn, key: str, default=None):
    r = conn.execute("SELECT val FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(r["val"]) if r else default


def kv_set(conn, key: str, val) -> None:
    conn.execute("INSERT OR REPLACE INTO kv(key,val) VALUES(?,?)", (key, json.dumps(val)))
    conn.commit()
