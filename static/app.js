const COLORS = ["#317CFF", "#e8590c", "#2b8a3e", "#ae3ec9", "#c92a2a", "#1098ad", "#5c940d", "#495057", "#d6336c", "#f59f00", "#3b5bdb", "#087f5b"];
const PRODUCT_LABEL = { ure: "Urê", dap: "DAP", kali: "Kali", npk: "NPK", lan: "Lân", tsp: "TSP", phosphate_rock: "Quặng phosphate" };
const fmt = new Intl.NumberFormat("vi-VN");
const charts = {};
let SERIES = [];

const api = (path) => fetch(path).then((r) => { if (!r.ok) throw new Error(r.statusText); return r.json(); });
const pct = (a, b) => (a == null || b == null || b === 0 ? null : ((a - b) / b) * 100);
const pctCell = (v) => v == null ? '<td class="num na">—</td>' : `<td class="num ${v > 0 ? "up" : v < 0 ? "down" : ""}">${v > 0 ? "+" : ""}${v.toFixed(1)}%</td>`;
const numCell = (v, d = 0) => v == null ? '<td class="num na">—</td>' : `<td class="num">${fmt.format(Number(v.toFixed(d)))}</td>`;
const viDate = (iso) => iso ? iso.split("-").reverse().join("/") : "—";
const viMonth = (iso) => iso ? `${iso.slice(5, 7)}/${iso.slice(0, 4)}` : "—";

function startDate(years) {
  if (!years) return null;
  const d = new Date();
  d.setFullYear(d.getFullYear() - years);
  return d.toISOString().slice(0, 10);
}

