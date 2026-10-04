// BM Stack -- the bids and offers still available (untouched) for National
// Grid to call, per settlement period (data: GET /api/bm-stack, built by
// engine/bm_stack.py). Chart design follows the reference app's own BM Stack window
// (views/windows/bm-stack): one stacked bar chart per side, x = distinct
// prices, a bar segment per unit coloured by fuel type, and a thin running-
// sum line (GW) on a right-hand axis; SP / Vol Sign / FT controls on the
// right. Differences from the reference app: Chart.js 4, no 16-units-per-price cap (the
// number of stack "levels" is computed from the data), a by-fuel table and
// summary tiles, and a 30s poll instead of a socket push.

if (window.ChartDataLabels) Chart.register(window.ChartDataLabels);

// Same fuel -> colour map as all-plants-boalf.js (itself the reference app's palette
// extended to Elexon's full fuel set) -- the `backgroundColor` values.
const FUEL_COLORS = {
  CCGT: "#2c7a65", OCGT: "#5a7d1f", OIL: "#5c3a1e", COAL: "#7c9333", NUCLEAR: "#8a6d00",
  WIND: "#3075ba", PS: "#a55a76", NPSHYD: "#1f6b7c", BIOMASS: "#1a2c20", OTHER: "#4d4d4d",
  NATGRID: "rgb(83, 0, 171)", NO_FUEL: "#ae672c",
};
const FUEL_ORDER = ["BIOMASS", "CCGT", "PS", "COAL", "WIND", "NUCLEAR", "OTHER", "NPSHYD", "OCGT", "OIL", "NATGRID", "NO_FUEL"];
// the reference app labelled every segment >= 50 MW, which piles names on top of each
// other where bars are thin. A name is now drawn (vertically, inside its own
// segment) only when the segment is tall enough to hold it; every unit is
// still in the tooltip.
const MIN_LABEL_MW = 20;
const PX_PER_PRICE = 26;     // horizontal room per distinct price -- the chart scrolls sideways
// Price ticks: bold white monospace so digits are evenly spaced and easy to
// read when rotated (monospace keeps "1.00" / "100.00" the same glyph width).
const PRICE_TICK_FONT = { size: 12, weight: "bold", family: "Consolas, 'SF Mono', 'Courier New', monospace" };
const GW_LABEL_STEP = 0.5;   // running-sum label only each time it climbs this many GW
const POLL_MS = 30000;

// Units that don't want to be called bid/offer at +/-99,999 (or 8,000-9,998)
// are technically "available" but they dwarf the real stack and flatten the
// MW axis, so only bands priced within +/- this are drawn (adjustable).
const DEFAULT_MAX_PRICE = 1000;

