// All Plants Exploded BOALF -- ported from Zapdos's own
// all-plants-exploded-boalf-scripts/ (config.js/renderers.js/data-utils.js):
// one stacked-area line per BM unit (Chart.js `fill: true` + `stacked:
// true`), coloured by fuel type, a settlement-period sidebar toggle list,
// and a custom fuel-type legend (the built-in Chart.js legend stays off,
// same as Zapdos's own `showLegend: false`). Fed by /api/all-plants-boalf
// (initial paint) + /ws/all-plants-boalf (live pushes) -- see
// engine/stack.py's exploded_boalf_by_unit() and engine/fpn.py's
// exploded_boalf_with_fuel_type() for where the data comes from.

// By request, coloured by Elexon's own full fuel-type set (engine/fpn.py's
// ELEXON_FUEL_TYPES -- whatever fuel_ref's own FT carries) rather than
// Zapdos's narrower 8-bucket palette. The 6 types Zapdos's own config.js
// already had a colour for keep those exact hues; everything else here is
// a new colour chosen to stay distinguishable against them, not a Zapdos
// citation. Interconnector fuel types are excluded entirely server-side
// (exploded_boalf_with_fuel_type() -- they aren't real balancing actions),
// so no INT* entry is needed here. NATGRID (synthetic DISBSAD rows) and
// NO_FUEL (a bmUnit fuel_ref has no row for at all) are this page's own
// synthetic buckets, not real Elexon fuel types.
const FUEL_COLORS = {
  CCGT: { color: "#9bccbf", backgroundColor: "#2c7a65" },
  OCGT: { color: "#c9dfa0", backgroundColor: "#5a7d1f" },
  OIL: { color: "#c9a68a", backgroundColor: "#5c3a1e" },
  COAL: { color: "#bdc999", backgroundColor: "#7c9333" },
  NUCLEAR: { color: "#f5d76e", backgroundColor: "#8a6d00" },
  WIND: { color: "#82acd5", backgroundColor: "#3075ba" },
  PS: { color: "#dbb2c1", backgroundColor: "#a55a76" },
  NPSHYD: { color: "#a3c9d8", backgroundColor: "#1f6b7c" },
  BIOMASS: { color: "#8ea896", backgroundColor: "#1a2c20" },
  OTHER: { color: "#c7c7c7", backgroundColor: "#4d4d4d" },
  NATGRID: { color: "#c99afc", backgroundColor: "rgb(83, 0, 171)" },
  NO_FUEL: { color: "#d6b395", backgroundColor: "#ae672c" },
};
// Z-stack order for the stacked area -- generation types first (large,
// roughly Zapdos's own relative ordering), then the two synthetic buckets
// last (smallest/rarest, by observation).
const FUEL_ORDER = [
  "PS", "NUCLEAR", "COAL", "OCGT", "OIL", "NPSHYD", "WIND", "CCGT", "BIOMASS", "OTHER",
  "NATGRID", "NO_FUEL",
];
// A plant only gets its name labelled on the chart if its own peak
// magnitude reaches this many MW -- see the datalabels plugin config
// below for why.
const LABEL_MIN_MW = 150;

// How many periods either side of "live" stay in the window -- by request,
// exactly the live period, the one before it, and the two after it (the
// same "gate closure already passed" convention engine/runner.py's own
// rolling-window comment documents for the pricing stack generally).
const PERIODS_BEFORE_LIVE = 1;
const PERIODS_AFTER_LIVE = 2;

if (window.ChartDataLabels) Chart.register(window.ChartDataLabels);

// One absolute, ever-increasing period index per (date, sp) pair, treating
// every calendar day as exactly 48 periods -- wrong on the two real
// DST-transition days a year (46/50 periods), rendered the same
// simplified way fundies.js's own SETTLEMENT_PERIODS comment already
// accepts for this app; good enough for "how many periods apart" maths.
function periodIndex(dateIso, sp) {
  const days = Math.floor(new Date(`${dateIso}T00:00:00Z`).getTime() / 86400000);
  return days * 48 + (sp - 1);
}

let liveSd = null;
let liveSp = null;
// key "date|sp" -> {sd, sp, rows}
const periodBuffer = new Map();
const visibleKeys = new Set();

