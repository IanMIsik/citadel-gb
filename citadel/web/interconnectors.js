// Interconnector Graphs -- one chart per interconnector, each showing the
// SCHEDULED commercial flow (ENTSO-E's 7 pairs + SEMO's 3, dotted) against
// the REAL metered flow (FUELHH, solid) for the same pair -- fed by
// /api/fundies/interconnectors (see ingest/entsoe_flows.py / ingest/semo.py
// / engine/fundies.py's interconnector_real_flows()). No direct the reference app
// precedent (the real app never built this as its own page); reuses this
// project's own established Chart.js `.graph-container` pattern.

const SP_LABELS = Array.from({ length: 48 }, (_, i) => {
  const totalMinutes = i * 30;
  const hh = String(Math.floor(totalMinutes / 60)).padStart(2, "0");
  const mm = totalMinutes % 60 === 0 ? "00" : "30";
  return `${hh}:${mm}`;
});

const CHARTS = [
  { canvas: "ic-eleclink", scheduledSource: "entsoe", scheduledKey: "eleclink_net", realKey: "real_eleclink_net" },
  { canvas: "ic-ifa", scheduledSource: "entsoe", scheduledKey: "uk_ifa_net", realKey: "real_ifa_net" },
  { canvas: "ic-ifa2", scheduledSource: "entsoe", scheduledKey: "uk_ifa2_net", realKey: "real_ifa2_net" },
  { canvas: "ic-nl", scheduledSource: "entsoe", scheduledKey: "uk_nl_net", realKey: "real_nl_net" },
  { canvas: "ic-be", scheduledSource: "entsoe", scheduledKey: "uk_be_net", realKey: "real_be_net" },
  { canvas: "ic-norway", scheduledSource: "entsoe", scheduledKey: "uk_norway_net", realKey: "real_norway_net" },
  { canvas: "ic-dk", scheduledSource: "entsoe", scheduledKey: "uk_dk_net", realKey: "real_dk_net" },
  { canvas: "ic-ew", scheduledSource: "semo", scheduledKey: "intew_net", realKey: "real_ew_net" },
  { canvas: "ic-moyle", scheduledSource: "semo", scheduledKey: "intmoyle_net", realKey: "real_moyle_net" },
  { canvas: "ic-grnl", scheduledSource: "semo", scheduledKey: "intgrnl_net", realKey: "real_grnl_net" },
];

function makeLineChart(canvasId, tickFontSize = 10) {
  const ctx = document.getElementById(canvasId);
  return new Chart(ctx, {
    type: "line",
    data: {
      labels: SP_LABELS,
      datasets: [
        // Thicker than the other Chart.js pages here (1.5 -> 2) -- at this
        // grid's small per-chart size, thin dashed/solid lines sitting near
        // each other were hard to tell apart; a heavier stroke reads better
        // without needing a bigger box.
        { label: "scheduled", data: [], borderColor: "#f5a742", backgroundColor: "#f5a742", pointRadius: 0, pointHoverRadius: 3, borderWidth: 2, tension: 0.15, borderDash: [5, 5] },
        { label: "real", data: [], borderColor: "#72c4f6", backgroundColor: "#72c4f6", pointRadius: 0, pointHoverRadius: 3, borderWidth: 2, tension: 0.15 },
      ],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        // Vertical gridlines dropped entirely -- with 48 half-hour points
        // squeezed into a ~200px-tall box, the full grid of both axes read
        // as visual noise on top of the two lines. The x-axis ticks
        // themselves (still shown, just without a line per tick) are
        // enough to read time off the chart.
        x: { ticks: { color: "#b9bbb3", autoSkip: true, maxTicksLimit: 6, font: { size: tickFontSize } }, grid: { display: false } },
        // The zero line (where scheduled/real flow direction flips) drawn
        // brighter than the rest of the y gridlines -- these charts sit
        // near zero often, so that crossing is the single most useful
        // reference line on the whole chart.
        y: {
          ticks: { color: "#b9bbb3", font: { size: tickFontSize } },
          grid: { color: (ctx) => (ctx.tick && ctx.tick.value === 0 ? "#4a4f5c" : "#2a2e38") },
        },
      },
      plugins: {
        legend: { labels: { color: "#b9bbb3", font: { size: tickFontSize }, boxWidth: 12 } },
        tooltip: { backgroundColor: "#171a21", borderColor: "#2a2e38", borderWidth: 1, titleColor: "#b9bbb3", bodyColor: "#b9bbb3" },
      },
    },
  });
}

function seriesFromRows(rows, key) {
  const byPeriod = new Map(rows.map((r) => [r.settlement_period, r]));
  return Array.from({ length: 48 }, (_, i) => {
    const row = byPeriod.get(i + 1);
    const v = row ? row[key] : null;
    return v === null || v === undefined ? null : Number(v);
  });
}

const charts = CHARTS.map((c) => ({ ...c, chart: makeLineChart(c.canvas) }));
const aggregateChart = makeLineChart("ic-aggregate");

// Real (metered) aggregate -- sum of every interconnector's own FUELHH
// flow plus natgrid, computed client-side from fundies_real_time's own
// per-pair columns (already fetched as `real` below) -- the counterpart
// to interconnector_ng, which is the SCHEDULED equivalent already summed
// server-side (see engine/fundies.py's build_derived_rows()).
const REAL_SUM_KEYS = [...CHARTS.map((c) => c.realKey), "ng_vol"];
function realAggregateSeries(realRows) {
  const byPeriod = new Map(realRows.map((r) => [r.settlement_period, r]));
  return Array.from({ length: 48 }, (_, i) => {
    const row = byPeriod.get(i + 1);
    if (!row) return null;
    let sum = 0;
    let any = false;
    for (const key of REAL_SUM_KEYS) {
      const v = row[key];
      if (v !== null && v !== undefined) { sum += Number(v); any = true; }
    }
    return any ? sum : null;
  });
}

async function loadAll() {
  const [icResp, daResp] = await Promise.all([
    fetch("/api/fundies/interconnectors"), fetch("/api/fundies/day-ahead"),
  ]);
  const data = await icResp.json();
  const da = (await daResp.json()).rows || [];
  const bySource = { entsoe: data.entsoe || [], semo: data.semo || [] };
  const real = data.real || [];
  charts.forEach(({ chart, scheduledSource, scheduledKey, realKey }) => {
    chart.data.datasets[0].data = seriesFromRows(bySource[scheduledSource], scheduledKey);
    chart.data.datasets[1].data = seriesFromRows(real, realKey);
    chart.update();
  });

  aggregateChart.data.datasets[0].data = seriesFromRows(da, "interconnector_ng");
  aggregateChart.data.datasets[1].data = realAggregateSeries(real);
  aggregateChart.update();
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
loadAll();
setInterval(loadAll, 30000);
