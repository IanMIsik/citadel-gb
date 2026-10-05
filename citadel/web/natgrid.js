// Natgrid: National Grid's balancing trades, period by period. One stacked
// bar per settlement period (x-axis), one segment per trade -- segment height
// is the trade's MW, its label and colour are its price. Buys (positive volume)
// stack UP from zero, cheapest nearest the axis; sells (negative volume, National
// Grid selling) stack DOWN from it, so a bar reads as "bought this much at these
// prices, sold that much at those". Prices can be negative too.
// Data: /api/natgrid ({date: {sp: [trade, ...]}}, each trade carrying side/volume_mw/price).

const POLL_MS = 60000;
const BG = "#0c0c0c";
// Price colour: one hue, pale (cheap) to deep (dear). Matches the legend ramp in natgrid.html.
const RAMP_LOW = { h: 203, s: 85, l: 80 };
const RAMP_HIGH = { h: 203, s: 70, l: 38 };

if (window.ChartDataLabels) Chart.register(window.ChartDataLabels);

const state = { data: {}, date: null, spMin: 1, spMax: 50, days: 2, check: null };
let chart = null;

const $ = (id) => document.getElementById(id);
const gbp = (v, dp = 0) => `${v < 0 ? "−" : ""}£${Math.abs(v).toFixed(dp)}`;
const mwText = (v) => `${v < 0 ? "−" : v > 0 ? "+" : ""}${Math.abs(Math.round(v)).toLocaleString()} MW`;

function spTime(sp) {
  const mins = (sp - 1) * 30;
  return `${String(Math.floor(mins / 60) % 24).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`;
}

function rampColour(price, pMin, pMax) {
  const t = pMax > pMin ? (price - pMin) / (pMax - pMin) : 0.5;
  const mix = (a, b) => a + (b - a) * t;
  const l = mix(RAMP_LOW.l, RAMP_HIGH.l);
  return { bg: `hsl(${mix(RAMP_LOW.h, RAMP_HIGH.h)}, ${mix(RAMP_LOW.s, RAMP_HIGH.s)}%, ${l}%)`, fg: l > 55 ? "#06121f" : "#ffffff" };
}

function periodsForDate() {
  const day = state.data[state.date] || {};
  return Object.keys(day).map(Number).filter((sp) => sp >= state.spMin && sp <= state.spMax).sort((a, b) => a - b)
    .map((sp) => ({ sp, trades: day[sp] }));
}

function renderDates() {
  const dates = Object.keys(state.data).sort();
  if (!dates.includes(state.date)) state.date = dates[dates.length - 1] || null;
  $("ng-dates").innerHTML = dates.map((d) => `
    <label class="form-check"><input type="radio" name="ng-date" value="${d}" ${d === state.date ? "checked" : ""} />
    ${new Date(d).toLocaleDateString("en-GB")}</label>`).join("");
  $("ng-dates").querySelectorAll("input").forEach((r) => r.addEventListener("change", () => { state.date = r.value; render(); }));
}

function renderSummary(periods, pMin, pMax) {
  const trades = periods.flatMap((p) => p.trades);
  const bought = trades.filter((t) => t.volume_mw > 0).reduce((s, t) => s + t.volume_mw, 0);
  const sold = trades.filter((t) => t.volume_mw < 0).reduce((s, t) => s + t.volume_mw, 0);
  const gross = bought - sold;
  const vwap = gross ? trades.reduce((s, t) => s + t.price * Math.abs(t.volume_mw), 0) / gross : null;
  const card = (label, value) => `<div class="bm-tile"><span class="label">${label}</span><span class="value">${value}</span></div>`;
  $("ng-summary").innerHTML = trades.length
    ? card("Trades", trades.length) + card("Periods", periods.length) + card("Bought", mwText(bought)) +
      card("Sold", sold ? mwText(sold) : "0 MW") + card("Net", mwText(bought + sold)) +
      card("Volume-weighted price", gbp(vwap)) + card("Price range", `${gbp(pMin)} – ${gbp(pMax)}`)
    : "";
  $("ng-pmin").textContent = trades.length ? gbp(pMin) : "--";
  $("ng-pmax").textContent = trades.length ? gbp(pMax) : "--";
}

