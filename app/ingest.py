"""Fetch real data from free public sources. Missing values are stored as NULL, never invented."""
import csv
import io
import re
import unicodedata
from datetime import datetime, timezone

import httpx
import openpyxl
from bs4 import BeautifulSoup

from .db import connect

UA = {"User-Agent": "Mozilla/5.0 (fertilizer-price-vn; +https://github.com)"}

VIETNAMBIZ_URL = "https://vietnambiz.vn/gia-phan-bon.html"
WORLD_BANK_PAGE = "https://www.worldbank.org/en/research/commodity-markets"
WORLD_BANK_XLSX_FALLBACK = (
    "https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/"
    "related/CMO-Historical-Data-Monthly.xlsx"
)
BDI_CSV_URL = (
    "https://www.dashboard-konjunktur.de/api/visualizationdownload/downloadcsv"
    "?id=ed5c016e-ea60-4202-8b52-9c0a391814aa"
)

WORLD_BANK_COLUMNS = {
    "DAP": ("wb_dap", "DAP (f.o.b. US Gulf)", "dap"),
    "UREA": ("wb_urea", "Urê (f.o.b. Trung Đông)", "ure"),
    "POTASSIUM CHLORIDE": ("wb_kcl", "Kali clorua (CFR Brazil)", "kali"),
    "TSP": ("wb_tsp", "TSP (US Gulf)", "tsp"),
    "PHOSPHATE ROCK": ("wb_phosphate_rock", "Quặng phosphate (f.o.b. Bắc Phi)", "phosphate_rock"),
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text):
    t = unicodedata.normalize("NFD", text.lower()).replace("đ", "d")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")


def _product_of(name):
    s = _slug(name)
    for key, product in (("ure", "ure"), ("dap", "dap"), ("kali", "kali"), ("npk", "npk"), ("lan", "lan")):
        if s.startswith(key) or f"_{key}_" in f"_{s}_":
            return product
    return None


def _display_name(name):
    words = []
    for w in name.title().split():
        words.append({"Dap": "DAP", "Npk": "NPK", "Ure": "Urê"}.get(w, w))
    return " ".join(words)


def _num(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return float(digits) if digits else None


def _log(conn, source, ok, rows, message=""):
    conn.execute(
        "INSERT INTO fetch_log(source, fetched_at, ok, rows, message) VALUES (?,?,?,?,?)",
        (source, _now(), int(ok), rows, message[:500]),
    )


def _upsert_series(conn, sid, name, category, product, unit, source, url):
    conn.execute(
        """INSERT INTO series(id,name,category,product,unit,source,source_url) VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name, product=excluded.product,
           unit=excluded.unit, source_url=excluded.source_url""",
        (sid, name, category, product, unit, source, url),
    )


def _upsert_obs(conn, sid, date, value, low=None, high=None):
    conn.execute(
        """INSERT INTO observations(series_id,date,value,low,high,fetched_at) VALUES (?,?,?,?,?,?)
           ON CONFLICT(series_id,date) DO UPDATE SET value=excluded.value, low=excluded.low,
           high=excluded.high, fetched_at=excluded.fetched_at""",
        (sid, date, value, low, high, _now()),
    )


def fetch_vietnambiz(client):
    r = client.get(VIETNAMBIZ_URL)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    m = re.search(r"Cập nhật lúc:\s*(\d{2})/(\d{2})/(\d{4})", soup.get_text(" ", strip=True))
    if not m:
        raise ValueError("Không tìm thấy ngày cập nhật trên VietnamBiz")
    date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    table = soup.select_one("table.table-phanbon")
    if table is None:
        raise ValueError("Không tìm thấy bảng giá phân bón trên VietnamBiz")
    rows = 0
    with connect() as conn:
        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            name = tds[0].get_text(strip=True)
            unit = tds[1].get_text(strip=True).lower()
            price = tds[2].select_one(".pb-cp--price")
            low = tds[2].select_one(".pb-cp--price-min")
            high = tds[2].select_one(".pb-cp--price-max")
            sid = "vnb_" + _slug(name)
            _upsert_series(conn, sid, _display_name(name), "vn_retail", _product_of(name), f"VND/{unit}",
                           "VietnamBiz", VIETNAMBIZ_URL)
            _upsert_obs(conn, sid, date,
                        _num(price.get_text()) if price else None,
                        _num(low.get_text()) if low else None,
                        _num(high.get_text()) if high else None)
            rows += 1
        _log(conn, "vietnambiz", True, rows, f"ngày số liệu {date}")
    return rows


def _world_bank_xlsx_url(client):
    try:
        r = client.get(WORLD_BANK_PAGE)
        m = re.search(r'https://thedocs\.worldbank\.org/[^"\']*CMO-Historical-Data-Monthly\.xlsx', r.text)
        if m:
            return m.group(0)
    except httpx.HTTPError:
        pass
    return WORLD_BANK_XLSX_FALLBACK


def fetch_world_bank(client):
    url = _world_bank_xlsx_url(client)
    r = client.get(url)
    r.raise_for_status()
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    rows_iter = list(wb["Monthly Prices"].iter_rows(values_only=True))
    header_idx = next(i for i, row in enumerate(rows_iter) if row[1] and "Crude oil" in str(row[1]))
    header = rows_iter[header_idx]
    cols = {}
    for j, h in enumerate(header):
        key = str(h or "").replace("*", "").strip().upper()
        if key in WORLD_BANK_COLUMNS:
            cols[j] = WORLD_BANK_COLUMNS[key]
    rows = 0
    with connect() as conn:
        for sid, name, product in cols.values():
            _upsert_series(conn, sid, name, "world", product, "USD/tấn", "World Bank Pink Sheet", url)
        for row in rows_iter[header_idx + 2:]:
            label = str(row[0] or "")
            m = re.fullmatch(r"(\d{4})M(\d{2})", label)
            if not m or int(m.group(1)) < 2000:
                continue
            date = f"{m.group(1)}-{m.group(2)}-01"
            for j, (sid, _, _) in cols.items():
                v = row[j]
                _upsert_obs(conn, sid, date, float(v) if isinstance(v, (int, float)) else None)
                rows += 1
        _log(conn, "world_bank", True, rows, url)
    return rows


def fetch_bdi(client):
    r = client.get(BDI_CSV_URL)
    r.raise_for_status()
    reader = csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";")
    next(reader, None)
    rows = 0
    with connect() as conn:
        _upsert_series(conn, "bdi", "Baltic Dry Index (cước tàu hàng rời)", "freight", None, "điểm",
                       "Baltic Exchange qua Dashboard Konjunktur (DL-DE/BY-2.0)", BDI_CSV_URL)
        for rec in reader:
            if len(rec) < 2 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rec[0]):
                continue
            val = rec[1].strip().replace(",", ".")
            _upsert_obs(conn, "bdi", rec[0], float(val) if val else None)
            rows += 1
        _log(conn, "bdi", True, rows, "")
    return rows


SOURCES = {"vietnambiz": fetch_vietnambiz, "world_bank": fetch_world_bank, "bdi": fetch_bdi}


def run(names=None):
    results = {}
    with httpx.Client(headers=UA, timeout=60, follow_redirects=True) as client:
        for name in names or SOURCES:
            try:
                results[name] = {"ok": True, "rows": SOURCES[name](client)}
            except Exception as exc:  # keep other sources running
                with connect() as conn:
                    _log(conn, name, False, 0, repr(exc))
                results[name] = {"ok": False, "error": repr(exc)}
    return results


if __name__ == "__main__":
    from .db import init_db

    init_db()
    print(run())