function lineChart(id, datasets, unit, timeUnit) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), {
    type: "line",
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false, spanGaps: false,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      elements: { point: { radius: 0, hitRadius: 6 }, line: { borderWidth: 1.6 } },
      scales: {
        x: { type: "time", time: { unit: timeUnit, tooltipFormat: timeUnit === "day" ? "dd/MM/yyyy" : "MM/yyyy" } },
        y: { title: { display: !!unit, text: unit } },
      },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y == null ? "—" : fmt.format(Math.round(c.parsed.y * 10) / 10)}` } } },
    },
  });
}

const toPoints = (obs) => obs.map((o) => ({ x: o.date, y: o.value }));

function monthly(obs) {
  const acc = {};
  for (const o of obs) {
    if (o.value == null) continue;
    const m = o.date.slice(0, 7) + "-01";
    (acc[m] ||= []).push(o.value);
  }
  return Object.fromEntries(Object.entries(acc).map(([m, v]) => [m, v.reduce((a, b) => a + b, 0) / v.length]));
}

function pearson(xs, ys) {
  const n = xs.length;
  if (n < 6) return null;
  const mx = xs.reduce((a, b) => a + b, 0) / n, my = ys.reduce((a, b) => a + b, 0) / n;
  let sxy = 0, sxx = 0, syy = 0;
  for (let i = 0; i < n; i++) { sxy += (xs[i] - mx) * (ys[i] - my); sxx += (xs[i] - mx) ** 2; syy += (ys[i] - my) ** 2; }
  return sxx && syy ? sxy / Math.sqrt(sxx * syy) : null;
}

function monthlyChanges(map) {
  const keys = Object.keys(map).sort();
  const out = {};
  for (let i = 1; i < keys.length; i++) {
    const [y0, m0] = keys[i - 1].split("-").map(Number), [y1, m1] = keys[i].split("-").map(Number);
    if ((y1 - y0) * 12 + (m1 - m0) === 1) out[keys[i]] = pct(map[keys[i]], map[keys[i - 1]]);
  }
  return out;
}

function shiftMonth(iso, k) {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCMonth(d.getUTCMonth() + k);
  return d.toISOString().slice(0, 10);
}

async function loadStatus() {
  const log = await api("/api/fetch-log");
  const names = { vietnambiz: "VietnamBiz", world_bank: "World Bank", bdi: "BDI", fao: "FAO", yahoo: "Yahoo Finance", vietcombank: "Vietcombank", model: "Mô hình dự báo" };
  document.getElementById("fetch-status").innerHTML = log.map((l) =>
    `${names[l.source] || l.source}: ${l.ok ? "OK" : "lỗi"} lúc ${new Date(l.fetched_at).toLocaleString("vi-VN")}`).join("<br>");
}

async function loadVN() {
  const rows = await api("/api/vn-latest");
  const tbody = document.querySelector("#vn-table tbody");
  tbody.innerHTML = rows.map((r) => `<tr><td>${PRODUCT_LABEL[r.product] || "—"}</td><td>${r.name}</td>${numCell(r.value)}${numCell(r.low)}${numCell(r.high)}${pctCell(pct(r.value, r.prev_value))}<td>${viDate(r.date)}</td></tr>`).join("")
    || '<tr><td colspan="7" class="na">Chưa có dữ liệu</td></tr>';
  const vnSeries = SERIES.filter((s) => s.category === "vn_retail");
  const first = vnSeries.map((s) => s.first_date).filter(Boolean).sort()[0];
  document.getElementById("vn-history-note").textContent = first
    ? `Nguồn chỉ công bố giá của ngày hiện tại, nên lịch sử được app tự lưu từ ngày ${viDate(first)}. Trước ngày này để trống.`
    : "";
  const products = [...new Set(vnSeries.map((s) => s.product).filter(Boolean))];
  const sel = document.getElementById("vn-product");
  sel.innerHTML = products.map((p) => `<option value="${p}">${PRODUCT_LABEL[p] || p}</option>`).join("");
  sel.onchange = drawVN;
  await drawVN();
}

async function drawVN() {
  const product = document.getElementById("vn-product").value;
  const list = SERIES.filter((s) => s.category === "vn_retail" && s.product === product);
  if (!list.length) return;
  const data = await api(`/api/observations?series=${list.map((s) => s.id).join(",")}`);
  lineChart("vn-chart", list.map((s, i) => ({
    label: s.name, data: toPoints(data[s.id] || []), borderColor: COLORS[i % COLORS.length], backgroundColor: COLORS[i % COLORS.length],
    pointRadius: 3,
  })), "VND/bao", "day");
}

async function drawWorld(years) {
  const list = SERIES.filter((s) => s.category === "world");
  const data = await api(`/api/observations?series=${list.map((s) => s.id).join(",")}${years ? `&start=${startDate(years)}` : ""}`);
  lineChart("world-chart", list.map((s, i) => ({ label: s.name, data: toPoints(data[s.id] || []), borderColor: COLORS[i], backgroundColor: COLORS[i] })), "USD/tấn", years && years <= 1 ? "month" : "year");
  const full = await api(`/api/observations?series=${list.map((s) => s.id).join(",")}&start=${startDate(2)}`);
  document.querySelector("#world-table tbody").innerHTML = list.map((s) => {
    const obs = (full[s.id] || []).filter((o) => o.value != null);
    const last = obs[obs.length - 1];
    if (!last) return `<tr><td>${s.name}</td><td>—</td><td class="num na">—</td><td class="num na">—</td><td class="num na">—</td></tr>`;
    const byDate = Object.fromEntries(obs.map((o) => [o.date, o.value]));
    return `<tr><td>${s.name}</td><td>${viMonth(last.date)}</td>${numCell(last.value, 1)}${pctCell(pct(last.value, byDate[shiftMonth(last.date, -1)]))}${pctCell(pct(last.value, byDate[shiftMonth(last.date, -12)]))}</tr>`;
  }).join("");
}

async function drawBDI(years) {
  const data = await api(`/api/observations?series=bdi${years ? `&start=${startDate(years)}` : ""}`);
  const obs = data.bdi || [];
  lineChart("bdi-chart", [{ label: "BDI", data: toPoints(obs), borderColor: COLORS[0], backgroundColor: COLORS[0] }], "điểm", years && years <= 1 ? "month" : "year");
  const all = (await api(`/api/observations?series=bdi&start=${startDate(2)}`)).bdi.filter((o) => o.value != null);
  const last = all[all.length - 1];
  const before = (days) => { const t = new Date(last.date); t.setDate(t.getDate() - days); const iso = t.toISOString().slice(0, 10); return [...all].reverse().find((o) => o.date <= iso); };
  const m1 = before(30), y1 = before(365);
  const kpi = (label, val) => `<div class="kpi"><div class="label">${label}</div><div class="value">${val}</div></div>`;
  const p = (a, b) => { const v = pct(a, b?.value); return v == null ? "—" : `<span class="${v > 0 ? "up" : "down"}">${v > 0 ? "+" : ""}${v.toFixed(1)}%</span>`; };
  document.getElementById("bdi-kpis").innerHTML = last
    ? kpi(`Mới nhất (${viDate(last.date)})`, fmt.format(last.value)) + kpi("So với 1 tháng trước", p(last.value, m1)) + kpi("So với 1 năm trước", p(last.value, y1))
    : "";
}

async function drawCompare(years) {
  const start = startDate(years);
  const world = SERIES.filter((s) => s.category === "world" && ["ure", "dap", "kali"].includes(s.product));
  const vn = SERIES.filter((s) => s.category === "vn_retail");
  const ids = ["bdi", ...world.map((s) => s.id), ...vn.map((s) => s.id)];
  const data = await api(`/api/observations?series=${ids.join(",")}&start=${start}`);
  const lines = [];
  lines.push({ label: "Cước tàu (BDI)", map: monthly(data.bdi || []) });
  for (const s of world) lines.push({ label: `Thế giới: ${PRODUCT_LABEL[s.product]}`, map: monthly(data[s.id] || []) });
  const vnGroups = {};
  for (const s of vn) if (s.product) (vnGroups[s.product] ||= []).push(...(data[s.id] || []));
  for (const [p, obs] of Object.entries(vnGroups)) lines.push({ label: `Trong nước: ${PRODUCT_LABEL[p]}`, map: monthly(obs), dashed: true });
  const datasets = lines.map((l, i) => {
    const keys = Object.keys(l.map).sort();
    const base = keys.length ? l.map[keys[0]] : null;
    return { label: l.label, data: keys.map((k) => ({ x: k, y: base ? (l.map[k] / base) * 100 : null })),
      borderColor: COLORS[i % COLORS.length], backgroundColor: COLORS[i % COLORS.length], borderDash: l.dashed ? [5, 4] : [], pointRadius: keys.length < 3 ? 3 : 0 };
  });
  lineChart("compare-chart", datasets, "chỉ số (đầu kỳ = 100)", years <= 1 ? "month" : "year");

  const bdiCh = monthlyChanges(monthly(data.bdi || []));
  document.querySelector("#corr-table tbody").innerHTML = world.map((s) => {
    const ch = monthlyChanges(monthly(data[s.id] || []));
    let n0 = 0;
    const cells = [0, 1, 2, 3].map((lag) => {
      const xs = [], ys = [];
      for (const [m, v] of Object.entries(bdiCh)) {
        const t = ch[shiftMonth(m, lag)];
        if (v != null && t != null) { xs.push(v); ys.push(t); }
      }
      if (lag === 0) n0 = xs.length;
      const r = pearson(xs, ys);
      return r == null ? '<td class="num na">—</td>' : `<td class="num">${r.toFixed(2)}</td>`;
    });
    return `<tr><td>${s.name}</td>${cells.join("")}<td class="num">${n0}</td></tr>`;
  }).join("");
}

const accTxt = (a) => a ? `${(a.acc * 100).toFixed(0)}%` : "—";
let FORECAST = null;

async function loadForecast() {
  try { FORECAST = await api("/api/forecast"); } catch (e) {
    document.getElementById("fc-cards").innerHTML = `<p class="na">Dự báo chưa sẵn sàng: ${e.message}</p>`;
    return;
  }
  const res = FORECAST.results;
  document.getElementById("fc-meta").textContent = `Tính lúc ${new Date(FORECAST.generated_at).toLocaleString("vi-VN")}`;
  document.getElementById("fc-cards").innerHTML = res.map((r) => {
    const up = r.direction === "up";
    const p = up ? r.prob_up : 1 - r.prob_up;
    const b = r.backtest;
    const beats = b.model && b.momentum && b.majority && b.model.acc > b.momentum.acc && b.model.acc > b.majority.acc;
    return `<div class="fc-card">
      <div class="title">${r.target_name} · ${r.horizon === 1 ? "tháng tới" : "3 tháng tới"}</div>
      <div class="muted small">Tháng ${viMonth(r.as_of)}: ${fmt.format(r.last_price)} USD/tấn → dự báo cho ${viMonth(r.for_month)}</div>
      <div class="dir ${up ? "up" : "down"}">${up ? "▲ Tăng" : "▼ Giảm"} · xác suất ${(p * 100).toFixed(0)}%</div>
      <div class="small">Backtest: đúng ${accTxt(b.model)} · theo đà ${accTxt(b.momentum)}</div>
      ${beats ? "" : '<div class="warn">Mô hình chưa tốt hơn cách đoán đơn giản ở kỳ này, chỉ nên tham khảo.</div>'}
      <ul>${r.drivers.slice(0, 4).map((d) => `<li>${d.name}: kéo ${d.effect > 0 ? "lên" : "xuống"}</li>`).join("")}</ul>
    </div>`;
  }).join("");
  document.querySelector("#fc-table tbody").innerHTML = res.map((r) => {
    const b = r.backtest;
    return `<tr><td>${r.target_name}</td><td>${r.horizon} tháng</td><td class="num">${accTxt(b.model)}</td><td class="num">${accTxt(b.momentum)}</td><td class="num">${accTxt(b.majority)}</td><td class="num">${accTxt(b.model_last24)}</td><td class="num">${accTxt(b.momentum_last24)}</td><td class="num">${b.model ? b.model.n : "—"}</td></tr>`;
  }).join("");
  const sel = document.getElementById("fc-select");
  sel.innerHTML = res.map((r, i) => `<option value="${i}">${r.target_name} · ${r.horizon} tháng</option>`).join("");
  sel.onchange = drawForecastHistory;
  drawForecastHistory();
}

function drawForecastHistory() {
  const r = FORECAST.results[Number(document.getElementById("fc-select").value)];
  const pts = r.history;
  lineChart("fc-chart", [
    { label: "Xác suất tăng theo mô hình", data: pts.map((h) => ({ x: h.date, y: h.prob_up })), borderColor: COLORS[0], backgroundColor: COLORS[0] },
    { label: "Thực tế (1 = tăng, 0 = giảm)", data: pts.map((h) => ({ x: h.date, y: h.actual })), borderColor: COLORS[1], backgroundColor: COLORS[1], showLine: false, pointRadius: 2 },
  ], "xác suất", "year");
}

async function drawDrivers(years) {
  if (years !== undefined) drawDrivers.years = years;
  const cat = document.getElementById("drv-cat").value;
  const list = SERIES.filter((s) => s.category === cat);
  if (!list.length) return;
  const data = await api(`/api/observations?series=${list.map((s) => s.id).join(",")}&start=${startDate(drawDrivers.years)}`);
  lineChart("drv-chart", list.map((s, i) => {
    const map = monthly(data[s.id] || []);
    const keys = Object.keys(map).sort();
    const base = keys.length ? map[keys[0]] : null;
    return { label: s.name, data: keys.map((k) => ({ x: k, y: base ? (map[k] / base) * 100 : null })), borderColor: COLORS[i % COLORS.length], backgroundColor: COLORS[i % COLORS.length], pointRadius: keys.length < 3 ? 3 : 0 };
  }), "chỉ số (đầu kỳ = 100)", drawDrivers.years <= 1 ? "month" : "year");
  const recent = await api(`/api/observations?series=${list.map((s) => s.id).join(",")}&start=${startDate(2)}`);
  document.querySelector("#drv-table tbody").innerHTML = list.map((s) => {
    const obs = (recent[s.id] || []).filter((o) => o.value != null);
    const last = obs[obs.length - 1];
    if (!last) return `<tr><td>${s.name}</td><td>${s.unit}</td><td>—</td><td class="num na">—</td><td class="num na">—</td><td class="num na">—</td></tr>`;
    const prev = obs[obs.length - 2];
    const daily = !last.date.endsWith("-01") || s.id === "fx_vcb_usd";
    const byDate = Object.fromEntries(obs.map((o) => [o.date, o.value]));
    return `<tr><td>${s.name}</td><td>${s.unit}</td><td>${daily ? viDate(last.date) : viMonth(last.date)}</td>${numCell(last.value, 2)}${pctCell(pct(last.value, prev?.value))}${daily ? '<td class="num na">—</td>' : pctCell(pct(last.value, byDate[shiftMonth(last.date, -12)]))}</tr>`;
  }).join("");
}

function sourcesTable() {
  document.querySelector("#sources-table tbody").innerHTML = SERIES.map((s) =>
    `<tr><td>${s.name}</td><td>${s.unit}</td><td><a href="${s.source_url}" target="_blank" rel="noopener">${s.source}</a></td><td>${viDate(s.first_date)}</td><td>${viDate(s.last_date)}</td><td class="num">${fmt.format(s.n_values)}</td><td class="num">${fmt.format(s.n_missing || 0)}</td></tr>`).join("");
}

function bindRange(name, fn) {
  const box = document.querySelector(`[data-range="${name}"]`);
  box.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    box.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    fn(Number(b.dataset.years));
  });
  fn(Number(box.querySelector("button.active").dataset.years));
}

(async function main() {
  SERIES = await api("/api/series");
  sourcesTable();
  loadStatus();
  loadVN();
  bindRange("world", drawWorld);
  bindRange("bdi", drawBDI);
  loadForecast();
  document.getElementById("drv-cat").onchange = () => drawDrivers();
  bindRange("drivers", drawDrivers);
  bindRange("compare", drawCompare);
})();
