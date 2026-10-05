// All Plants Exploded BOALF -- ported from the reference app's own
// all-plants-exploded-boalf-scripts/ (config.js/renderers.js/data-utils.js):
// one stacked-area line per BM unit (Chart.js `fill: true` + `stacked:
// true`), coloured by fuel type, a settlement-period sidebar toggle list,
// and a custom fuel-type legend (the built-in Chart.js legend stays off,
// same as the reference app's own `showLegend: false`). Fed by /api/all-plants-boalf
// (initial paint) + /ws/all-plants-boalf (live pushes) -- see
// engine/stack.py's exploded_boalf_by_unit() and engine/fpn.py's
// exploded_boalf_with_fuel_type() for where the data comes from.

// By request, coloured by Elexon's own full fuel-type set (engine/fpn.py's
// ELEXON_FUEL_TYPES -- whatever fuel_ref's own FT carries) rather than
// the reference app's narrower 8-bucket palette. The 6 types the reference app's own config.js
// already had a colour for keep those exact hues; everything else here is
// a new colour chosen to stay distinguishable against them, not a the reference app
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
// roughly the reference app's own relative ordering), then the two synthetic buckets
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

// Which periods are shown is the USER's choice and must survive every live push. Two
// pieces of state, never touched by incoming data:
//   hiddenKeys  periods the user unticked (a period arriving for the first time is shown
//               unless it is in here);
//   focusKey    when set, ONLY that period is shown -- new periods arriving on the websocket
//               stay hidden. followLive makes the focus track the live period as it advances.
// (This used to re-add every period to the visible set on every update, so a filter to one
// SP was undone by the next push.)
const hiddenKeys = new Set();
let focusKey = null;
let followLive = false;
const isVisible = (key) => (focusKey !== null ? key === focusKey : !hiddenKeys.has(key));
const liveKey = () => (liveSd == null ? null : `${liveSd}|${liveSp}`);

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
      hiddenKeys.delete(key);
      if (focusKey === key) focusKey = null;
    }
  }
}

function setLive(sd, sp) {
  if (sd == null || sp == null) return;
  liveSd = sd;
  liveSp = sp;
  pruneToLiveWindow();
  if (followLive) focusKey = liveKey();
}

function mergePeriod(sd, sp, rows) {
  if (!withinLiveWindow(sd, sp)) return; // outside the live +/- window -- by request, never shown at all
  periodBuffer.set(`${sd}|${sp}`, { sd, sp, rows });
}

function renderSpSelector() {
  const el = document.getElementById("sp-selector");
  const keys = Array.from(periodBuffer.keys()).sort((a, b) => {
    const pa = periodBuffer.get(a), pb = periodBuffer.get(b);
    return periodIndex(pa.sd, pa.sp) - periodIndex(pb.sd, pb.sp);
  });
  el.innerHTML = keys.map((key) => {
    const { sp } = periodBuffer.get(key);
    const label = key === liveKey() ? `SP ${sp} (live)` : `SP ${sp}`;
    return `<div class="sp-row${focusKey === key ? " sp-focused" : ""}">` +
      `<label><input type="checkbox" data-key="${key}" ${isVisible(key) ? "checked" : ""}/> ${label}</label>` +
      `<button type="button" class="sp-only" data-key="${key}" title="Show only this settlement period">only</button></div>`;
  }).join("");

  el.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      const key = e.target.dataset.key;
      if (focusKey !== null) {
        // Leaving focus mode: everything except the focused period becomes explicitly hidden,
        // then this tick applies on top of that.
        for (const k of periodBuffer.keys()) if (k !== focusKey) hiddenKeys.add(k);
        focusKey = null;
        followLive = false;
      }
      if (e.target.checked) hiddenKeys.delete(key); else hiddenKeys.add(key);
      refreshView();
    });
  });
  el.querySelectorAll("button.sp-only").forEach((btn) => {
    btn.addEventListener("click", () => { followLive = false; focusKey = btn.dataset.key; refreshView(); });
  });
  document.getElementById("sp-follow-live").classList.toggle("active", followLive);
}

function refreshView() {
  renderSpSelector();
  renderChart();
}

// Every recompute pushes one message per settlement period (about six), back to back. Each
// redraw rebuilds 150+ datasets, so redrawing per message made the page lag behind the data;
// the messages are merged into the buffer as they arrive and drawn once, a moment after the
// last of the burst.
let redrawTimer = null;
function scheduleRefresh() {
  if (redrawTimer !== null) return;
  redrawTimer = setTimeout(() => { redrawTimer = null; refreshView(); }, 60);
}

document.getElementById("sp-show-all").addEventListener("click", () => {
  focusKey = null; followLive = false; hiddenKeys.clear(); refreshView();
});
document.getElementById("sp-follow-live").addEventListener("click", () => {
  followLive = !followLive;
  focusKey = followLive ? liveKey() : null;
  refreshView();
});

