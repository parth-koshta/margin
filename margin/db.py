from __future__ import annotations

import os
import sqlite3

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY,
    uid           TEXT UNIQUE NOT NULL,      -- source:message-id:n
    source        TEXT NOT NULL,             -- email | sms
    sender        TEXT,
    bank          TEXT,
    ts            TEXT NOT NULL,             -- ISO datetime (local)
    amount        REAL NOT NULL,
    direction     TEXT NOT NULL CHECK (direction IN ('in','out')),
    account       TEXT,                      -- e.g. 'HDFC card ••1234'
    last4         TEXT,
    counterparty  TEXT,
    vpa           TEXT,                      -- UPI id, when the alert has one
    category      TEXT,
    category_locked INTEGER DEFAULT 0,
    duplicate_of  INTEGER,                   -- same txn seen via another source
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS tx_ts ON transactions(ts);

CREATE TABLE IF NOT EXISTS balances (
    account TEXT NOT NULL, ts TEXT NOT NULL, balance REAL NOT NULL, source TEXT,
    PRIMARY KEY (account, ts)
);

CREATE TABLE IF NOT EXISTS card_dues (
    account TEXT NOT NULL, ts TEXT NOT NULL, total_due REAL NOT NULL, due_date TEXT,
    PRIMARY KEY (account, ts)
);

CREATE TABLE IF NOT EXISTS holdings (
    source_file TEXT NOT NULL, name TEXT NOT NULL, type TEXT, units REAL, value REAL NOT NULL, as_of TEXT
);

CREATE TABLE IF NOT EXISTS networth_history (
    date TEXT PRIMARY KEY, assets REAL, liabilities REAL, net REAL
);

CREATE TABLE IF NOT EXISTS review (            -- looked financial but wasn't counted
    uid TEXT PRIMARY KEY, source TEXT, sender TEXT, ts TEXT, reason TEXT, snippet TEXT
);

CREATE TABLE IF NOT EXISTS seen (                -- every message already processed
    uid TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS reviews (             -- daily / weekly / monthly reviews
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, created TEXT NOT NULL,
    headline TEXT, summary TEXT, flags TEXT, by_ai INTEGER DEFAULT 0, note TEXT,
    UNIQUE (kind, start)
);

CREATE TABLE IF NOT EXISTS ai_decisions (        -- what the AI sorted, so it can be shown and undone
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, job TEXT NOT NULL, key TEXT NOT NULL, verdict TEXT NOT NULL,
    label TEXT, reason TEXT, detail TEXT, applied INTEGER DEFAULT 0, undone INTEGER DEFAULT 0,
    UNIQUE (job, key)
);

CREATE TABLE IF NOT EXISTS flag_state (          -- flags you marked "looks fine"
    fkey TEXT PRIMARY KEY, state TEXT NOT NULL, at TEXT
);

-- Investments (invest.py). kind: in_stock | mf | us_stock | nps
CREATE TABLE IF NOT EXISTS inv_statements (      -- monthly statements read: CDSL CAS, NPS
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, as_of TEXT, read_at TEXT, ok INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS inv_positions (       -- what each statement says you held on its date
    stmt TEXT NOT NULL, as_of TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, name TEXT,
    units REAL, price REAL, value REAL, cost REAL, extra TEXT
);
CREATE INDEX IF NOT EXISTS inv_pos_asof ON inv_positions(as_of);
CREATE TABLE IF NOT EXISTS us_orders (           -- INDmoney order emails that went through
    id TEXT PRIMARY KEY, ts TEXT NOT NULL, side TEXT NOT NULL, name TEXT NOT NULL, shares REAL NOT NULL,
    price REAL, amount REAL
);
CREATE TABLE IF NOT EXISTS us_tickers (          -- INDmoney's company name -> ticker
    name TEXT PRIMARY KEY, ticker TEXT, how TEXT
);
CREATE TABLE IF NOT EXISTS splits (ticker TEXT NOT NULL, day TEXT NOT NULL, ratio REAL NOT NULL,
    PRIMARY KEY (ticker, day));
CREATE TABLE IF NOT EXISTS prices (              -- closing prices: ISIN (India), ticker (US, indices)
    key TEXT NOT NULL, day TEXT NOT NULL, close REAL NOT NULL, prev REAL, cur TEXT, PRIMARY KEY (key, day)
);
CREATE TABLE IF NOT EXISTS inv_history (         -- value of each kind on each day, for the charts
    date TEXT NOT NULL, kind TEXT NOT NULL, value REAL NOT NULL, cost REAL, PRIMARY KEY (date, kind)
);
CREATE TABLE IF NOT EXISTS stops (               -- your stop-loss levels; alerts only, nothing is ever sold
    key TEXT PRIMARY KEY, kind TEXT, name TEXT, price REAL NOT NULL, set_at TEXT, hit_at TEXT, hit_close REAL
);
CREATE TABLE IF NOT EXISTS inv_reviews (         -- the AI's weekly / monthly look at the investments
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, created TEXT NOT NULL,
    headline TEXT, summary TEXT, watch TEXT, by_ai INTEGER DEFAULT 0, note TEXT, UNIQUE (kind, start)
);
"""


def connect() -> sqlite3.Connection:
    paths.ensure_data_dir()
    conn = sqlite3.connect(paths.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    os.chmod(paths.DB_PATH, 0o600)
    return conn


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