function withinLiveWindow(sd, sp) {
  if (liveSd == null) return true; // live pointer not known yet -- accept, pruned once it arrives
  const idx = periodIndex(sd, sp);
  const liveIdx = periodIndex(liveSd, liveSp);
  return idx >= liveIdx - PERIODS_BEFORE_LIVE && idx <= liveIdx + PERIODS_AFTER_LIVE;
}

function pruneToLiveWindow() {
  for (const [key, period] of periodBuffer) {
    if (!withinLiveWindow(period.sd, period.sp)) {
      periodBuffer.delete(key);
      visibleKeys.delete(key);
    }
  }
}

function setLive(sd, sp) {
  if (sd == null || sp == null) return;
  liveSd = sd;
  liveSp = sp;
  pruneToLiveWindow();
}

function mergePeriod(sd, sp, rows) {
  if (!withinLiveWindow(sd, sp)) return; // outside the live +/- window -- by request, never shown at all
  const key = `${sd}|${sp}`;
  periodBuffer.set(key, { sd, sp, rows });
  visibleKeys.add(key);
}

function renderSpSelector() {
  const el = document.getElementById("sp-selector");
  const keys = Array.from(periodBuffer.keys()).sort((a, b) => {
    const pa = periodBuffer.get(a), pb = periodBuffer.get(b);
    return periodIndex(pa.sd, pa.sp) - periodIndex(pb.sd, pb.sp);
  });
  el.innerHTML = keys.map((key) => {
    const { sp } = periodBuffer.get(key);
    const label = sp === liveSp ? `SP ${sp} (live)` : `SP ${sp}`;
    return `<label><input type="checkbox" data-key="${key}" ${visibleKeys.has(key) ? "checked" : ""}/> ${label}</label>`;
  }).join("");
  el.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      const key = e.target.dataset.key;
      if (e.target.checked) visibleKeys.add(key); else visibleKeys.delete(key);
      renderChart();
    });
  });
}

function renderFuelLegend() {
  const el = document.getElementById("fuel-legend");
  el.innerHTML = FUEL_ORDER.map((ft) => {
    const c = FUEL_COLORS[ft];
    return `<li class="legend-item"><span class="legend-box" style="background:${c.backgroundColor};border:1px solid ${c.color};"></span>${ft.replace(/_/g, " ")}</li>`;
  }).join("");
}

const ctx = document.getElementById("all-plants-boalf-canvas");
const chart = new Chart(ctx, {
  type: "line",
  data: { datasets: [] },
  options: {
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "nearest", intersect: false },
    scales: {
      x: {
        type: "time",
        time: { unit: "minute" },
        // Chart.js's own "auto" tick source for a time scale doesn't
        // reliably land exactly one tick per 30 minutes at every data
        // density (it can add extra in-between ticks when there's room) --
        // forced here instead: ticks are set directly to the exact
        // half-hour boundaries within the visible range, so there is
        // always exactly one divider per settlement period, never more.
        // A UK settlement-period boundary is always at :00/:30 in UTC too
        // (London sits at a whole-hour UTC offset year-round, DST included),
        // so stepping from UTC epoch 0 in fixed 30-minute increments still
        // lines up correctly on both sides of a clock change.
        afterBuildTicks: (axis) => {
          const stepMs = 30 * 60 * 1000;
          const start = Math.ceil(axis.min / stepMs) * stepMs;
          const ticks = [];
          for (let t = start; t <= axis.max; t += stepMs) ticks.push({ value: t });
          axis.ticks = ticks;
        },
        // Labelled with the SP number itself (ported from Zapdos's own
        // tick callback), not the raw clock time -- these double as
        // visible settlement-period separators rather than plain time
        // gridlines.
        ticks: {
          color: "#b9bbb3", font: { size: 9 },
          callback: (value) => {
            const parts = new Intl.DateTimeFormat("en-GB", {
              timeZone: "Europe/London", hour: "2-digit", minute: "2-digit", hour12: false,
            }).formatToParts(new Date(value));
            const hh = Number(parts.find((p) => p.type === "hour").value);
            const mm = Number(parts.find((p) => p.type === "minute").value);
            return `SP ${hh * 2 + Math.floor(mm / 30) + 1}`;
          },
        },
        grid: { color: "#2a2e38" },
      },
      y: {
        stacked: true,
        ticks: { color: "#b9bbb3" },
        grid: { color: "#2a2e38" },
      },
    },
    plugins: {
      legend: { display: false },
      tooltip: { backgroundColor: "#171a21", borderColor: "#2a2e38", borderWidth: 1, titleColor: "#b9bbb3", bodyColor: "#b9bbb3" },
      // Plant NAME shown once at each dataset's own peak-magnitude point
      // (see renderChart()'s own `peakIndex`) instead of a numeric value
      // at every large point -- a label at every qualifying point would
      // repeat the same name dozens of times across one area. Gated by
      // LABEL_MIN_MW below (a small/minor plant's own peak is often only
      // a handful of MW -- with ~150+ datasets in the live window, every
      // one of them getting a label crowded the chart into illegible
      // overlapping text; only genuinely significant contributors are
      // named now).
      datalabels: {
        color: "#e6e8eb",
        font: { size: 9, weight: 700 },
        display: (context) => context.dataIndex === context.dataset.peakIndex
          && Math.abs(context.dataset.data[context.dataset.peakIndex].y) >= LABEL_MIN_MW,
        formatter: (value, context) => context.dataset.label.replace(/_$/, ""),
      },
    },
  },
});

