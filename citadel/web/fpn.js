const statusEl = document.getElementById("connection-status");

// "Generation Chart" -- named that on this page; the real Zapdos source
// calls it "Forecast Chart" (components/chartjs/forecast_chart.js). Only
// 6 of that source's 7 lines are shown here, by request (cobble_ndf/
// misco_ndf is the one left out), labelled with their own raw field names
// (not a friendlier description) and each one's exact color + dash
// "characteristic" (solid/dotted) copied from that source's own lineConfig
// object -- this project's own "misco"/"spot" naming already mirrors that
// source's "cobble"/"spot" split one-for-one:
//   spot_latest_ndf         -> spot_latest_ndf  { color: '#d6d6d6', dotted: true }
//   adjusted_fpn_by_spottime-> adjusted_fpn     { color: '#09AB00' }
//   fpn_by_spottime         -> fpn_spot_vol     { color: '#7FF878' }
//   cobble_indo             -> misco_indo       { color: '#ffffff' }
//   spot_indo               -> spot_indo        { color: '#FFE72E' }
//   cobble_da_adj_ndf       -> misco_da_adj_ndf { color: '#ff3333', dotted: true }
const GENERATION_SERIES = [
  { key: "spot_latest_ndf", label: "spot_latest_ndf", color: "#d6d6d6", dash: true },
  { key: "adjusted_fpn", label: "adjusted_fpn", color: "#09AB00" },
  { key: "fpn_spot_vol", label: "fpn_spot_vol", color: "#7FF878" },
  { key: "misco_indo", label: "misco_indo", color: "#ffffff" },
  { key: "spot_indo", label: "spot_indo", color: "#FFE72E" },
  { key: "misco_da_adj_ndf", label: "misco_da_adj_ndf", color: "#ff3333", dash: true },
];

// "Delta_chart" -- a combo chart (a semi-transparent filled area alongside
// plain lines) in the real Zapdos source (components/chartjs/delta_chart.js),
// labelled and colored the same way as GENERATION_SERIES above -- raw field
// names, each one's exact color/fill/dash copied from that source's own
// lineConfig object:
//   auc                -> area_under_curve   { color: 'rgba(87,184,255,0.3)', fill: 'origin' }
//   auc_av             -> auc_av             { color: 'rgb(87,184,255)' }
//   niv_spot_time_max  -> delta              { color: 'red', dotted: true }
//   niv_sp_max         -> delta_av           { color: 'red' }
//   cobble_indo_vs_ndf -> misco_indo_vs_ndf  { color: '#fff' }
//   niv_error          -> niv_error          { color: 'green' }
// (that source's own 7th line, unexp_delta, is left out here, same as
// GENERATION_SERIES leaving out cobble_ndf.)
const DELTA_SERIES = [
  { key: "area_under_curve", label: "area_under_curve", color: "rgba(87,184,255,0.3)", fill: true },
  { key: "auc_av", label: "auc_av", color: "rgb(87,184,255)" },
  { key: "delta", label: "delta", color: "red", dash: true },
  { key: "delta_av", label: "delta_av", color: "red" },
  { key: "misco_indo_vs_ndf", label: "misco_indo_vs_ndf", color: "#fff" },
  { key: "niv_error", label: "niv_error", color: "green" },
];

const FUEL_COLOR_PALETTE = [
  "#72c4f6", "#e0645a", "#58b06a", "#f6c445", "#b06af6", "#4d8dff",
  "#ff9f4d", "#4de0c8", "#e04dd0", "#a3d94d", "#d94d8f", "#4d76d9",
];

function fuelColor(fuelType, index) {
  return FUEL_COLOR_PALETTE[index % FUEL_COLOR_PALETTE.length];
}

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

// Exact magnitude-band shading from the real Zapdos app source
// (components/tables/generate-fuel-type-table.js), applied there to this
// same table (`market_gen_vs_adj_fpn_by_sp`) -- discrete bands, not a
// continuous scale: |value| <= 50 stays plain, then low/medium/high (or
// their negative mirrors, a different colour ramp) as magnitude grows.
// Colors are that source's own custom.css values for these exact
// data-range names, ported verbatim.
function tdShaded(value, digits = 0) {
  if (value == null) return td(value, digits);
  const v = Number(value);
  let range = null;
  if (v > 300) range = "high";
  else if (v > 200) range = "medium";
  else if (v > 50) range = "low";
  else if (v <= -301) range = "high-negative";
  else if (v <= -201) range = "medium-negative";
  else if (v <= -51) range = "low-negative";
  const rangeAttr = range ? ` data-range="${range}"` : "";
  return `<td data-sign="${value}"${rangeAttr}>${fmt(value, digits)}</td>`;
}

