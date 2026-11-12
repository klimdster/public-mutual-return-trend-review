"""SQLite schema and helpers for Public Mutual fund data."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS funds (
    fund_code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    abbr TEXT,
    fund_type TEXT NOT NULL CHECK(fund_type IN ('UT','PRS')),
    category TEXT,
    is_islamic INTEGER,
    foreign_exposure TEXT,
    dist_policy TEXT,
    launch_date TEXT,
    risk_level REAL,
    currency TEXT DEFAULT 'MYR',
    source_url TEXT,
    updated_at TEXT,
    epf_approved TEXT,
    epf_source TEXT,
    top1_name TEXT, top1_pct REAL,
    top2_name TEXT, top2_pct REAL,
    top3_name TEXT, top3_pct REAL,
    top4_name TEXT, top4_pct REAL,
    top5_name TEXT, top5_pct REAL,
    top_holdings TEXT,
    holdings_as_at TEXT,
    holdings_source TEXT,
    holdings_report_type TEXT
);

CREATE TABLE IF NOT EXISTS nav_daily (
    fund_code TEXT NOT NULL,
    date TEXT NOT NULL,
    nav REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (fund_code, date),
    FOREIGN KEY (fund_code) REFERENCES funds(fund_code)
);

CREATE TABLE IF NOT EXISTS distributions (
    fund_code TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    declare_date TEXT,
    amount_per_unit_sen REAL NOT NULL,
    amount_per_unit_rm REAL NOT NULL,
    dist_type TEXT,
    income_pct REAL,
    capital_pct REAL,
    for_year_end TEXT,
    dist_no INTEGER,
    unit_split REAL,  -- NULL if unknown; 1.0 = no split
    source TEXT NOT NULL,
    PRIMARY KEY (fund_code, ex_date, dist_no),
    FOREIGN KEY (fund_code) REFERENCES funds(fund_code)
);

CREATE TABLE IF NOT EXISTS monthly_returns (
    fund_code TEXT NOT NULL,
    month_end TEXT NOT NULL,
    nav REAL,
    tr_index REAL NOT NULL,          -- rebased to 100 at first available month
    price_return_pct REAL,            -- vs prior month-end NAV
    total_return_pct REAL,            -- vs prior month-end TR index
    cumulative_tr_pct REAL,           -- Scope from chart window start
    source TEXT NOT NULL,
    PRIMARY KEY (fund_code, month_end),
    FOREIGN KEY (fund_code) REFERENCES funds(fund_code)
);

CREATE TABLE IF NOT EXISTS annualised_returns (
    fund_code TEXT NOT NULL,
    as_of TEXT NOT NULL,
    years INTEGER NOT NULL,
    total_return_pct REAL,            -- cumulative over the window
    arithmetic_avg_annual_pct REAL,   -- PHS-style: total/years
    cagr_pct REAL,
    source TEXT NOT NULL,
    PRIMARY KEY (fund_code, as_of, years),
    FOREIGN KEY (fund_code) REFERENCES funds(fund_code)
);

CREATE TABLE IF NOT EXISTS etl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS data_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT,
    gap_type TEXT,
    detail TEXT,
    recorded_at TEXT
);
"""


FUNDS_EXTRA_COLUMNS = {
    "epf_approved": "TEXT",
    "epf_source": "TEXT",
    "top1_name": "TEXT", "top1_pct": "REAL",
    "top2_name": "TEXT", "top2_pct": "REAL",
    "top3_name": "TEXT", "top3_pct": "REAL",
    "top4_name": "TEXT", "top4_pct": "REAL",
    "top5_name": "TEXT", "top5_pct": "REAL",
    "top_holdings": "TEXT",
    "holdings_as_at": "TEXT",
    "holdings_source": "TEXT",
    "holdings_report_type": "TEXT",
}


def migrate(conn: sqlite3.Connection):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(funds)")}
    for name, typ in FUNDS_EXTRA_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE funds ADD COLUMN {name} {typ}")
    conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def upsert_many(conn: sqlite3.Connection, sql: str, rows: Iterable[tuple]):
    conn.executemany(sql, list(rows))
    conn.commit()