// One dataset per trade "level" on each side: level k holds every period's k-th
// buy (or k-th sell), so Chart.js stacks buys up and sells down in dataset order.
function buildDatasets(periods, pMin, pMax, labelMinMw) {
  const sides = {
    buy: periods.map((p) => p.trades.filter((t) => t.volume_mw > 0)),
    sell: periods.map((p) => p.trades.filter((t) => t.volume_mw < 0)),
  };
  const datasets = [];
  for (const side of ["buy", "sell"]) {
    const levels = Math.max(0, ...sides[side].map((ts) => ts.length));
    for (let k = 0; k < levels; k++) {
      const tradeAt = sides[side].map((ts) => ts[k] || null);
      const colours = tradeAt.map((t) => (t ? rampColour(t.price, pMin, pMax) : null));
      datasets.push({
        label: `${side} ${k + 1}`,
        data: tradeAt.map((t) => (t ? t.volume_mw : null)),
        backgroundColor: colours.map((c) => (c ? c.bg : "transparent")),
        borderColor: BG, borderWidth: 1, borderSkipped: false,
        trades: tradeAt, textColours: colours.map((c) => (c ? c.fg : "#fff")),
        datalabels: {
          display: (ctx) => Math.abs(ctx.dataset.data[ctx.dataIndex]) >= labelMinMw,
          color: (ctx) => ctx.dataset.textColours[ctx.dataIndex],
          font: { size: 10, family: "Consolas, monospace" },
          formatter: (_v, ctx) => gbp(ctx.dataset.trades[ctx.dataIndex].price),
        },
      });
    }
  }
  return datasets;
}

function render() {
  const periods = periodsForDate();
  const trades = periods.flatMap((p) => p.trades);
  $("ng-empty").classList.toggle("hidden", trades.length > 0);
  const prices = trades.map((t) => t.price);
  const pMin = prices.length ? Math.min(...prices) : 0;
  const pMax = prices.length ? Math.max(...prices) : 0;
  renderSummary(periods, pMin, pMax);

  const biggest = Math.max(1, ...periods.map((p) => Math.max(
    p.trades.filter((t) => t.volume_mw > 0).reduce((s, t) => s + t.volume_mw, 0),
    -p.trades.filter((t) => t.volume_mw < 0).reduce((s, t) => s + t.volume_mw, 0))));
  const labelMinMw = Math.max(20, biggest * 0.04); // segments shorter than this are too thin to carry a label
  const datasets = buildDatasets(periods, pMin, pMax, labelMinMw);

  $("ng-title").textContent = state.date
    ? `National Grid trades — ${new Date(state.date).toLocaleDateString("en-GB")} (segment = one trade: height MW, label £/MWh; buys up, sells down)`
    : "National Grid trades";

  const labels = periods.map((p) => [`SP${p.sp}`, spTime(p.sp)]);
  if (chart) chart.destroy();
  chart = new Chart($("ng-chart"), {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "nearest", intersect: true },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { color: "#b9bbb3", font: { size: 10 }, maxRotation: 0, autoSkip: false },
             title: { display: true, text: "Settlement period", color: "#8a8f9a" } },
        y: { stacked: true,
             // The zero line is the buy/sell divide, so it is drawn brighter than the rest.
             grid: { color: (ctx) => (ctx.tick.value === 0 ? "#b9bbb3" : "rgba(138,143,154,0.2)"), lineWidth: (ctx) => (ctx.tick.value === 0 ? 2 : 1) },
             ticks: { color: "#b9bbb3", callback: (v) => `${v} MW` },
             title: { display: true, text: "Volume traded (+ buy / − sell)", color: "#8a8f9a" } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#171a21", borderColor: "#2a2e38", borderWidth: 1, titleColor: "#e6e8eb", bodyColor: "#b9bbb3", footerColor: "#8a8f9a",
          callbacks: {
            title: (items) => `SP${periods[items[0].dataIndex].sp} · ${spTime(periods[items[0].dataIndex].sp)}`,
            label: (item) => {
              const t = item.dataset.trades[item.dataIndex];
              const extras = [t.reason, t.so_flag ? `SO flag ${t.so_flag}` : null, t.source].filter(Boolean).join(", ");
              return `${t.volume_mw < 0 ? "Sell" : "Buy"} ${Math.abs(t.volume_mw)} MW at ${gbp(t.price, 2)}/MWh${extras ? ` (${extras})` : ""}`;
            },
            footer: (items) => {
              const ts = periods[items[0].dataIndex].trades;
              const bought = ts.filter((t) => t.volume_mw > 0).reduce((s, t) => s + t.volume_mw, 0);
              const sold = ts.filter((t) => t.volume_mw < 0).reduce((s, t) => s + t.volume_mw, 0);
              const gross = bought - sold;
              const avg = gross ? ts.reduce((s, t) => s + t.price * Math.abs(t.volume_mw), 0) / gross : 0;
              return `Period: bought ${Math.round(bought)} MW, sold ${Math.round(-sold)} MW, net ${mwText(bought + sold)}, ${ts.length} trades, average ${gbp(avg)}`;
            },
          },
        },
      },
    },
  });
}

