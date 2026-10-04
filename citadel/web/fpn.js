const statusEl = document.getElementById("connection-status");

// "Generation Chart" -- named that on this page; the real reference app source
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
// plain lines) in the real reference app source (components/chartjs/delta_chart.js),
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

// Exact magnitude-band shading from the real reference app source
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

// Same band thresholds as tdShaded() above, but the real reference app source's
// own SEPARATE blue/red "delta-positive/negative" data-range names (see
// custom.css) instead of its green/orange "low/medium/high" ones -- used
// for a genuine delta/change metric like Mil Mel Drop, distinct from
// tdShaded()'s plain-magnitude tables (confirmed against real user usage;
// the source page that assigns these classes wasn't recoverable from the
// reference set, so this mapping is per direct user confirmation, not a
// verified source citation like tdShaded()'s own).
function tdDeltaShaded(value, digits = 0) {
  if (value == null) return td(value, digits);
  const v = Number(value);
  let range = null;
  if (v > 300) range = "delta-positive-high";
  else if (v > 200) range = "delta-positive-medium";
  else if (v > 50) range = "delta-positive-low";
  else if (v <= -301) range = "delta-negative-high";
  else if (v <= -201) range = "delta-negative-medium";
  else if (v <= -51) range = "delta-negative-low";
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

// Market_gen vs Adj_fpn -- port of the reference app's max_gen_vs_adj_fpn_chart.js:
// one line per fuel type of (market generation - adjusted FPN), so above
// zero is over-performance and below zero under-performance. Same design as
// that chart: its own per-fuel colour map (lowercase fuel names), 1.5px
// lines without points, legend on the right whose click toggles a fuel and
// rescales y to the fuels still visible, a name label (white, 11px) on
// points with |value| >= 200 MW, and the reference app's grey y ticks/gridlines.
const MARKET_GEN_COLORS = {
  biomass: "#96031A", ccgt: "#D90368", coal: "#daaffd", intew: "#588B8B",
  intfr: "#FAA916", intirl: "#57B8FF", intned: "#cccccc", intnem: "#F2E86D",
  npshyd: "#9c8eff", nuclear: "#ffa493", ps: "#fde17c", wind: "#248232",
  other: "#30d4ec", ocgt: "#beffbb", oil: "#eaba8a",
};
const MARKET_GEN_LABEL_MW = 200;
const MARKET_GEN_TICK = "rgb(185,185,185)";
const MARKET_GEN_GRID = "rgba(155,155,155,0.2)";

// Fuels the reference app has no colour for (e.g. other interconnectors, NATGRID):
// stable hash so a fuel keeps its colour between refreshes.
function marketGenColor(fuelType) {
  const key = String(fuelType).toLowerCase();
  if (MARKET_GEN_COLORS[key]) return MARKET_GEN_COLORS[key];
  let h = 0;
  for (const c of key) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360}, 60%, 62%)`;
}

const LONDON_PARTS = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/London", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

// SP number + HH:MM in London time (the market's clock) for one instant.
function londonSp(t) {
  const parts = Object.fromEntries(LONDON_PARTS.formatToParts(new Date(t)).map((p) => [p.type, p.value]));
  const mins = Number(parts.hour) * 60 + Number(parts.minute);
  return { sp: Math.floor(mins / 30) + 1, mins, clock: `${parts.hour}:${parts.minute}` };
}

function makeMarketGenChart(canvasId) {
  const chart = new Chart(document.getElementById(canvasId), {
    type: "line",
    plugins: window.ChartDataLabels ? [window.ChartDataLabels] : [],
    data: { labels: [], datasets: [] },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false },
      hover: { mode: "nearest", intersect: false },
      scales: {
        x: {
          ticks: { color: MARKET_GEN_TICK, autoSkip: false, maxRotation: 0, minRotation: 0, font: { size: 9 }, padding: 2 },
          grid: { color: MARKET_GEN_GRID },
          afterBuildTicks: (axis) => {
            const keep = axis.chart.$tickIndices;
            if (keep) axis.ticks = axis.ticks.filter((t) => keep.has(t.value));
          },
        },
        y: {
          grace: "5%",
          ticks: { color: MARKET_GEN_TICK, font: { size: 9 }, maxTicksLimit: 7 },
          grid: { color: MARKET_GEN_GRID, zeroLineColor: MARKET_GEN_GRID },
        },
      },
      plugins: {
        legend: {
          position: "right",
          labels: { color: MARKET_GEN_TICK, boxWidth: 10, font: { size: 9 } },
        },
        tooltip: {
          mode: "nearest", intersect: false,
          backgroundColor: "#171a21", borderColor: "#2a2e38", borderWidth: 1,
          titleColor: "#b9bbb3", bodyColor: "#b9bbb3",
          callbacks: { label: (c) => `${c.dataset.label}: ${Number(c.parsed.y).toFixed(1)} MW` },
        },
        datalabels: {
          color: "#fff",
          font: { size: 11 },
          align: "top",
          clip: false,
          // Name the fuel only where it is far from plan, and only once per
          // settlement-period boundary tick so 180 minute-points don't
          // smother the chart in text.
          display: (ctx) => {
            const keep = ctx.chart.$tickIndices;
            const v = ctx.dataset.data[ctx.dataIndex];
            return !!keep && keep.has(ctx.dataIndex) && v != null && Math.abs(v) >= MARKET_GEN_LABEL_MW;
          },
          formatter: (_v, ctx) => ctx.dataset.label,
        },
      },
    },
  });
  return chart;
}

function updateMarketGenChart(chart, rows, valueKey) {
  const spotTimes = [...new Set(rows.map((r) => r.spot_time))].sort((a, b) => new Date(a) - new Date(b));
  // NATGRID isn't a generating fuel type, so it is left off this chart.
  rows = rows.filter((r) => String(r.fuel_type).toUpperCase() !== "NATGRID");
  const fuelTypes = [...new Set(rows.map((r) => r.fuel_type))].filter(Boolean).sort();
  const byFuel = new Map(fuelTypes.map((ft) => [ft, new Map()]));
  for (const r of rows) if (r.fuel_type) byFuel.get(r.fuel_type).set(r.spot_time, r[valueKey]);

  // Keep which fuels the user has toggled off across the 30s refreshes.
  const hidden = new Set(chart.data.datasets.filter((_, i) => !chart.isDatasetVisible(i)).map((d) => d.label));

  const sps = spotTimes.map(londonSp);
  chart.data.labels = sps.map((p) => [`SP${p.sp}`, p.clock]);
  chart.data.datasets = fuelTypes.map((ft) => {
    const color = marketGenColor(ft);
    return {
      label: ft.toLowerCase(), data: spotTimes.map((t) => byFuel.get(ft).get(t) ?? null),
      borderColor: color, backgroundColor: color,
      pointRadius: 0, borderWidth: 1.5, tension: 0.15, spanGaps: true,
      hidden: hidden.has(ft.toLowerCase()),
    };
  });

  // Ticks only at a settlement-period start (minute-of-day on a half hour).
  const boundaries = [];
  sps.forEach((p, i) => { if (p.mins % 30 === 0 && (i === 0 || sps[i - 1].mins !== p.mins)) boundaries.push(i); });
  const width = chart.width || chart.canvas.clientWidth || 300;
  // the reference app puts the legend on the right; on a narrow screen its 19 entries
  // would eat the plot, so it drops underneath there.
  chart.options.plugins.legend.position = width < 700 ? "bottom" : "right";
  const plotWidth = width < 700 ? width : width * 0.85;
  const maxTicks = Math.max(2, Math.floor(plotWidth / 65));
  const step = Math.max(1, Math.ceil(boundaries.length / maxTicks));
  chart.$tickIndices = new Set(boundaries.filter((_, i) => i % step === 0));
  chart.update();
}

const generationChart = makeChart("generation-chart", GENERATION_SERIES);
const deltaChart = makeChart("delta-chart", DELTA_SERIES);
const marketGenChart = makeMarketGenChart("market-gen-vs-adj-fpn-chart");

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

// `shadeMode`: null/false (plain), "magnitude" (tdShaded's green/orange
// bands) or "delta" (tdDeltaShaded's blue/red bands) -- see those
// functions' own docstrings for which table each is confirmed/intended for.
function renderFuelPivot(tableId, rows, valueKey, digits = 0, shadeMode = null) {
  const table = document.getElementById(tableId);
  const cell = shadeMode === "delta" ? tdDeltaShaded : shadeMode === "magnitude" ? tdShaded : td;
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

// Cached for the decision table (renderDecisionColumn() below), which
// recalculates independently of these tables' own render cycle -- on every
// keystroke, not just on a dashboard refresh.
let latestByFuelSp = [];
let latestDecisionDriversSp = [];
let latestGenerationByFuel = [];

async function loadDashboard() {
  const resp = await fetch("/api/fpn/dashboard");
  if (!resp.ok) return;
  const data = await resp.json();
  const byFuelSp = data.by_fuel_sp || [];
  const decisionDriversSp = data.decision_drivers_sp || [];
  const marketGenSeries = data.market_gen_vs_adj_fpn_series || [];
  latestByFuelSp = byFuelSp;
  latestDecisionDriversSp = decisionDriversSp;

  renderFuelPivot("tbl-fpn-by-fuel", byFuelSp, "fpn_spot_vol");
  renderFuelPivot("tbl-fpn-delta", computeFpnDelta(byFuelSp), "fpn_delta");
  renderFuelPivot("tbl-mel-mil-drop", excludeInterconnectorsAndNatgrid(byFuelSp), "mel_mil_drop", 0, "delta");
  renderFuelPivot("tbl-market-gen-vs-adj-fpn", excludeNatgrid(byFuelSp), "market_gen_vs_adj_fpn", 0, "magnitude");

  // Row order is the user's own preference (AUC, Delta, NIV error, Dmd
  // risk, Dmd error, Unexp), not the real reference app "Delta table" (components/
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

  updateMarketGenChart(marketGenChart, marketGenSeries, "market_gen_vs_adj_fpn");

  const melValues = byFuelSp.map((r) => r.mel_spot_vol).filter((v) => v != null);
  setFigure("stat-avg-of-mells", melValues.length ? melValues.reduce((a, b) => a + b, 0) / melValues.length : null);

  const latestSp = Math.max(...byFuelSp.map((r) => r.settlement_period));
  const latestDropSum = byFuelSp.filter((r) => r.settlement_period === latestSp)
    .reduce((sum, r) => sum + (r.mel_mil_drop || 0), 0);
  setFigure("stat-sum-mel-mil-drop", byFuelSp.length ? latestDropSum : null);

  renderDecisionTable();
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

// Suffix rule per fuel type started from the real reference app source's own
// three-way split (components/tables/generate-real-time-generation-table-html.js),
// then trimmed further by request: OTHER keeps just `_m`/`_d` (no `_r`),
// NPSHYD/OCGT drop `_r` (keep `_m`/`_d` only), PS switched from `_r`-only
// to `_m`/`_d` (same reasoning as OTHER/NPSHYD/OCGT), and every
// interconnector is `_r`-only now (the 5 that source's own "anything else"
// rule would have given `_m`/`_d` too are cut back to match the other 5).
// Column ORDER is
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
  { ft: "PS", suffixes: ["m", "d"] },
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

  // wind5/wind15/wind30 -- how much WIND_m has moved over the trailing
  // 5/15/30 minutes, at the bottom-right of this table (also on the reference app'
  // own FPN accuracy page). Rows sit on FUELINST's 5-minute grid (see
  // `timestamps` above), so 5/15/30 minutes back is simply 1/3/6 rows back
  // from the latest (index 0, since `timestamps` sorts newest-first).
  const windMarketGenAt = (idx) => byKey.get(`${timestamps[idx]}|WIND`)?.market_gen;
  const windChangeOver = (rowsBack) => {
    const latest = windMarketGenAt(0);
    const prior = windMarketGenAt(rowsBack);
    return latest != null && prior != null ? latest - prior : null;
  };
  // Explicit "+" for a non-negative change -- same convention as the
  // pricing stack's own NIV figure (app.js's nivHtml()).
  const setWindDiff = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value == null ? "--" : `${value >= 0 ? "+" : ""}${value.toFixed(1)}`;
    el.setAttribute("data-sign", value == null ? "" : String(value));
  };
  setWindDiff("stat-wind5", windChangeOver(1));
  setWindDiff("stat-wind15", windChangeOver(3));
  setWindDiff("stat-wind30", windChangeOver(6));
}

async function loadGenerationByFuel() {
  const resp = await fetch("/api/fpn/generation-by-fuel?hours=1");
  if (!resp.ok) return;
  const data = await resp.json();
  const rows = data.generation_by_fuel || [];
  latestGenerationByFuel = rows;
  renderRealTimeGenerationTable("tbl-fuelinst-max", rows, 12);
  renderDecisionTable();
}

// ---------------------------------------------------------------------------
// Decision table -- ported from the real reference app source
// (components/tables/decision-table/{index,data-utils}.js, found in the
// Old World install; only CSS fragments of this page survived in this
// project's own reference/ folder). Three independent "what if" scenario
// columns, each its own settlement period plus six trader inputs,
// recalculated on every keystroke and every dashboard refresh. Inputs are
// session-only (per request) -- nothing here is persisted server-side.
// ---------------------------------------------------------------------------

const DECISION_COLUMNS = ["1", "2", "3"];

function decisionInputValue(id) {
  const el = document.getElementById(id);
  return el ? el.value : "";
}

function decisionInputNumber(id) {
  const v = decisionInputValue(id);
  return v === "" ? 0 : Number(v);
}

function latestByPeriod(rows) {
  if (!rows.length) return null;
  return rows.reduce((a, b) => (a.settlement_period > b.settlement_period ? a : b));
}

// FPN for a specific fuel type at a specific (trader-chosen) settlement
// period -- not necessarily the latest one, unlike the other lookups below.
function fpnForSpAndFuel(sp, fuelType) {
  const row = latestByFuelSp.find((r) => r.settlement_period === sp && r.fuel_type === fuelType);
  return row ? row.fpn_spot_vol : null;
}

// Latest live market_gen for a fuel type, off the same 5-minute grid the
// Real Time Generation table itself uses (renderRealTimeGenerationTable()).
function latestMarketGen(fuelType) {
  const rows = latestGenerationByFuel.filter((r) => r.fuel_type === fuelType);
  if (!rows.length) return null;
  const latest = rows.reduce((a, b) => (new Date(a.ts) > new Date(b.ts) ? a : b));
  return latest.market_gen;
}

// Sum of market_gen_vs_adj_fpn across every fuel type except WIND, for the
// most recently completed settlement period -- matches the real source's
// own generateOtherGen() exactly (it only ever excludes wind, nothing else).
function latestOtherGenDeviationTotal() {
  const latest = latestByPeriod(latestByFuelSp);
  if (!latest) return null;
  const values = latestByFuelSp
    .filter((r) => r.settlement_period === latest.settlement_period && r.fuel_type !== "WIND")
    .map((r) => r.market_gen_vs_adj_fpn)
    .filter((v) => v != null);
  return values.length ? values.reduce((a, b) => a + b, 0) : null;
}

function latestDriver(field) {
  const latest = latestByPeriod(latestDecisionDriversSp);
  return latest ? latest[field] : null;
}

function setDecisionText(id, value, digits = 0) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = value == null || Number.isNaN(value) ? "--" : fmt(value, digits);
  el.setAttribute("data-sign", value == null ? "" : String(value));
}

// One scenario column's full set of rows -- mirrors the real source's
// renderInputValues()/getSummation() exactly:
//   column total    = fcauc - wind_in - othergen_in + dmd_in + nonbm_in + unexp_in - rnp_vs_fpn
//   vs-now total     = -wind_vs_now - othergen_vs_now + dmd_derived + unexp_derived
//   CF NIV           = column total - vs-now total - unexp_derived
function renderDecisionColumn(col) {
  const spRaw = decisionInputValue(`sp-input-${col}`);
  const sp = spRaw === "" ? null : Number(spRaw);

  const auc = sp != null ? (latestDecisionDriversSp.find((r) => r.settlement_period === sp) || {}).auc : null;
  setDecisionText(`fcauc-${col}`, auc);

  // RNP vs FPN: the real source displays a pre-fetched Regional Nomination
  // Platform value here, no calculation. We have no RNP data source (per
  // direct confirmation -- that's a paid feed not yet integrated), so this
  // stays "N/A" and contributes 0 to the totals below until that's wired in.
  const rnpVsFpn = null;

  const windInput = decisionInputNumber(`wind-input-${col}`);
  const windFpn = sp != null ? fpnForSpAndFuel(sp, "WIND") : null;
  const windMarketGen = latestMarketGen("WIND");
  const windVsNow = windFpn != null && windMarketGen != null ? windFpn + windInput - windMarketGen : null;
  setDecisionText(`wind-text-${col}`, windVsNow);

  const otherGenInput = decisionInputNumber(`othergen-input-${col}`);
  const otherGenCurrent = latestOtherGenDeviationTotal();
  const otherGenVsNow = otherGenCurrent != null ? otherGenInput - otherGenCurrent : null;
  setDecisionText(`othergen-text-${col}`, otherGenVsNow);

  const dmdInput = decisionInputNumber(`dmd-input-${col}`);
  const dmdCurrent = latestDriver("dmd_error");
  const dmdDerived = dmdCurrent != null ? dmdInput - dmdCurrent : null;
  setDecisionText(`dmd-text-${col}`, dmdDerived);

  const unexpInput = decisionInputNumber(`unexp-input-${col}`);
  const unexpCurrent = latestDriver("unexp");
  const unexpDerived = unexpCurrent != null ? unexpInput - unexpCurrent : null;
  setDecisionText(`unexp-text-${col}`, unexpDerived);

  const nonBmInput = decisionInputNumber(`nonbm-input-${col}`);

  const columnTotal = (auc || 0) - windInput - otherGenInput + dmdInput + nonBmInput + unexpInput - (rnpVsFpn || 0);
  const vsNowTotal = -(windVsNow || 0) - (otherGenVsNow || 0) + (dmdDerived || 0) + (unexpDerived || 0);
  const cfNiv = columnTotal - vsNowTotal - (unexpDerived || 0);

  setDecisionText(`col-total-${col}`, columnTotal);
  setDecisionText(`col-vs-now-total-${col}`, vsNowTotal);
  setDecisionText(`col-cf-niv-${col}`, cfNiv);
}

function renderDecisionTable() {
  DECISION_COLUMNS.forEach(renderDecisionColumn);
}

document.querySelectorAll("#decision-input-table .decision-input").forEach((el) => {
  el.addEventListener("input", () => renderDecisionColumn(el.dataset.col));
});

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

function loadEnvBadge() {
  fetch("/api/health").then(r => r.json()).then(data => {
    if (data.environment_label === "prod") return;
    const badge = document.getElementById("env-badge");
    badge.textContent = data.environment_label.toUpperCase();
    badge.classList.remove("hidden");
  });
}

// Plant trip alerts -- a short WebAudio beep (no binary asset needed) plus
// an in-page toast, per request: no OS Notification permission, just
// something impossible to miss while this tab is open. See
// engine/fpn.py's detect_trips() for what counts as a trip (>50MW single-
// unit MIL/MEL drop, edge-triggered) and engine/fpn_runner.py's
// _remit_poll_loop for the REMIT match/revision/resolved follow-ups this
// same toast gets updated in place with.
function playTripAlarm() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.18, 0.36].forEach((delay, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "square";
      osc.frequency.value = i % 2 === 0 ? 880 : 660;
      gain.gain.setValueAtTime(0.15, ctx.currentTime + delay);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + delay + 0.15);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + delay);
      osc.stop(ctx.currentTime + delay + 0.15);
    });
  } catch (e) { /* WebAudio unavailable -- toast alone still shows */ }
}

const tripToasts = new Map(); // trip_id -> toast element

function tripToastContainer() {
  let el = document.getElementById("trip-toast-container");
  if (!el) {
    el = document.createElement("div");
    el.id = "trip-toast-container";
    document.body.appendChild(el);
  }
  return el;
}

function showTripToast(msg) {
  playTripAlarm();
  const el = document.createElement("div");
  el.className = "trip-toast";
  el.innerHTML =
    `<button class="trip-toast-close" aria-label="Dismiss">&times;</button>` +
    `<div class="trip-toast-title">Plant trip -- ${msg.bm_unit}</div>` +
    `<div class="trip-toast-body">${msg.fuel_type || "Unknown fuel"} &middot; dropped ${msg.drop_mw.toFixed(0)} MW</div>` +
    `<div class="trip-toast-status">Checking REMIT for expected return time…</div>` +
    `<a class="trip-toast-link" href="/trips" target="citadel-trips" rel="noopener">Open Plant Trips</a>`;
  el.querySelector(".trip-toast-close").addEventListener("click", () => el.remove());
  tripToastContainer().appendChild(el);
  tripToasts.set(msg.trip_id ?? `${msg.bm_unit}:${msg.detected_at}`, el);
  setTimeout(() => el.remove(), 60000);
}

function updateTripToast(tripId, text) {
  const el = tripToasts.get(tripId);
  if (!el) return;
  const status = el.querySelector(".trip-toast-status");
  if (status) status.textContent = text;
}

// "Trip going away" pop-up (green, softer rising chime): `partial` once the
// drop has halved from its peak, `full` once it's cleared. Fires once per
// stage per trip -- see engine/fpn.py's detect_trips().
function playRecoveryChime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.15].forEach((delay, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = i === 0 ? 523 : 784;
      gain.gain.setValueAtTime(0.12, ctx.currentTime + delay);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + delay + 0.25);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + delay);
      osc.stop(ctx.currentTime + delay + 0.25);
    });
  } catch (e) { /* toast alone still shows */ }
}

function showRecoveryToast(msg) {
  playRecoveryChime();
  const back = Math.max(0, Math.min(100, Math.round((1 - msg.drop_mw / msg.peak_mw) * 100)));
  const full = msg.kind === "full";
  const expected = msg.expected_back
    ? `<div class="trip-toast-status">REMIT expected back: ${new Date(msg.expected_back).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</div>` : "";
  const el = document.createElement("div");
  el.className = "trip-toast recovery";
  el.innerHTML =
    `<button class="trip-toast-close" aria-label="Dismiss">&times;</button>` +
    `<div class="trip-toast-title">${full ? "Back online" : "Coming back"} -- ${msg.bm_unit}</div>` +
    `<div class="trip-toast-body">${msg.fuel_type || "Unknown fuel"} &middot; ` +
    (full ? `trip cleared (peak drop was ${msg.peak_mw.toFixed(0)} MW)`
          : `drop ${msg.peak_mw.toFixed(0)} &rarr; ${msg.drop_mw.toFixed(0)} MW (${back}% recovered)`) +
    ` &middot; SP${msg.settlement_period}</div>` +
    `<div class="trip-toast-bar"><span style="width:${full ? 100 : back}%"></span></div>` + expected +
    `<a class="trip-toast-link" href="/trips" target="citadel-trips" rel="noopener">Open Plant Trips</a>`;
  el.querySelector(".trip-toast-close").addEventListener("click", () => el.remove());
  tripToastContainer().appendChild(el);
  setTimeout(() => el.remove(), 60000);
}

function connectTripsWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/trips`);
  ws.onclose = () => setTimeout(connectTripsWebSocket, 2000);
  ws.onerror = () => ws.close();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "trip") {
      showTripToast(msg);
    } else if (msg.type === "remit_match") {
      updateTripToast(msg.trip_id, "Matched to REMIT -- fetching expected return time…");
    } else if (msg.type === "remit_revision") {
      const when = msg.event_end_time ? new Date(msg.event_end_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "unknown";
      updateTripToast(msg.trip_id, `Revised: now expected back ~${when}.`);
    } else if (msg.type === "trip_recovery") {
      showRecoveryToast(msg);
    } else if (msg.type === "resolved") {
      updateTripToast(msg.trip_id, "Resolved -- unit is back per REMIT.");
    }
  };
  setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send("ping"); }, 30000);
}

loadEnvBadge();
loadAll();
connectWebSocket();
connectTripsWebSocket();
setInterval(loadAll, 30000);
