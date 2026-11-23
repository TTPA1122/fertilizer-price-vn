import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "prices.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,      -- vn_retail | world | freight
    product TEXT,                -- ure | dap | kali | npk | lan | tsp | phosphate_rock | NULL
    unit TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    series_id TEXT NOT NULL REFERENCES series(id),
    date TEXT NOT NULL,          -- YYYY-MM-DD (monthly series use day 01)
    value REAL,                  -- NULL when the source has no value
    low REAL,
    high REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (series_id, date)
);
CREATE TABLE IF NOT EXISTS fetch_log (
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    message TEXT
);
"""


@contextmanager
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
