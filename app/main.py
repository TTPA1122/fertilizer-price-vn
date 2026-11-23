import os
from collections import defaultdict
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import threading

from . import ingest, model
from .db import connect, init_db

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Giá phân bón Việt Nam")
scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")


def refresh(names):
    ingest.run(names)
    try:
        model.build()
    except Exception as exc:  # forecast failure must not break ingestion
        with connect() as conn:
            ingest._log(conn, "model", False, 0, repr(exc))


@app.on_event("startup")
def startup():
    init_db()
    if not model.FORECAST_PATH.exists():
        threading.Thread(target=model.build, daemon=True).start()
    if os.environ.get("DISABLE_SCHEDULER") != "1":
        scheduler.add_job(refresh, "cron", hour="9,15", minute=0,
                          args=[["vietnambiz", "bdi", "vietcombank", "yahoo"]], id="daily")
        scheduler.add_job(refresh, "cron", day="1-10", hour=10, minute=0, args=[["world_bank", "fao", "comtrade"]], id="monthly")
        scheduler.start()


@app.on_event("shutdown")
def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/api/series")
def list_series():
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.*, MIN(o.date) AS first_date, MAX(o.date) AS last_date,
                      COUNT(o.value) AS n_values, SUM(o.value IS NULL) AS n_missing
               FROM series s LEFT JOIN observations o ON o.series_id = s.id
               GROUP BY s.id ORDER BY s.category, s.product, s.name"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/observations")
def observations(series: str, start: str | None = None, end: str | None = None):
    ids = [s for s in series.split(",") if s]
    if not ids:
        raise HTTPException(400, "series is required")
    q = f"SELECT series_id, date, value, low, high FROM observations WHERE series_id IN ({','.join('?' * len(ids))})"
    params = list(ids)
    if start:
        q += " AND date >= ?"
        params.append(start)
    if end:
        q += " AND date <= ?"
        params.append(end)
    out = defaultdict(list)
    with connect() as conn:
        for r in conn.execute(q + " ORDER BY date", params):
            out[r["series_id"]].append({"date": r["date"], "value": r["value"], "low": r["low"], "high": r["high"]})
    return out


@app.get("/api/vn-latest")
def vn_latest():
    """Latest domestic retail price per product with change vs the previous recorded day."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.id, s.name, s.product, s.unit, s.source, s.source_url, o.date, o.value, o.low, o.high,
                      (SELECT p.value FROM observations p WHERE p.series_id = s.id AND p.date < o.date
                       ORDER BY p.date DESC LIMIT 1) AS prev_value,
                      (SELECT p.date FROM observations p WHERE p.series_id = s.id AND p.date < o.date
                       ORDER BY p.date DESC LIMIT 1) AS prev_date
               FROM series s JOIN observations o ON o.series_id = s.id
               WHERE s.category = 'vn_retail'
                 AND o.date = (SELECT MAX(date) FROM observations WHERE series_id = s.id)
               ORDER BY s.product, s.name"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/fetch-log")
def fetch_log():
    with connect() as conn:
        rows = conn.execute(
            """SELECT f.* FROM fetch_log f
               JOIN (SELECT source, MAX(fetched_at) m FROM fetch_log GROUP BY source) l
                 ON l.source = f.source AND l.m = f.fetched_at"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/forecast")
def forecast():
    if not model.FORECAST_PATH.exists():
        raise HTTPException(503, "Dự báo đang được tính, thử lại sau ít phút")
    return model.load()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
