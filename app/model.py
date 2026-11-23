"""Up/down forecast for world fertilizer prices with a walk-forward backtest.

Only real observations are used. Months where a driver has no data stay NaN inside the
feature matrix; the model pipeline imputes them for fitting only and nothing is written back.
"""
import json
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .db import DATA_DIR, connect

FORECAST_PATH = DATA_DIR / "forecast.json"

TARGETS = {"wb_urea": "Urê", "wb_dap": "DAP", "wb_kcl": "Kali"}
HORIZONS = (1, 3)
DRIVERS = {
    "wb_gas_eu": "Khí châu Âu",
    "wb_gas_us": "Khí Mỹ",
    "wb_lng_jp": "LNG Nhật",
    "wb_coal_au": "Than Úc",
    "wb_brent": "Dầu Brent",
    "bdi": "Cước tàu BDI",
    "fx_usdvnd": "Tỷ giá USD/VND",
    "wb_rice_vn": "Gạo VN",
    "wb_maize": "Ngô",
    "wb_robusta": "Cà phê Robusta",
    "wb_rubber": "Cao su",
    "fao_ffpi": "Chỉ số lương thực FAO",
    "wb_urea": "Urê thế giới",
    "wb_dap": "DAP thế giới",
    "wb_kcl": "Kali thế giới",
    "wb_phosphate_rock": "Quặng phosphate",
    "stock_dcm": "Cổ phiếu DCM",
    "stock_dpm": "Cổ phiếu DPM",
}
TEST_START = "2010-01-01"
MIN_TRAIN = 60


def load_monthly():
    ids = list(dict.fromkeys(list(TARGETS) + list(DRIVERS)))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT series_id, date, value FROM observations WHERE series_id IN ({','.join('?' * len(ids))})",
            ids,
        ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.to_period("M").dt.to_timestamp()
    wide = df.groupby(["month", "series_id"])["value"].mean().unstack()
    return wide.sort_index()


def build_features(wide, target):
    logp = np.log(wide.where(wide > 0))
    f = pd.DataFrame(index=wide.index)
    own = logp[target]
    for k in (1, 3, 6, 12):
        f[f"own_r{k}"] = own.diff(k)
    f["own_dev12"] = own - own.rolling(12).mean()
    f["own_vol6"] = own.diff().rolling(6).std()
    for sid in DRIVERS:
        if sid == target or sid not in logp:
            continue
        f[f"{sid}_r1"] = logp[sid].diff(1)
        f[f"{sid}_r3"] = logp[sid].diff(3)
    m = f.index.month
    f["season_sin"] = np.sin(2 * np.pi * m / 12)
    f["season_cos"] = np.cos(2 * np.pi * m / 12)
    return f, own


def make_model():
    return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=0.1, max_iter=2000))


def feature_label(col):
    if col.startswith("own_"):
        return {"own_r1": "Đà giá 1 tháng", "own_r3": "Đà giá 3 tháng", "own_r6": "Đà giá 6 tháng",
                "own_r12": "Đà giá 12 tháng", "own_dev12": "Lệch so với TB 12 tháng",
                "own_vol6": "Biến động 6 tháng"}[col]
    if col.startswith("season"):
        return "Mùa vụ"
    sid, k = col.rsplit("_r", 1)
    return f"{DRIVERS.get(sid, sid)} ({k} tháng)"


def run_target(wide, target, h):
    f, own = build_features(wide, target)
    fut = own.shift(-h) - own
    valid = own.notna() & f["own_r12"].notna()
    f, own, fut = f[valid], own[valid], fut[valid]
    label = (fut > 0).astype(float).where(fut.notna() & (fut != 0))
    dates = f.index
    history = []
    for i, t in enumerate(dates):
        if t < pd.Timestamp(TEST_START) or np.isnan(label.iloc[i]):
            continue
        known = (dates <= t - pd.DateOffset(months=h)) & label.notna().values
        if known.sum() < MIN_TRAIN:
            continue
        X, y = f[known], label[known]
        if y.nunique() < 2:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            p = float(make_model().fit(X, y).predict_proba(f.iloc[[i]])[0, 1])
        past = own.iloc[i] - own.shift(h).iloc[i]
        history.append({
            "date": t.strftime("%Y-%m-%d"),
            "actual": int(label.iloc[i]),
            "prob_up": round(p, 4),
            "pred": int(p >= 0.5),
            "momentum": None if np.isnan(past) or past == 0 else int(past > 0),
            "majority": int(y.mean() >= 0.5),
        })

    def acc(rows, key):
        r = [x for x in rows if x[key] is not None]
        return {"acc": round(sum(x[key] == x["actual"] for x in r) / len(r), 4), "n": len(r)} if r else None

    last24 = history[-24:]
    known = label.notna().values
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = make_model().fit(f[known], label[known])
    x_now = f.iloc[[-1]]
    p_now = float(model.predict_proba(x_now)[0, 1])
    lr = model[-1]
    z = model[:-1].transform(x_now)[0]
    contrib = {}
    for col, c, v in zip(f.columns, lr.coef_[0], z):
        name = feature_label(col)
        contrib[name] = contrib.get(name, 0.0) + float(c * v)
    top = sorted(contrib.items(), key=lambda kv: -abs(kv[1]))[:6]
    asof = dates[-1]
    return {
        "target": target,
        "target_name": TARGETS[target],
        "horizon": h,
        "as_of": asof.strftime("%Y-%m-%d"),
        "for_month": (asof + pd.DateOffset(months=h)).strftime("%Y-%m-%d"),
        "last_price": round(float(wide[target].loc[asof]), 2),
        "prob_up": round(p_now, 4),
        "direction": "up" if p_now >= 0.5 else "down",
        "drivers": [{"name": n, "effect": round(v, 3)} for n, v in top],
        "backtest": {
            "start": history[0]["date"] if history else None,
            "model": acc(history, "pred"),
            "momentum": acc(history, "momentum"),
            "majority": acc(history, "majority"),
            "model_last24": acc(last24, "pred"),
            "momentum_last24": acc(last24, "momentum"),
            "ties_excluded": int((fut == 0).sum()),
        },
        "history": history,
    }


def build():
    wide = load_monthly()
    results = [run_target(wide, t, h) for t in TARGETS for h in HORIZONS if t in wide]
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "Hồi quy logistic, huấn luyện lại mỗi tháng chỉ với dữ liệu đã biết tại thời điểm đó (walk-forward)",
        "results": results,
    }
    tmp = FORECAST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    tmp.replace(FORECAST_PATH)
    return out


def load():
    if FORECAST_PATH.exists():
        return json.loads(FORECAST_PATH.read_text())
    return build()


if __name__ == "__main__":
    o = build()
    for r in o["results"]:
        b = r["backtest"]
        print(r["target_name"], r["horizon"], r["as_of"], r["direction"], r["prob_up"],
              "model", b["model"], "mom", b["momentum"], "maj", b["majority"], "l24", b["model_last24"], b["momentum_last24"])
        print("   ", r["drivers"][:4])
