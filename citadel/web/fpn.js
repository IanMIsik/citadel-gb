const statusEl = document.getElementById("connection-status");

const GENERATION_SERIES = [
  { key: "misco_indo", label: "Actual demand", color: "#72c4f6" },
  { key: "misco_ndf", label: "Adjusted NDF", color: "#e0645a" },
  { key: "misco_da_adj_ndf", label: "Day-ahead adjusted", color: "#58b06a" },
  { key: "adjusted_fpn", label: "Adjusted FPN (total)", color: "#f6c445" },
];

const DELTA_SERIES = [
  { key: "delta", label: "Delta", color: "#e0645a" },
  { key: "delta_av", label: "Delta (period avg)", color: "#72c4f6" },
];

const OVER_UNDER_SERIES = [
  { key: "market_gen_vs_adj_fpn", label: "Market gen vs adjusted FPN", color: "#b06af6" },
];

function fmt(value, digits = 1) {
  return value == null || Number.isNaN(Number(value)) ? "--" : Number(value).toFixed(digits);
}

function setFigure(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = value == null ? "--" : fmt(value);
  el.setAttribute("data-sign", value == null ? "" : String(value));
}

function td(value, digits = 0) {
  return `<td data-sign="${value == null ? "" : value}">${fmt(value, digits)}</td>`;
}

function makeChart(canvasId, series) {
  const ctx = document.getElementById(canvasId);
  return new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: series.map((s) => ({
        label: s.label, data: [], borderColor: s.color, backgroundColor: s.color,
        pointRadius: 0, borderWidth: 1.5, tension: 0.15,
      })),
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { ticks: { color: "#8a8f9a", maxTicksLimit: 6 }, grid: { color: "#2a2e38" } },
        y: { ticks: { color: "#8a8f9a" }, grid: { color: "#2a2e38" } },
      },
      plugins: { legend: { display: false } },
    },
  });
}

function renderLegend(elId, chart, series) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = "";
  series.forEach((s, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span style="background:${s.color}"></span>${s.label}`;
    li.onclick = () => {
      const meta = chart.getDatasetMeta(i);
      meta.hidden = meta.hidden === null ? !chart.data.datasets[i].hidden : !meta.hidden;
      li.classList.toggle("strike", !!meta.hidden);
      chart.update();
    };
    el.appendChild(li);
  });
}

function updateChart(chart, series, rows) {
  chart.data.labels = rows.map((r) => new Date(r.spot_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  series.forEach((s, i) => { chart.data.datasets[i].data = rows.map((r) => r[s.key]); });
  chart.update();
}

const generationChart = makeChart("generation-chart", GENERATION_SERIES);
renderLegend("generation-legend", generationChart, GENERATION_SERIES);
const deltaChart = makeChart("delta-chart", DELTA_SERIES);
renderLegend("delta-legend", deltaChart, DELTA_SERIES);
const overUnderChart = makeChart("over-under-chart", OVER_UNDER_SERIES);

async function loadCurrent() {
  const resp = await fetch("/api/fpn/current");
  if (!resp.ok) return;
  const data = await resp.json();
  const rows = (data.aggregated || []).slice().sort((a, b) => new Date(a.spot_time) - new Date(b.spot_time));
  updateChart(generationChart, GENERATION_SERIES, rows);
  updateChart(deltaChart, DELTA_SERIES, rows);
  updateChart(overUnderChart, OVER_UNDER_SERIES, rows);

  const latest = rows[rows.length - 1];
  if (latest) {
    setFigure("ds-niv-estimate", latest.niv_estimate);
    setFigure("ds-auc-av", latest.auc_av);
    setFigure("ds-unexp-delta", latest.unexp_delta);
  }
}

// --- Left column: per-fuel-type / per-SP pivot tables ---

function renderFuelPivot(tableId, rows, valueKey, digits = 0) {
  const table = document.getElementById(tableId);
  const fuelTypes = [...new Set(rows.map((r) => r.fuel_type))].filter(Boolean).sort();
  const periods = [...new Set(rows.map((r) => r.settlement_period))].sort((a, b) => a - b);
  const byKey = new Map(rows.map((r) => [`${r.settlement_period}|${r.fuel_type}`, r[valueKey]]));

  table.querySelector("thead tr").innerHTML = "<th>Fuel</th>" + periods.map((sp) => `<th>SP${sp}</th>`).join("");
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  for (const ft of fuelTypes) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${ft}</td>` + periods.map((sp) => td(byKey.get(`${sp}|${ft}`), digits)).join("");
    tbody.appendChild(tr);
  }
}

function computeFpnDelta(byFuelSp) {
  const byFuel = new Map();
  for (const r of byFuelSp) {
    if (!byFuel.has(r.fuel_type)) byFuel.set(r.fuel_type, []);
    byFuel.get(r.fuel_type).push(r);
  }
  const out = [];
  for (const [fuel_type, rows] of byFuel) {
    rows.sort((a, b) => a.settlement_period - b.settlement_period);
    rows.forEach((row, i) => {
      const next = rows[i + 1];
      out.push({
        settlement_period: row.settlement_period,
        fuel_type,
        fpn_delta: next ? row.fpn_spot_vol - next.fpn_spot_vol : null,
      });
    });
  }
  return out;
}