function makeChart(canvasId, series) {
  const ctx = document.getElementById(canvasId);
  return new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: series.map((s) => ({
        label: s.label, data: [], borderColor: s.color, backgroundColor: s.color,
        pointRadius: 0, borderWidth: s.width || 1.5, tension: 0.15,
        borderDash: s.dash ? [5, 5] : undefined,
        fill: s.fill ? "origin" : false,
      })),
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      // "nearest" (not "index") -- one popup for whichever single line is
      // closest to the cursor, not one combined popup listing every line
      // at that x position. `intersect: false` means you don't have to
      // land exactly on the (invisible, pointRadius: 0) line itself.
      interaction: { mode: "nearest", intersect: false },
      hover: { mode: "nearest", intersect: false },
      scales: {
        // Tick text is `--table-text` (this page's own table-body colour,
        // the "brighter white" the tables use) rather than `--muted` --
        // matches the tables around this chart instead of reading dimmer.
        x: {
          // Smaller than the default (~12px) so the two-line SP/time label
          // fits inside the box's own fixed height (313px, matching "Fpn
          // by Fuel") instead of needing a taller box for it.
          ticks: { color: "#b9bbb3", autoSkip: false, maxRotation: 0, minRotation: 0, font: { size: 9 }, padding: 2 },
          grid: { color: "#2a2e38" },
          // Only ever tick at a real settlement-period boundary (set by
          // updateChart on `chart.$tickIndices`) -- Chart.js's own autoSkip
          // otherwise picks arbitrary minute-level indices, which showed a
          // time like "11:45 AM" next to "SP24" even though SP24 actually
          // starts at 11:30 (see updateChart's own comment).
          afterBuildTicks: (axis) => {
            const keep = axis.chart.$tickIndices;
            if (keep) axis.ticks = axis.ticks.filter((t) => keep.has(t.value));
          },
        },
        y: { ticks: { color: "#b9bbb3" }, grid: { color: "#2a2e38" } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          mode: "nearest",
          intersect: false,
          backgroundColor: "#171a21",
          borderColor: "#2a2e38",
          borderWidth: 1,
          titleColor: "#b9bbb3",
          bodyColor: "#b9bbb3",
        },
      },
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

// Ticks only ever land on a settlement period's own first minute (its real
// start), labelled with that period's number stacked above its own real
// start time -- not an arbitrary minute-level index Chart.js's own
// autoSkip might otherwise land on, which showed a time like "11:45 AM"
// next to "SP24" even though SP24 actually starts at 11:30. `step` further
// thins the boundary list so labels never crowd on a narrow portrait
// chart -- ~50px is roughly what a two-line "SP24"/"11:30 AM" tick needs.
function updateChart(chart, series, rows) {
  chart.data.labels = rows.map((r) => [
    r.settlement_period != null ? `SP${r.settlement_period}` : "",
    new Date(r.spot_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
  ]);
  series.forEach((s, i) => { chart.data.datasets[i].data = rows.map((r) => r[s.key]); });

  const boundaries = [];
  let lastSp = null;
  rows.forEach((r, i) => {
    if (r.settlement_period !== lastSp) { boundaries.push(i); lastSp = r.settlement_period; }
  });
  const width = chart.width || chart.canvas.clientWidth || 300;
  const maxTicks = Math.max(2, Math.floor(width / 65));
  const step = Math.max(1, Math.ceil(boundaries.length / maxTicks));
  chart.$tickIndices = new Set(boundaries.filter((_, i) => i % step === 0));

  chart.update();
}

// Market_gen vs Adj_fpn is one toggleable line per fuel type (matches the
// real Zapdos home page's own chart, which plots biomass/ccgt/coal/wind/
// etc as separate series -- not a single aggregate line).
function updateDynamicFuelChart(chart, legendElId, rows, valueKey) {
  const fuelTypes = [...new Set(rows.map((r) => r.fuel_type))].filter(Boolean).sort();
  const byKey = new Map();
  for (const r of rows) {
    const key = r.fuel_type;
    if (!byKey.has(key)) byKey.set(key, new Map());
    byKey.get(key).set(r.spot_time, r[valueKey]);
  }
  const spotTimes = [...new Set(rows.map((r) => r.spot_time))].sort((a, b) => new Date(a) - new Date(b));

  chart.data.labels = spotTimes.map((t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  chart.data.datasets = fuelTypes.map((ft, i) => ({
    label: ft, data: spotTimes.map((t) => byKey.get(ft)?.get(t)),
    borderColor: fuelColor(ft, i), backgroundColor: fuelColor(ft, i),
    pointRadius: 0, borderWidth: 1.5, tension: 0.15,
  }));
  chart.update();
  renderLegend(legendElId, chart, fuelTypes.map((ft, i) => ({ label: ft, color: fuelColor(ft, i) })));
}

const generationChart = makeChart("generation-chart", GENERATION_SERIES);
const deltaChart = makeChart("delta-chart", DELTA_SERIES);
const marketGenChart = makeChart("market-gen-vs-adj-fpn-chart", []);

async function loadCurrent() {
  const resp = await fetch("/api/fpn/current");
  if (!resp.ok) return;
  const data = await resp.json();
  const rows = (data.aggregated || []).slice().sort((a, b) => new Date(a.spot_time) - new Date(b.spot_time));

  const fpnValues = rows.map((r) => r.fpn_spot_vol).filter((v) => v != null);
  const adjFpnValues = rows.map((r) => r.adjusted_fpn).filter((v) => v != null);
  const average = (values) => (values.length ? values.reduce((a, b) => a + b, 0) / values.length : null);
  setFigure("stat-average-fpn", average(fpnValues));
  setFigure("stat-average-adj-fpn", average(adjFpnValues));
}

// --- Left column: per-fuel-type / per-SP pivot tables ---

function renderFuelPivot(tableId, rows, valueKey, digits = 0, shaded = false) {
  const table = document.getElementById(tableId);
  const cell = shaded ? tdShaded : td;
  const fuelTypes = [...new Set(rows.map((r) => r.fuel_type))].filter(Boolean).sort();
  const periods = [...new Set(rows.map((r) => r.settlement_period))].sort((a, b) => a - b);
  const byKey = new Map(rows.map((r) => [`${r.settlement_period}|${r.fuel_type}`, r[valueKey]]));

  table.querySelector("thead tr").innerHTML = "<th>Fuel</th>" + periods.map((sp) => `<th>SP${sp}</th>`).join("");
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  for (const ft of fuelTypes) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${ft}</td>` + periods.map((sp) => cell(byKey.get(`${sp}|${ft}`), digits)).join("");
    tbody.appendChild(tr);
  }

  // Total row -- sum of every fuel type actually shown above (whatever the
  // caller already excluded, e.g. interconnectors/NATGRID, stays excluded
  // here too since it's summing this same table's own rows).
  const totalRow = document.createElement("tr");
  totalRow.className = "total-row";
  totalRow.innerHTML = "<td>Total</td>" + periods.map((sp) => {
    const values = fuelTypes.map((ft) => byKey.get(`${sp}|${ft}`)).filter((v) => v != null);
    return td(values.length ? values.reduce((a, b) => a + b, 0) : null, digits);
  }).join("");
  tbody.appendChild(totalRow);
}

// How much a fuel type's FPN changed from the *previous* SP to this one
// (e.g. SP10 = 2500, SP11 = 2200 -> SP11's own delta is -300, this SP's
// value minus the one before it -- not the notebook's own forward-looking
// convention this was originally ported from).
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
      const previous = rows[i - 1];
      out.push({
        settlement_period: row.settlement_period,
        fuel_type,
        fpn_delta: previous ? row.fpn_spot_vol - previous.fpn_spot_vol : null,
      });
    });
  }
  return out;
}

// NATGRID is engine/fpn.py's own synthetic fuel-type row (National Grid's
// BM-adjacent trades, not a real generating unit) -- it has no MEL/MIL and
// no real generation to compare a plan against, so it's excluded from
// every one of these pivots. Interconnectors, unlike NATGRID, DO have real
// FUELINST generation and an adjusted FPN (just no MEL/MIL), so they're
// only excluded from the MEL/MIL-drop pivot specifically, not this one.
function excludeNatgrid(rows) {
  return rows.filter((r) => (r.fuel_type || "").toUpperCase() !== "NATGRID");
}

function excludeInterconnectorsAndNatgrid(rows) {
  return excludeNatgrid(rows).filter((r) => !(r.fuel_type || "").toUpperCase().startsWith("INT"));
}

// SPs as columns, one row per named metric -- same orientation as the
// fuel-type pivots above (renderFuelPivot), for consistency across every
// table on the page rather than this pair's own previous row-per-SP shape.
function renderMetricPivot(tableId, rows, columns, labels) {
  const table = document.getElementById(tableId);
  const periods = [...new Set(rows.map((r) => r.settlement_period))].sort((a, b) => a - b);
  const byPeriod = new Map(rows.map((r) => [r.settlement_period, r]));

  table.querySelector("thead tr").innerHTML = "<th>Metric</th>" + periods.map((sp) => `<th>SP${sp}</th>`).join("");
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  columns.forEach((col, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${labels[i]}</td>` + periods.map((sp) => td(byPeriod.get(sp)?.[col], 1)).join("");
    tbody.appendChild(tr);
  });
}

async function loadDashboard() {
  const resp = await fetch("/api/fpn/dashboard");
  if (!resp.ok) return;
  const data = await resp.json();
  const byFuelSp = data.by_fuel_sp || [];
  const decisionDriversSp = data.decision_drivers_sp || [];
  const marketGenSeries = data.market_gen_vs_adj_fpn_series || [];

  renderFuelPivot("tbl-fpn-by-fuel", byFuelSp, "fpn_spot_vol");
  renderFuelPivot("tbl-fpn-delta", computeFpnDelta(byFuelSp), "fpn_delta");
  renderFuelPivot("tbl-mel-mil-drop", excludeInterconnectorsAndNatgrid(byFuelSp), "mel_mil_drop");
  renderFuelPivot("tbl-market-gen-vs-adj-fpn", excludeNatgrid(byFuelSp), "market_gen_vs_adj_fpn", 0, true);

  // Row order is the user's own preference (AUC, Delta, NIV error, Dmd
  // risk, Dmd error, Unexp), not the real Zapdos "Delta table" (components/
  // tables/delta-table.js)'s own order. `delta` here is this engine's own
  // raw system-wide NIV figure (see engine/fpn.py's build_aggregated,
  // niv_sp_max * 2) -- confirmed against the FPN notebook's actual formula
  // (aggregated_fpn_mel_boalf['niv_error'] = ...['delta'] - ...['area_under_curve']):
  // niv_error is delta minus AUC, so this is the row that actually feeds it.
  renderMetricPivot(
    "tbl-decision-drivers", decisionDriversSp,
    ["auc", "delta", "niv_error", "dmd_risk", "dmd_error", "unexp"],
    ["AUC", "Delta", "NIV error", "Dmd risk", "Dmd error", "Unexp"],
  );
  renderMetricPivot(
    "tbl-decision", decisionDriversSp,
    ["niv_estimate", "auc", "wind_deviation", "other_gen_deviation", "dmd_error", "unexp"],
    ["NIV estimate", "AUC", "Wind dev.", "Other gen dev.", "Dmd error", "Unexp"],
  );

  const aggregatedWindow = (data.aggregated_window || []).slice().sort((a, b) => new Date(a.spot_time) - new Date(b.spot_time));
  updateChart(generationChart, GENERATION_SERIES, aggregatedWindow);
  updateChart(deltaChart, DELTA_SERIES, aggregatedWindow);

  updateDynamicFuelChart(marketGenChart, "market-gen-vs-adj-fpn-legend", marketGenSeries, "market_gen_vs_adj_fpn");

  const melValues = byFuelSp.map((r) => r.mel_spot_vol).filter((v) => v != null);
  setFigure("stat-avg-of-mells", melValues.length ? melValues.reduce((a, b) => a + b, 0) / melValues.length : null);

  const latestSp = Math.max(...byFuelSp.map((r) => r.settlement_period));
  const latestDropSum = byFuelSp.filter((r) => r.settlement_period === latestSp)
    .reduce((sum, r) => sum + (r.mel_mil_drop || 0), 0);
  setFigure("stat-sum-mel-mil-drop", byFuelSp.length ? latestDropSum : null);
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

  const downsideSum = rows.reduce((sum, r) => sum + (r.mel_mil_downside || 0), 0);
  setFigure("stat-sum-mel-mil-downside", rows.length ? downsideSum : null);
}

// Suffix rule per fuel type started from the real Zapdos app source's own
// three-way split (components/tables/generate-real-time-generation-table-html.js),
// then trimmed further by request: OTHER keeps just `_m`/`_d` (no `_r`),
// NPSHYD/OCGT drop `_r` (keep `_m`/`_d` only), and every interconnector is
// `_r`-only now (the 5 that source's own "anything else" rule would have
// given `_m`/`_d` too are cut back to match the other 5). Column ORDER is
// the user's own original request -- the main fuel types in this exact
// sequence, with every interconnector grouped at the tail end (not
// interspersed the way that source's own orderedArray does it). COAL
// excluded per earlier request -- not a real generating fuel type worth a
// column.
const GENERATION_TABLE_FUELS = [
  { ft: "NUCLEAR", suffixes: ["r"] },
  { ft: "WIND", suffixes: ["m", "d"] },
  { ft: "CCGT", suffixes: ["m", "d"] },
  { ft: "OTHER", suffixes: ["m", "d"] },
  { ft: "PS", suffixes: ["r"] },
  { ft: "BIOMASS", suffixes: ["m", "d"] },
  { ft: "NPSHYD", suffixes: ["m", "d"] },
  { ft: "OCGT", suffixes: ["m", "d"] },
];
const GENERATION_VALUE_KEYS = { r: "real_gen", m: "market_gen", d: "delta_gen" };
const EXCLUDED_GENERATION_FUELS = new Set(["OIL", "COAL"]);

function renderRealTimeGenerationTable(tableId, rows, maxRows = 24) {
  const table = document.getElementById(tableId);
  const interconnectors = [...new Set(rows.map((r) => r.fuel_type))].filter((ft) => ft.startsWith("INT")).sort();
  const columns = [
    ...GENERATION_TABLE_FUELS.flatMap(({ ft, suffixes }) => suffixes.map((suffix) => ({ ft, suffix }))),
    ...interconnectors.map((ft) => ({ ft, suffix: "r" })),
  ];

  const byKey = new Map();
  for (const r of rows) {
    if (EXCLUDED_GENERATION_FUELS.has(r.fuel_type)) continue;
    byKey.set(`${r.ts}|${r.fuel_type}`, r);
  }
  const timestamps = [...new Set(rows.map((r) => r.ts))].sort((a, b) => new Date(b) - new Date(a)).slice(0, maxRows);

  table.querySelector("thead tr").innerHTML = "<th>SP</th><th>Time</th>" +
    columns.map(({ ft, suffix }) => `<th class="rotate"><div><span>${ft}_${suffix}</span></div></th>`).join("");
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = "";
  for (const ts of timestamps) {
    const sp = columns.map(({ ft }) => byKey.get(`${ts}|${ft}`)).find(Boolean)?.settlement_period;
    const tr = document.createElement("tr");
    const cells = columns.map(({ ft, suffix }) => td(byKey.get(`${ts}|${ft}`)?.[GENERATION_VALUE_KEYS[suffix]])).join("");
    const spCell = `<td>${sp != null ? sp : "--"}</td>`;
    const timeCell = `<td>${new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</td>`;
    tr.innerHTML = spCell + timeCell + cells;
    tbody.appendChild(tr);
  }
}

async function loadGenerationByFuel() {
  const resp = await fetch("/api/fpn/generation-by-fuel?hours=1");
  if (!resp.ok) return;
  const data = await resp.json();
  const rows = data.generation_by_fuel || [];
  renderRealTimeGenerationTable("tbl-fuelinst-max", rows, 12);
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