function renderChart() {
  // One dataset per (bmUnit, sign) pair -- a unit whose volume crosses
  // zero across the window gets a "_"-suffixed twin dataset, exactly
  // Zapdos's own renderers.js trick, so a stacked area never has to
  // represent one line swinging from positive to negative.
  const series = new Map(); // key -> {fuel_bucket, points: Map(iso -> delta)}
  for (const key of visibleKeys) {
    const period = periodBuffer.get(key);
    if (!period) continue;
    for (const row of period.rows) {
      const seriesKey = row.delta >= 0 ? row.bmUnit : `${row.bmUnit}_`;
      if (!series.has(seriesKey)) series.set(seriesKey, { fuel_bucket: row.fuel_bucket, points: new Map() });
      series.get(seriesKey).points.set(row.spot_time, row.delta);
    }
  }

  const datasets = Array.from(series.entries())
    .sort((a, b) => FUEL_ORDER.indexOf(a[1].fuel_bucket) - FUEL_ORDER.indexOf(b[1].fuel_bucket))
    .map(([key, { fuel_bucket, points }]) => {
      const colors = FUEL_COLORS[fuel_bucket] || FUEL_COLORS.NO_FUEL;
      const data = Array.from(points.entries())
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([iso, delta]) => ({ x: iso, y: delta }));
      let peakIndex = 0;
      data.forEach((p, i) => { if (Math.abs(p.y) > Math.abs(data[peakIndex].y)) peakIndex = i; });
      return {
        label: key, data, fill: true, stack: "boalf", peakIndex,
        borderColor: colors.color, backgroundColor: colors.backgroundColor,
        pointRadius: 0, borderWidth: 1, spanGaps: false,
      };
    });

  chart.data.datasets = datasets;
  chart.update();
}

async function loadInitial() {
  const resp = await fetch("/api/all-plants-boalf");
  const data = await resp.json();
  setLive(data.live_settlement_date, data.live_settlement_period);
  const byPeriod = new Map(); // "date|sp" -> {sd, sp, rows}
  for (const row of data.rows || []) {
    const key = `${row.settlement_date}|${row.settlement_period}`;
    if (!byPeriod.has(key)) byPeriod.set(key, { sd: row.settlement_date, sp: row.settlement_period, rows: [] });
    byPeriod.get(key).rows.push(row);
  }
  for (const { sd, sp, rows } of byPeriod.values()) mergePeriod(sd, sp, rows);
  renderSpSelector();
  renderFuelLegend();
  renderChart();
}

function loadEnvBadge() {
  fetch("/api/health").then((r) => r.json()).then((data) => {
    if (data.environment_label === "prod") return;
    const badge = document.getElementById("env-badge");
    badge.textContent = data.environment_label.toUpperCase();
    badge.classList.remove("hidden");
  });
}

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/all-plants-boalf`);
  const status = document.getElementById("connection-status");
  ws.onopen = () => { status.textContent = "live"; status.className = "connected"; };
  ws.onclose = () => {
    status.textContent = "reconnecting…";
    status.className = "disconnected";
    setTimeout(connectWebSocket, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.settlement_period === undefined) return;
    setLive(msg.live_settlement_date, msg.live_settlement_period);
    mergePeriod(msg.settlement_date, msg.settlement_period, msg.rows || []);
    renderSpSelector();
    renderChart();
  };
  setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send("ping"); }, 30000);
}

loadEnvBadge();
loadInitial();
connectWebSocket();