function renderMetricTable(tableId, rows, columns) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  tbody.innerHTML = "";
  const sorted = rows.slice().sort((a, b) => a.settlement_period - b.settlement_period);
  for (const r of sorted) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.settlement_period}</td>` + columns.map((c) => td(r[c], c === "niv_estimate" ? 1 : 1)).join("");
    tbody.appendChild(tr);
  }
}

async function loadDashboard() {
  const resp = await fetch("/api/fpn/dashboard");
  if (!resp.ok) return;
  const data = await resp.json();
  const byFuelSp = data.by_fuel_sp || [];
  const decisionDriversSp = data.decision_drivers_sp || [];

  renderFuelPivot("tbl-fpn-by-fuel", byFuelSp, "fpn_spot_vol");
  renderFuelPivot("tbl-fpn-delta", computeFpnDelta(byFuelSp), "fpn_delta");
  renderFuelPivot("tbl-mel-mil-drop", byFuelSp, "mel_mil_drop");
  renderFuelPivot("tbl-market-gen-vs-adj-fpn", byFuelSp, "market_gen_vs_adj_fpn");

  renderMetricTable("tbl-decision-drivers", decisionDriversSp, ["auc", "delta", "niv_error", "dmd_risk", "dmd_error", "unexp"]);
  renderMetricTable("tbl-decision", decisionDriversSp, ["niv_estimate", "auc", "wind_deviation", "other_gen_deviation", "dmd_error", "unexp"]);
}

// --- Right column: worst deviants + generation-by-fuel-type tables ---

async function loadWorstDeviants() {
  const resp = await fetch("/api/fpn/worst-deviants");
  if (!resp.ok) return;
  const data = await resp.json();
  const byUnit = new Map();
  for (const r of data.worst_deviants || []) {
    if (!byUnit.has(r.bm_unit) || new Date(r.spot_time) > new Date(byUnit.get(r.bm_unit).spot_time)) byUnit.set(r.bm_unit, r);
  }
  const rows = [...byUnit.values()].sort((a, b) => Math.abs(b.mel_mil_downside || 0) - Math.abs(a.mel_mil_downside || 0));

  const tbody = document.querySelector("#worst-deviants-table tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.bm_unit}</td><td>${r.fuel_type || ""}</td>` +
      td(r.fpn_spot_vol) + td(r.adjusted_fpn) + td(r.mel_mil_downside, 2);
    tbody.appendChild(tr);
  }
}

function renderGenerationPivot(tableId, rows, valueKey, maxCols = 24) {
  const table = document.getElementById(tableId);
  const fuelTypes = [...new Set(rows.map((r) => r.fuel_type))].sort();
  const byTs = new Map();
  for (const r of rows) {
    if (!byTs.has(r.ts)) byTs.set(r.ts, {});
    byTs.get(r.ts)[r.fuel_type] = r[valueKey];
  }
  const timestamps = [...byTs.keys()].sort((a, b) => new Date(b) - new Date(a)).slice(0, maxCols);

  table.querySelector("thead tr").innerHTML = "<th>Time</th>" + fuelTypes.map((ft) => `<th class="rotate"><div><span>${ft}</span></div></th>`).join("");
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  for (const ts of timestamps) {
    const tr = document.createElement("tr");
    const timeCell = `<td>${new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</td>`;
    tr.innerHTML = timeCell + fuelTypes.map((ft) => td(byTs.get(ts)[ft])).join("");
    tbody.appendChild(tr);
  }
}

async function loadGenerationByFuel() {
  const resp = await fetch("/api/fpn/generation-by-fuel?hours=3");
  if (!resp.ok) return;
  const data = await resp.json();
  const rows = data.generation_by_fuel || [];
  const interconnectorRows = rows.filter((r) => (r.fuel_type || "").toUpperCase().startsWith("INT"));
  const otherRows = rows.filter((r) => !(r.fuel_type || "").toUpperCase().startsWith("INT"));

  renderGenerationPivot("tbl-interconnector-gen", interconnectorRows, "real_gen");
  renderGenerationPivot("tbl-other-gen", otherRows, "market_gen");
}

async function loadAll() {
  await Promise.all([loadCurrent(), loadDashboard(), loadWorstDeviants(), loadGenerationByFuel()]);
}

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/fpn`);

  ws.onopen = () => { statusEl.textContent = "live"; statusEl.className = "connected"; };
  ws.onclose = () => {
    statusEl.textContent = "reconnecting…";
    statusEl.className = "disconnected";
    setTimeout(connectWebSocket, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type !== "fpn_update") return;
    loadAll();
  };
  setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send("ping"); }, 30000);
}

loadAll();
connectWebSocket();
setInterval(loadAll, 30000);
