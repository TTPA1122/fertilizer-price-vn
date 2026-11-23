"""Fetch real data from free public sources. Missing values are stored as NULL, never invented."""
import csv
import io
import re
import time
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

FAO_PAGE = "https://www.fao.org/worldfoodsituation/foodpricesindex/en/"
FAO_CSV_FALLBACK = (
    "https://www.fao.org/media/docs/worldfoodsituationlibraries/wfs-library/food_price_indices_data.csv?download=true"
)
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=max&interval=1mo"
VCB_XML = "https://portal.vietcombank.com.vn/Usercontrols/TVPortal.TyGia/pXML.aspx"
COMTRADE_API = "https://comtradeapi.un.org/public/v1"
COMTRADE_PAGE = "https://comtradeplus.un.org/"
COMTRADE_VN, COMTRADE_CHINA = 704, 156
# HS code -> (series suffix, label, product)
COMTRADE_HS = {
    "3102": ("dam", "phân đạm (HS 3102, gồm urê, SA)", "ure"),
    "3104": ("kali", "phân kali (HS 3104)", "kali"),
    "3105": ("hon_hop", "phân hỗn hợp (HS 3105, gồm NPK, DAP, MAP)", "npk"),
}

# header -> (series id, name, category, product, unit)
WORLD_BANK_COLUMNS = {
    "DAP": ("wb_dap", "DAP (f.o.b. US Gulf)", "world", "dap", "USD/tấn"),
    "UREA": ("wb_urea", "Urê (f.o.b. Trung Đông)", "world", "ure", "USD/tấn"),
    "POTASSIUM CHLORIDE": ("wb_kcl", "Kali clorua (CFR Brazil)", "world", "kali", "USD/tấn"),
    "TSP": ("wb_tsp", "TSP (US Gulf)", "world", "tsp", "USD/tấn"),
    "PHOSPHATE ROCK": ("wb_phosphate_rock", "Quặng phosphate (f.o.b. Bắc Phi)", "world", "phosphate_rock", "USD/tấn"),
    "NATURAL GAS, EUROPE": ("wb_gas_eu", "Khí tự nhiên châu Âu (TTF)", "energy", None, "USD/mmbtu"),
    "NATURAL GAS, US": ("wb_gas_us", "Khí tự nhiên Mỹ (Henry Hub)", "energy", None, "USD/mmbtu"),
    "LIQUEFIED NATURAL GAS, JAPAN": ("wb_lng_jp", "LNG Nhật Bản", "energy", None, "USD/mmbtu"),
    "COAL, AUSTRALIAN": ("wb_coal_au", "Than Úc (Newcastle)", "energy", None, "USD/tấn"),
    "CRUDE OIL, BRENT": ("wb_brent", "Dầu thô Brent", "energy", None, "USD/thùng"),
    "RICE, VIET NAMESE 5%": ("wb_rice_vn", "Gạo Việt Nam 5% tấm", "crop", None, "USD/tấn"),
    "COFFEE, ROBUSTA": ("wb_robusta", "Cà phê Robusta", "crop", None, "USD/kg"),
    "MAIZE": ("wb_maize", "Ngô (Mỹ)", "crop", None, "USD/tấn"),
    "RUBBER, TSR20": ("wb_rubber", "Cao su TSR20", "crop", None, "USD/kg"),
}

FAO_COLUMNS = {
    "Food Price Index": ("fao_ffpi", "Chỉ số giá lương thực FAO"),
    "Cereals": ("fao_cereals", "Chỉ số giá ngũ cốc FAO"),
}

# symbol -> (series id, name, category, unit)
YAHOO_SYMBOLS = {
    "VND=X": ("fx_usdvnd", "Tỷ giá USD/VND (thị trường, cuối tháng)", "fx", "VND/USD"),
    "DCM.VN": ("stock_dcm", "Cổ phiếu DCM (Đạm Cà Mau)", "stock", "VND/cp"),
    "DPM.VN": ("stock_dpm", "Cổ phiếu DPM (Đạm Phú Mỹ)", "stock", "VND/cp"),
    "BFC.VN": ("stock_bfc", "Cổ phiếu BFC (Phân bón Bình Điền)", "stock", "VND/cp"),
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
        for sid, name, category, product, unit in cols.values():
            _upsert_series(conn, sid, name, category, product, unit, "World Bank Pink Sheet", url)
        for row in rows_iter[header_idx + 2:]:
            label = str(row[0] or "")
            m = re.fullmatch(r"(\d{4})M(\d{2})", label)
            if not m or int(m.group(1)) < 2000:
                continue
            date = f"{m.group(1)}-{m.group(2)}-01"
            for j, (sid, *_rest) in cols.items():
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


def fetch_fao(client):
    url = FAO_CSV_FALLBACK
    try:
        m = re.search(r'href="([^"]*food_price_indices_data\.csv[^"]*)"', client.get(FAO_PAGE).text)
        if m:
            url = m.group(1).replace("&amp;", "&")
    except httpx.HTTPError:
        pass
    r = client.get(url)
    r.raise_for_status()
    lines = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    header_idx = next(i for i, row in enumerate(lines) if row and row[0].strip() == "Date")
    cols = {j: FAO_COLUMNS[h.strip()] for j, h in enumerate(lines[header_idx]) if h.strip() in FAO_COLUMNS}
    rows = 0
    with connect() as conn:
        for sid, name in cols.values():
            _upsert_series(conn, sid, name, "crop", None, "điểm (2014-2016=100)", "FAO Food Price Index", url)
        for rec in lines[header_idx + 1:]:
            if not rec or not re.fullmatch(r"\d{4}-\d{2}", rec[0].strip()) or int(rec[0][:4]) < 2000:
                continue
            for j, (sid, _) in cols.items():
                v = rec[j].strip() if j < len(rec) else ""
                _upsert_obs(conn, sid, rec[0].strip() + "-01", float(v) if v else None)
                rows += 1
        _log(conn, "fao", True, rows, url)
    return rows


def fetch_yahoo(client):
    rows = 0
    with connect() as conn:
        for symbol, (sid, name, category, unit) in YAHOO_SYMBOLS.items():
            url = YAHOO_CHART.format(symbol=symbol.replace("=", "%3D"))
            r = client.get(url)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            offset = res["meta"].get("gmtoffset", 0)
            closes = res["indicators"]["quote"][0]["close"]
            _upsert_series(conn, sid, name, category, None, unit, "Yahoo Finance (API không chính thức)",
                           f"https://finance.yahoo.com/quote/{symbol}")
            for ts, close in zip(res["timestamp"], closes):
                d = datetime.fromtimestamp(ts + offset, timezone.utc)
                _upsert_obs(conn, sid, f"{d.year:04d}-{d.month:02d}-01", float(close) if close is not None else None)
                rows += 1
        _log(conn, "yahoo", True, rows, "")
    return rows


def fetch_vietcombank(client):
    r = client.get(VCB_XML)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "xml")
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", soup.find("DateTime").get_text(strip=True))
    date = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    usd = soup.find("Exrate", attrs={"CurrencyCode": "USD"})
    with connect() as conn:
        _upsert_series(conn, "fx_vcb_usd", "Tỷ giá USD Vietcombank (bán ra)", "fx", None, "VND/USD",
                       "Vietcombank", VCB_XML)
        _upsert_obs(conn, "fx_vcb_usd", date, _num(usd.get("Sell", "").split(".")[0]) if usd else None)
        _log(conn, "vietcombank", True, 1, f"ngày {date}")
    return 1