// NESO's trade list and Elexon's DISBSAD describe the same actions, so their
// per-period volumes should agree; this says where they do not.
function renderCheck() {
  const el = $("ng-check");
  const c = state.check;
  if (!c) { el.innerHTML = ""; return; }
  const n = (k) => c.counts[k] || 0;
  const compared = n("match") + n("mismatch");
  const bad = c.mismatches.filter((m) => state.data[m.date]);
  el.className = `natgrid-check ${n("mismatch") ? "natgrid-check-warn" : ""}`;
  el.innerHTML = `Source check, NESO vs Elexon DISBSAD: <b>${n("match")} of ${compared}</b> periods agree` +
    (n("mismatch") ? `, <b>${n("mismatch")} differ</b>` : "") +
    (n("neso_only") ? ` · ${n("neso_only")} awaiting DISBSAD` : "") + (n("disbsad_only") ? ` · ${n("disbsad_only")} only in DISBSAD` : "") +
    (bad.length ? `<details><summary>Show the differing periods</summary><ul>${bad.map((m) =>
      `<li>${new Date(m.date).toLocaleDateString("en-GB")} SP${m.sp}: NESO ${m.neso_mw} MW, DISBSAD ${m.disbsad_mw} MW (${m.diff_mw > 0 ? "+" : ""}${m.diff_mw})</li>`).join("")}</ul></details>` : "");
}

async function load() {
  const status = $("connection-status");
  const days = state.days;
  try {
    const [resp, checkResp] = await Promise.all([fetch(`/api/natgrid?days=${days}`), fetch(`/api/natgrid/reconcile?days=${days}`)]);
    if (!resp.ok) throw new Error(resp.status);
    const data = (await resp.json()).data || {};
    const check = checkResp.ok ? await checkResp.json() : null;
    if (days !== state.days) return; // the history length changed while this was loading: a newer load owns the page
    state.data = data;
    state.check = check;
    status.textContent = "live"; status.className = "connected";
    renderDates();
    render();
    renderCheck();
  } catch (e) {
    status.textContent = "reconnecting…"; status.className = "disconnected";
  }
}

$("ng-days").addEventListener("change", (e) => { state.days = Number(e.target.value) || 2; load(); });
$("ng-sp-min").addEventListener("change", (e) => { state.spMin = Number(e.target.value) || 1; render(); });
$("ng-sp-max").addEventListener("change", (e) => { state.spMax = Number(e.target.value) || 50; render(); });

fetch("/api/health").then((r) => r.json()).then((data) => {
  if (data.environment_label === "prod") return;
  const badge = $("env-badge");
  badge.textContent = data.environment_label.toUpperCase();
  badge.classList.remove("hidden");
});

load();
setInterval(load, POLL_MS);