function renderFuelLegend() {
  const el = document.getElementById("fuel-legend");
  el.innerHTML = FUEL_ORDER.map((ft) => {
    const c = FUEL_COLORS[ft];
    return `<li class="legend-item"><span class="legend-box" style="background:${c.backgroundColor};border:1px solid ${c.color};"></span>${ft.replace(/_/g, " ")}</li>`;
  }).join("");
}

const SP_MS = 30 * 60 * 1000;
const LONDON_FMT = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/London", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});

// Settlement date, period number and clock time of an instant, on the market (London) clock.
function londonSp(ms) {
  const p = Object.fromEntries(LONDON_FMT.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  const hh = Number(p.hour) % 24, mm = Number(p.minute);
  return { date: `${p.year}-${p.month}-${p.day}`, sp: hh * 2 + Math.floor(mm / 30) + 1, clock: `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}` };
}

// Makes every settlement period unmistakable: alternating shaded bands, a firm line at each
// period boundary, the live period tinted, and the period named at the top of its band.
const spBands = {
  id: "spBands",
  beforeDatasetsDraw(c) {
    const { ctx: g, chartArea: area, scales } = c;
    const x = scales.x;
    if (!area || !x) return;
    g.save();
    for (let t = Math.floor(x.min / SP_MS) * SP_MS; t < x.max; t += SP_MS) {
      const x0 = Math.max(x.getPixelForValue(t), area.left);
      const x1 = Math.min(x.getPixelForValue(t + SP_MS), area.right);
      if (x1 <= x0) continue;
      const info = londonSp(t);
      const live = liveSd != null && info.date === liveSd && info.sp === liveSp;
      g.fillStyle = live ? "rgba(114,196,246,0.10)" : info.sp % 2 ? "rgba(255,255,255,0.05)" : "rgba(255,255,255,0)";
      g.fillRect(x0, area.top, x1 - x0, area.height);
      if (x0 > area.left + 1) {
        g.strokeStyle = "rgba(185,187,179,0.6)"; g.lineWidth = 1;
        g.beginPath(); g.moveTo(x0, area.top); g.lineTo(x0, area.bottom); g.stroke();
      }
    }
    g.restore();
  },
  afterDatasetsDraw(c) {
    const { ctx: g, chartArea: area, scales } = c;
    const x = scales.x;
    if (!area || !x) return;
    g.save();
    g.textAlign = "center"; g.textBaseline = "top";
    for (let t = Math.floor(x.min / SP_MS) * SP_MS; t < x.max; t += SP_MS) {
      const x0 = Math.max(x.getPixelForValue(t), area.left);
      const x1 = Math.min(x.getPixelForValue(t + SP_MS), area.right);
      if (x1 - x0 < 44) continue; // too narrow to name
      const info = londonSp(t);
      const live = liveSd != null && info.date === liveSd && info.sp === liveSp;
      g.font = "bold 11px Consolas, monospace";
      g.fillStyle = live ? "#72c4f6" : "#e6e8eb";
      g.fillText(`SP ${info.sp}${live ? " LIVE" : ""}`, (x0 + x1) / 2, area.top + 4);
    }
    g.restore();
  },
};

const ctx = document.getElementById("all-plants-boalf-canvas");
const chart = new Chart(ctx, {
  type: "line",
  plugins: [spBands],
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
          // One tick per period boundary -- or every 5 minutes when zoomed to a single period.
          const stepMs = axis.max - axis.min <= 31 * 60 * 1000 ? 5 * 60 * 1000 : 30 * 60 * 1000;
          const start = Math.ceil(axis.min / stepMs) * stepMs;
          const ticks = [];
          for (let t = start; t <= axis.max; t += stepMs) ticks.push({ value: t });
          axis.ticks = ticks;
        },
        // Labelled with the SP number itself (ported from the reference app's own
        // tick callback), not the raw clock time -- these double as
        // visible settlement-period separators rather than plain time
        // gridlines.
        ticks: {
          color: "#b9bbb3", font: { size: 9 },
          // The period names are drawn inside each band (spBands); the axis carries the clock time.
          callback: (value) => londonSp(value).clock,
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
  // the reference app's own renderers.js trick, so a stacked area never has to
  // represent one line swinging from positive to negative.
  const series = new Map(); // key -> {fuel_bucket, points: Map(iso -> delta)}
  for (const key of periodBuffer.keys()) {
    if (!isVisible(key)) continue;
    const period = periodBuffer.get(key);
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
  // Focused on one period: pin the axis to exactly that half hour (even if its data is partial).
  const focusPeriod = focusKey !== null ? periodBuffer.get(focusKey) : null;
  const firstMs = focusPeriod && focusPeriod.rows.length
    ? Math.min(...focusPeriod.rows.map((r) => Date.parse(r.spot_time))) : null;
  chart.options.scales.x.min = firstMs !== null ? Math.floor(firstMs / SP_MS) * SP_MS : undefined;
  chart.options.scales.x.max = firstMs !== null ? Math.floor(firstMs / SP_MS) * SP_MS + SP_MS : undefined;
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
  renderFuelLegend();
  refreshView();
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
    scheduleRefresh();
  };
  setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send("ping"); }, 30000);
}

loadEnvBadge();
loadInitial();
connectWebSocket();