SOURCES = {
    "vietnambiz": fetch_vietnambiz,
    "world_bank": fetch_world_bank,
    "bdi": fetch_bdi,
    "fao": fetch_fao,
    "yahoo": fetch_yahoo,
    "vietcombank": fetch_vietcombank,
}


def _comtrade_get(client, path, params):
    for attempt in range(4):
        r = client.get(f"{COMTRADE_API}/{path}", params=params)
        if r.status_code != 429:
            r.raise_for_status()
            return r.json()
        time.sleep(5 * (attempt + 1))
    r.raise_for_status()


def _positive(v):
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def fetch_comtrade(client):
    """Vietnam's monthly fertilizer imports as reported to UN Comtrade (lags ~2-3 years)."""
    meta = _comtrade_get(client, "getDA/C/M/HS", {"reporterCode": COMTRADE_VN})["data"]
    periods = sorted({str(m["period"]) for m in meta})
    with connect() as conn:
        for hs, (suffix, label, product) in COMTRADE_HS.items():
            for kind, name, unit in (
                ("qty", f"Lượng nhập khẩu {label}", "nghìn tấn"),
                ("val", f"Trị giá nhập khẩu {label}", "triệu USD"),
                ("uv", f"Giá nhập bình quân {label}", "USD/tấn"),
                ("cn", f"Tỷ trọng từ Trung Quốc, {label}", "% theo lượng"),
            ):
                _upsert_series(conn, f"ct_{kind}_{suffix}", name, "trade", product, unit, "UN Comtrade", COMTRADE_PAGE)
        have = {r[0] for r in conn.execute("SELECT date FROM observations WHERE series_id='ct_val_dam'")}
    # new periods plus the last 3 stored ones (revisions)
    todo = [p for p in periods if f"{p[:4]}-{p[4:]}-01" not in have]
    todo += [p for p in periods if f"{p[:4]}-{p[4:]}-01" in have][-3:]
    rows = 0
    for period in sorted(set(todo)):
        recs = _comtrade_get(client, "preview/C/M/HS", {
            "reporterCode": COMTRADE_VN, "period": period, "flowCode": "M", "cmdCode": ",".join(COMTRADE_HS),
        }).get("data") or []
        date = f"{period[:4]}-{period[4:]}-01"
        with connect() as conn:
            for hs, (suffix, _, _) in COMTRADE_HS.items():
                def pick(partner):
                    for x in recs:
                        if (x["cmdCode"] == hs and x["partnerCode"] == partner and x.get("partner2Code", 0) == 0
                                and x.get("customsCode") == "C00" and x.get("motCode", 0) == 0):
                            return x
                total, china = pick(0), pick(COMTRADE_CHINA)
                qty = _positive(total and total.get("netWgt"))  # kg
                val = _positive(total and total.get("primaryValue"))  # USD
                cn_qty = _positive(china and china.get("netWgt"))
                _upsert_obs(conn, f"ct_qty_{suffix}", date, qty / 1e6 if qty else None)
                _upsert_obs(conn, f"ct_val_{suffix}", date, val / 1e6 if val else None)
                _upsert_obs(conn, f"ct_uv_{suffix}", date, val / qty * 1000 if qty and val else None)
                _upsert_obs(conn, f"ct_cn_{suffix}", date, cn_qty / qty * 100 if qty and cn_qty else None)
                rows += 4
        time.sleep(1.5)
    with connect() as conn:
        _log(conn, "comtrade", True, rows, f"{len(todo)} kỳ, dữ liệu tới {periods[-1] if periods else '—'}")
    return rows


SOURCES["comtrade"] = fetch_comtrade


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