const state = { rows: [], periods: [], current: null, spKey: null, side: "offer", fuels: null /* null = all */, maxPrice: DEFAULT_MAX_PRICE, chart: null };

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fuelColor = (ft) => FUEL_COLORS[ft] || "#6b6b6b";
const spKey = (p) => `${p.settlement_date}:${p.settlement_period}`;
const spTime = (sp) => {
  const m = (sp - 1) * 30;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${m % 60 === 0 ? "00" : "30"}`;
};

function sideRows(applyPriceCap = true) {
  return state.rows.filter((r) =>
    `${r.settlement_date}:${r.settlement_period}` === state.spKey &&
    r.side === state.side &&
    (!applyPriceCap || Math.abs(r.price) <= state.maxPrice) &&
    (state.fuels === null || state.fuels.has(r.fuel_type)));
}

function fuelsPresent() {
  const present = new Set(state.rows.map((r) => r.fuel_type));
  return [...FUEL_ORDER.filter((f) => present.has(f)), ...[...present].filter((f) => !FUEL_ORDER.includes(f)).sort()];
}

// ---- controls -------------------------------------------------------------

function renderControls() {
  const spBox = document.getElementById("bm-sp-selector");
  spBox.innerHTML = state.periods.map((p) => {
    const k = spKey(p);
    const tag = state.current && k === spKey(state.current) ? " (now)" : "";
    return `<label><input type="radio" name="bm-sp" value="${k}" ${k === state.spKey ? "checked" : ""} /> SP${p.settlement_period} ${spTime(p.settlement_period)}${tag}</label>`;
  }).join("");
  spBox.querySelectorAll("input").forEach((el) => el.addEventListener("change", () => { state.spKey = el.value; render(); }));

  const fuels = fuelsPresent();
  const fuelBox = document.getElementById("bm-fuel-selector");
  const allChecked = state.fuels === null;
  fuelBox.innerHTML =
    `<label><input type="checkbox" id="bm-fuel-all" ${allChecked ? "checked" : ""} /> (All)</label>` +
    fuels.map((f) => {
      const checked = allChecked || state.fuels.has(f);
      return `<label><input type="checkbox" class="bm-fuel-cb" value="${esc(f)}" ${checked ? "checked" : ""} />` +
        ` <span class="legend-box" style="background:${fuelColor(f)}"></span> ${esc(f.toLowerCase())}</label>`;
    }).join("");
  document.getElementById("bm-fuel-all").addEventListener("change", (e) => {
    state.fuels = e.target.checked ? null : new Set();
    renderControls(); render();
  });
  fuelBox.querySelectorAll(".bm-fuel-cb").forEach((el) => el.addEventListener("change", () => {
    const selected = new Set([...fuelBox.querySelectorAll(".bm-fuel-cb")].filter((c) => c.checked).map((c) => c.value));
    state.fuels = selected.size === fuels.length ? null : selected;
    renderControls(); render();
  }));

  const cap = document.getElementById("bm-max-price");
  cap.value = state.maxPrice;
  cap.onchange = () => {
    const v = Number(cap.value);
    state.maxPrice = Number.isFinite(v) && v > 0 ? v : DEFAULT_MAX_PRICE;
    render();
  };

  document.querySelectorAll('input[name="bm-side"]').forEach((el) => {
    el.checked = el.value === state.side;
    el.onchange = () => { state.side = el.value; render(); };
  });
}

// ---- chart ----------------------------------------------------------------

function buildChartData(rows) {
  // Offers run cheapest-first (ascending); bids descending (highest price =
  // cheapest for the system), as in the reference app.
  const prices = [...new Set(rows.map((r) => r.price))].sort((a, b) => (state.side === "offer" ? a - b : b - a));
  const idx = new Map(prices.map((p, i) => [p, i]));

  // One stacked "level" per extra unit at the same price & fuel.
  const byFuelPrice = new Map();
  rows.forEach((r) => {
    const k = `${r.fuel_type}|${r.price}|${r.bm_unit}`;
    const e = byFuelPrice.get(k) || { ...r, mw: 0 };
    e.mw += r.mw; // a unit with several pairs at the same price is one segment
    byFuelPrice.set(k, e);
  });
  const entries = [...byFuelPrice.values()];

  const datasets = [];
  fuelsPresent().filter((f) => entries.some((e) => e.fuel_type === f)).forEach((fuel) => {
    const atPrice = new Map();
    entries.filter((e) => e.fuel_type === fuel).sort((a, b) => b.mw - a.mw).forEach((e) => {
      const list = atPrice.get(e.price) || [];
      list.push(e);
      atPrice.set(e.price, list);
    });
    const levels = Math.max(...[...atPrice.values()].map((l) => l.length));
    for (let level = 0; level < levels; level++) {
      const data = new Array(prices.length).fill(0);
      const meta = new Array(prices.length).fill(null);
      atPrice.forEach((list, price) => {
        if (list[level]) { data[idx.get(price)] = list[level].mw; meta[idx.get(price)] = list[level]; }
      });
      datasets.push({
        type: "bar", label: fuel, data, meta, backgroundColor: fuelColor(fuel), borderWidth: 0,
        barPercentage: 1, categoryPercentage: 1, yAxisID: "y", stack: "mw", order: 1,
        datalabels: {
          color: "#fff", font: { size: 9, weight: "bold" }, anchor: "center", align: "center", rotation: -90, clamp: true,
          display: (ctx) => {
            const mw = ctx.dataset.data[ctx.dataIndex] || 0;
            const name = ctx.dataset.meta[ctx.dataIndex]?.bm_unit;
            if (mw < MIN_LABEL_MW || !name) return false;
            const y = ctx.chart.scales.y;
            const segmentPx = Math.abs(y.getPixelForValue(0) - y.getPixelForValue(mw));
            const c = ctx.chart.ctx;
            c.save(); c.font = "bold 9px sans-serif";
            const namePx = c.measureText(name).width + 4;
            c.restore();
            return segmentPx >= namePx; // vertical text must fit inside its own segment
          },
          formatter: (_v, ctx) => ctx.dataset.meta[ctx.dataIndex]?.bm_unit ?? "",
        },
      });
    }
  });

  // Running sum across the price axis (MW) -- labelled in GW, but only where
  // the figure actually changes at 0.1GW resolution, so the line isn't buried
  // in repeated labels.
  const perPrice = new Array(prices.length).fill(0);
  entries.forEach((e) => { perPrice[idx.get(e.price)] += e.mw; });
  let run = 0, lastShownGw = -Infinity;
  const cumulative = perPrice.map((v) => (run += v));
  const lineLabels = cumulative.map((v, i) => {
    const gw = v / 1000;
    const isLast = i === cumulative.length - 1;
    if (!isLast && gw - lastShownGw < GW_LABEL_STEP) return "";
    lastShownGw = gw;
    return gw.toFixed(1);
  });
  datasets.push({
    type: "line", label: "Running sum", data: cumulative, yAxisID: "y2", order: 0,
    borderColor: "#ffffff", borderWidth: 0.7, pointRadius: 0, fill: false, tension: 0,
    datalabels: {
      color: "#ff5353", font: { size: 12, weight: "bold" }, align: "top", anchor: "end", offset: 2,
      display: (ctx) => lineLabels[ctx.dataIndex] !== "",
      formatter: (_v, ctx) => lineLabels[ctx.dataIndex],
    },
  });
  return { prices, datasets, cumulative };
}

function renderChart(rows) {
  const { prices, datasets } = buildChartData(rows);
  const labels = prices.map((p) => `${p < 0 ? "-" : ""}£${Math.abs(p).toFixed(2)}`);
  if (state.chart) state.chart.destroy();
  // Give every price its own slot; never narrower than the visible area.
  const scroller = document.getElementById("bm-chart-scroll");
  document.getElementById("bm-chart-inner").style.width = `${Math.max(scroller.clientWidth - 2, prices.length * PX_PER_PRICE + 130)}px`;
  const muted = "#d6d6d6", grid = "#474847";
  state.chart = new Chart(document.getElementById("bm-stack-canvas"), {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 3 },
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { stacked: true, title: { display: true, text: "Price (£/MWh)", color: muted, font: { size: 12, weight: "bold" } }, ticks: { color: "#ffffff", autoSkip: false, maxRotation: 90, minRotation: 90, padding: 4, font: PRICE_TICK_FONT }, grid: { color: grid } },
        y: { stacked: true, beginAtZero: true, position: "left", title: { display: true, text: "Available MW", color: muted, font: { size: 12, weight: "bold" } }, ticks: { color: muted, font: { size: 11 } }, grid: { color: grid } },
        y2: { position: "right", beginAtZero: true, title: { display: true, text: "Running sum of available MW", color: muted, font: { size: 12, weight: "bold" } }, ticks: { color: muted, font: { size: 11 } }, grid: { drawOnChartArea: false } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          filter: (item) => item.dataset.type === "bar" ? (item.raw || 0) > 0 : true,
          callbacks: {
            label: (item) => {
              if (item.dataset.type === "line") return `Running sum: ${Math.round(item.raw)} MW`;
              const m = item.dataset.meta[item.dataIndex];
              if (!m) return "";
              const cap = m.capacity_mw != null ? `, ${Math.round(m.capacity_mw)} MW cap` : "";
              return `${m.fuel_type}: ${item.raw.toFixed(2)} MW  ${m.bm_unit} (${m.lead_party || "n/a"}${cap})`;
            },
            afterLabel: (item) => {
              const m = item.dataset.meta?.[item.dataIndex];
              return m ? `FPN ${m.fpn_mw} · MEL ${m.mel_mw} · accepted ${m.accepted_mw} MW` : "";
            },
          },
        },
      },
    },
  });
}

// ---- summary + table ------------------------------------------------------

function renderSummary(rows, hiddenMw) {
  const total = rows.reduce((s, r) => s + r.mw, 0);
  const units = new Set(rows.map((r) => r.bm_unit)).size;
  const prices = rows.map((r) => r.price);
  const tile = (label, value) => `<div class="bm-tile"><span class="label">${label}</span><span class="value">${value}</span></div>`;
  document.getElementById("bm-summary").innerHTML =
    tile("Available MW", Math.round(total).toLocaleString()) +
    tile("Units", units) +
    tile("Price range £/MWh", prices.length ? `${Math.min(...prices).toFixed(2)} – ${Math.max(...prices).toFixed(2)}` : "--") +
    tile(`Above £${state.maxPrice.toLocaleString()} (hidden)`, `${Math.round(hiddenMw).toLocaleString()} MW`) +
    tile("Side", state.side === "offer" ? "Offers (up)" : "Bids (down)");

  const byFuel = new Map();
  rows.forEach((r) => {
    const e = byFuel.get(r.fuel_type) || { units: new Set(), mw: 0, min: Infinity, max: -Infinity };
    e.units.add(r.bm_unit); e.mw += r.mw; e.min = Math.min(e.min, r.price); e.max = Math.max(e.max, r.price);
    byFuel.set(r.fuel_type, e);
  });
  const tbody = document.querySelector("#bm-fuel-table tbody");
  tbody.innerHTML = [...byFuel.entries()].sort((a, b) => b[1].mw - a[1].mw).map(([f, e]) =>
    `<tr><td><span class="legend-box" style="background:${fuelColor(f)}"></span> ${esc(f)}</td><td>${e.units.size}</td>` +
    `<td>${Math.round(e.mw).toLocaleString()}</td><td>${e.min.toFixed(2)}</td><td>${e.max.toFixed(2)}</td></tr>`).join("")
    || `<tr><td colspan="5" class="trips-hint">Nothing available for this selection.</td></tr>`;
}

function render() {
  const rows = sideRows();
  const hiddenMw = sideRows(false).reduce((s, r) => s + r.mw, 0) - rows.reduce((s, r) => s + r.mw, 0);
  const [sd, sp] = (state.spKey || ":").split(":");
  document.getElementById("bm-chart-title").textContent =
    `BM STACK for ${sd || "--"} SP${sp || "--"} (${sp ? spTime(Number(sp)) : "--"}) — ${state.side === "offer" ? "Offers (vol sign 1)" : "Bids (vol sign -1)"}`;
  renderSummary(rows, hiddenMw);
  renderChart(rows);
}

// ---- data -----------------------------------------------------------------

async function load() {
  const status = document.getElementById("connection-status");
  try {
    const data = await (await fetch("/api/bm-stack")).json();
    if (!data.enabled) {
      status.textContent = "BM Stack not enabled on this environment"; status.className = "disconnected";
      return;
    }
    state.rows = data.rows || [];
    state.periods = data.periods || [];
    state.current = data.current || null;
    const keys = state.periods.map(spKey);
    if (!keys.includes(state.spKey)) {
      state.spKey = state.current && keys.includes(spKey(state.current)) ? spKey(state.current) : keys[keys.length - 1] || null;
    }
    status.textContent = "live"; status.className = "connected";
    renderControls();
    render();
  } catch (e) {
    status.textContent = "reconnecting…"; status.className = "disconnected";
  }
}

function loadEnvBadge() {
  fetch("/api/health").then((r) => r.json()).then((data) => {
    if (data.environment_label === "prod") return;
    const badge = document.getElementById("env-badge");
    badge.textContent = data.environment_label.toUpperCase();
    badge.classList.remove("hidden");
  });
}

loadEnvBadge();
load();
setInterval(load, POLL_MS);
