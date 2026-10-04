// Fundies Graphs -- demand/wind/solar forecast-vs-outturn charts. No
// direct the reference app precedent was found for this specific page (the real app
// only had the fundies TABLE); built in this project's own established
// Chart.js `.graph-container` style instead (see fpn.js's makeChart()).

const SP_LABELS = Array.from({ length: 48 }, (_, i) => {
  const totalMinutes = i * 30;
  const hh = String(Math.floor(totalMinutes / 60)).padStart(2, "0");
  const mm = totalMinutes % 60 === 0 ? "00" : "30";
  return `${hh}:${mm}`;
});

function makeLineChart(canvasId, series) {
  const ctx = document.getElementById(canvasId);
  return new Chart(ctx, {
    type: "line",
    data: {
      labels: SP_LABELS,
      datasets: series.map((s) => ({
        label: s.label, data: [], borderColor: s.color, backgroundColor: s.color,
        pointRadius: 0, borderWidth: 1.5, tension: 0.15,
        borderDash: s.dash ? [5, 5] : undefined,
      })),
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: { ticks: { color: "#b9bbb3", autoSkip: true, maxTicksLimit: 12, font: { size: 9 } }, grid: { color: "#2a2e38" } },
        y: { ticks: { color: "#b9bbb3" }, grid: { color: "#2a2e38" } },
      },
      plugins: {
        legend: { labels: { color: "#b9bbb3", font: { size: 10 } } },
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

// Display-only smoothing for "actual + curtailed" -- ISPSTACK's accepted
// wind bids genuinely swing hard period-to-period (confirmed directly
// against Elexon's own API: e.g. one morning had 0 accepted wind-unit bid
// rows in SP13, then 379 in SP14 -- a real, correlated on/off transmission-
// constraint event, not a fetch or aggregation bug; total_wind_outturn's
// own sum-of-accepted-bid-volumes math matches the notebook's literal
// approach exactly, see engine/fundies.py's wind_outturn_with_curtailment).
// A short centered rolling mean here turns that genuine but jagged
// period-by-period figure into a readable trend line for the CHART only --
// the Fundies table still shows every period's raw, unsmoothed number.
function smoothSeries(values, window = 3) {
  const half = Math.floor(window / 2);
  return values.map((_, i) => {
    const slice = values.slice(Math.max(0, i - half), Math.min(values.length, i + half + 1))
      .filter((v) => typeof v === "number" && !Number.isNaN(v));
    if (!slice.length) return values[i];
    return slice.reduce((sum, v) => sum + v, 0) / slice.length;
  });
}

// Forecasts are always dotted, outturn/actuals are always solid -- per
// explicit request, applied consistently across every series here (not
// just the day-ahead ones): latest_ndf/latest_winfor ARE forecasts too
// (a closer-to-real-time one, but still a forecast, not metered outturn),
// so they're dashed the same as their day-ahead counterparts.
const demandChart = makeLineChart("demand-chart", [
  { label: "latest_ndf (forecast)", color: "#72c4f6", dash: true },
  { label: "da_ndf (forecast)", color: "#f5a742", dash: true },
  { label: "indo (outturn)", color: "#58b06a" },
]);
// Actual generation vs actual+curtailed (i.e. what would have generated
// without curtailment) -- plotting the SUM rather than "curtailed" as its
// own standalone line, since a bare "curtailed" line reads like a third
// generation source rather than what it is; the gap between these two
// lines is the curtailment.
const windChart = makeLineChart("wind-chart", [
  { label: "latest_winfor (forecast)", color: "#72c4f6", dash: true },
  { label: "da_winfor (forecast)", color: "#f5a742", dash: true },
  { label: "actual generation", color: "#58b06a" },
  { label: "actual + curtailed (smoothed)", color: "#e0645a" },
]);
const solarChart = makeLineChart("solar-chart", [
  { label: "pv_live (outturn)", color: "#58b06a" },
  { label: "embedded_solar_forecast", color: "#f5a742", dash: true },
]);

async function loadAll() {
  const [rtResp, daResp] = await Promise.all([
    fetch("/api/fundies/real-time"), fetch("/api/fundies/day-ahead"),
  ]);
  const rt = (await rtResp.json()).rows || [];
  const da = (await daResp.json()).rows || [];

  demandChart.data.datasets[0].data = seriesFromRows(rt, "latest_ndf");
  demandChart.data.datasets[1].data = seriesFromRows(da, "da_ndf");
  demandChart.data.datasets[2].data = seriesFromRows(rt, "indo");
  demandChart.update();

  windChart.data.datasets[0].data = seriesFromRows(rt, "latestwindfor");
  windChart.data.datasets[1].data = seriesFromRows(da, "da_windfor");
  windChart.data.datasets[2].data = seriesFromRows(rt, "wind_ot");
  windChart.data.datasets[3].data = smoothSeries(seriesFromRows(rt, "total_wind_outturn"));
  windChart.update();

  solarChart.data.datasets[0].data = seriesFromRows(rt, "pv_live");
  solarChart.data.datasets[1].data = seriesFromRows(da, "embedded_solar_forecast");
  solarChart.update();
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
