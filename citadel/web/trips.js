// Plant Trips page -- durable record of every trip engine/fpn.py's
// detect_trips() has ever fired, cross-checked against Elexon's own REMIT
// outage messages (ingest/remit.py, matched+polled by engine/fpn_runner.py's
// _remit_poll_loop). Fed by /api/trips/recent (+ /api/trips/{id}/revisions
// on click) for the initial paint, and /ws/trips for live updates -- same
// reconnecting-websocket idiom as every other page here (see
// all-plants-boalf.js's own connectWebSocket()).

function fmtTime(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtHours(h) {
  if (h === null || h === undefined) return "--";
  const n = Number(h);
  if (Number.isNaN(n)) return "--";
  return `${n >= 0 ? "+" : ""}${n.toFixed(1)}h`;
}

function fmtSpRange(startDate, startSp, endDate, endSp) {
  if (startSp == null) return "--";
  const startLabel = `SP${startSp}`;
  if (endSp == null) return `${startLabel} → ongoing`;
  const crossedDay = endDate && startDate && endDate !== startDate;
  const endLabel = crossedDay ? `SP${endSp} (+1d)` : `SP${endSp}`;
  if (!crossedDay && endSp === startSp) return startLabel;
  return `${startLabel} → ${endLabel}`;
}

// REMIT publication can lag a real trip by a few minutes, so a trip is only called "unpublished" once it is
// older than this and still has no match.
const REMIT_LAG_MS = 30 * 60 * 1000;

function noRemitNotice(trip) {
  return trip.status !== "resolved" && !trip.remit_mrid && Date.now() - Date.parse(trip.detected_at) > REMIT_LAG_MS;
}

function statusLabel(trip) {
  if (trip.status === "resolved") return "Resolved";
  if (trip.status === "matched") return "REMIT matched";
  if (noRemitNotice(trip)) {
    const ev = trip.mel_evidence;
    return ev && ev.verdict === "mel_cut"
      ? `No REMIT notice, MEL cut ${ev.unavailable_mw.toFixed(0)} MW`
      : "No REMIT notice";
  }
  return "Open (searching REMIT)";
}

// "Expected back": REMIT's own estimate when there is one, otherwise (marked) the plant's published MEL plan.
function expectedBackLabel(trip) {
  if (trip.event_end_time) return fmtTime(trip.event_end_time);
  const ev = trip.mel_evidence;
  if (ev && ev.verdict === "mel_cut") return ev.back_at ? `MEL plan: ${fmtTime(ev.back_at)}` : "MEL: not back in plan";
  return "--";
}

// The explanation under the table for a trip REMIT has not matched: why that can happen, and what the
// plant's own MEL says in its place.
function noMatchCommentary(trip) {
  const why = `No REMIT match yet for this trip. REMIT publication can lag a real trip by a few minutes, and some plants ` +
    `never publish their trips at all (or publish late, or under a different asset ID).`;
  const ev = trip && trip.mel_evidence;
  if (!ev || ev.verdict === "no_data") return `<p class="trips-hint">${why}</p>`;
  if (ev.verdict === "mel_cut") {
    const since = ev.since ? ` since ${fmtTime(ev.since)}` : "";
    const back = ev.back_at ? `Its published MEL has it back to plan by ${fmtTime(ev.back_at)}.` : "Its published MEL does not return to plan inside the window it covers.";
    return `<p class="trips-hint">${why}</p><p class="trips-hint trips-evidence"><strong>The plant's own MEL tells the story REMIT does not:</strong> ` +
      `it has cut its Maximum Export Level by about ${ev.unavailable_mw.toFixed(0)} MW${since}, so it is signalling that capacity is unavailable. ` +
      `${back} Treat this as a probable unpublished outage; the "expected back" shown is the MEL plan, not a REMIT estimate.</p>`;
  }
  return `<p class="trips-hint">${why}</p><p class="trips-hint trips-evidence">The plant's MEL does <strong>not</strong> show a clear cut ` +
    `(it is within ${ev.unavailable_mw ? "a few" : "0"} MW of plan), so this may be a plan or dispatch change rather than an outage.</p>`;
}

let selectedTripId = null;

function renderTrips(trips) {
  const tbody = document.querySelector("#trips-table tbody");
  tbody.innerHTML = trips.map((t) => {
    const capacity = (t.available_capacity != null && t.normal_capacity != null)
      ? `${t.available_capacity.toFixed(0)} / ${t.normal_capacity.toFixed(0)} MW`
      : "--";
    return `<tr class="trip-row${t.id === selectedTripId ? " selected" : ""}" data-trip-id="${t.id}" data-mrid="${t.remit_mrid || ""}">` +
      `<td>${t.bm_unit}</td>` +
      `<td>${t.fuel_type || "--"}</td>` +
      `<td data-sign="-1">-${t.drop_mw.toFixed(0)} MW</td>` +
      `<td>${fmtTime(t.detected_at)}</td>` +
      `<td>${statusLabel(t)}</td>` +
      `<td>${fmtSpRange(t.settlement_date, t.settlement_period, t.end_settlement_date, t.end_settlement_period)}</td>` +
      `<td>${expectedBackLabel(t)}</td>` +
      `<td>${capacity}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="trips-hint">No trips detected yet.</td></tr>`;

  tbody.querySelectorAll("tr.trip-row").forEach((row) => {
    row.addEventListener("click", () => {
      selectTrip(Number(row.dataset.tripId), trips);
    });
  });

  // First paint: show the most relevant trip (an open or REMIT-matched one if there is one).
  if (selectedTripId === null && trips.length) {
    const live = trips.find((t) => t.status !== "resolved") || trips[0];
    selectTrip(live.id, trips);
  }
}

function selectTrip(id, trips) {
  selectedTripId = id;
  const trip = trips.find((t) => t.id === id);
  loadRevisions(id);
  loadTelemetry(id, trip);
  renderTrips(trips);
}

function renderRevisions(tripId, mrid, startDate, startSp, revisions) {
  const el = document.getElementById("trip-revisions");
  if (!mrid) {
    el.innerHTML = noMatchCommentary(allTrips.find((t) => t.id === tripId));
    return;
  }
  if (!revisions.length) {
    el.innerHTML = `<p class="trips-hint">Matched to REMIT (${mrid}) -- waiting on its first revision detail.</p>`;
    return;
  }
  // Per-revision SP -- this is the "progressively coming back" view: each
  // row is a snapshot of what REMIT expected AT THAT TIME, so a unit whose
  // return keeps slipping later shows its own SP number climbing revision
  // over revision, while one coming back in stages shows unavailable_capacity
  // stepping down at roughly the same expected SP each time.
  const rows = revisions.map((r) => (
    `<tr><td>#${r.revision_number}</td><td>${r.event_status || "--"}</td>` +
    `<td>${fmtTime(r.event_start_time)}</td><td>${fmtTime(r.event_end_time)}</td>` +
    `<td>${fmtSpRange(startDate, startSp, r.end_settlement_date, r.end_settlement_period)}</td>` +
    `<td>${r.unavailable_capacity != null ? r.unavailable_capacity.toFixed(0) + " MW" : "--"}</td>` +
    `<td>${fmtTime(r.publish_time)}</td></tr>`
  )).join("");
  const first = revisions[0], last = revisions[revisions.length - 1];
  const slip = (first.event_end_time && last.event_end_time)
    ? (new Date(last.event_end_time) - new Date(first.event_end_time)) / 3600000
    : null;
  el.innerHTML = `
    <h3>REMIT revision history -- ${mrid}</h3>
    ${slip !== null ? `<p class="trips-hint">Return-time estimate moved <strong>${fmtHours(slip)}</strong> from its first to its latest revision.</p>` : ""}
    <div class="fpn-table-wrap"><table><thead><tr>
      <th>Rev</th><th>Status</th><th>Event start</th><th>Expected back</th><th>SPs affected</th><th>Unavailable</th><th>Published</th>
    </tr></thead><tbody>${rows}</tbody></table></div>`;
}

let allTrips = [];

async function loadTrips() {
  const res = await fetch("/api/trips/recent");
  const data = await res.json();
  allTrips = data.trips || [];
  renderTrips(allTrips);
}

async function loadRevisions(tripId) {
  const res = await fetch(`/api/trips/${tripId}/revisions`);
  const data = await res.json();
  renderRevisions(tripId, data.mrid, data.start_settlement_date, data.start_settlement_period, data.revisions || []);
}

async function loadWorstBehavior() {
  const res = await fetch("/api/trips/worst-behavior");
  const data = await res.json();
  const tbody = document.querySelector("#worst-behavior-table tbody");
  tbody.innerHTML = (data.units || []).map((u) => (
    `<tr><td>${u.bm_unit}</td><td>${u.fuel_type || "--"}</td><td>${u.outage_count}</td>` +
    `<td>${Number(u.avg_revisions).toFixed(1)}</td><td>${fmtHours(u.avg_slippage_hours)}</td>` +
    `<td>${fmtHours(u.worst_slippage_hours)}</td><td>${u.times_slipped_later}</td></tr>`
  )).join("") || `<tr><td colspan="7" class="trips-hint">No REMIT-matched outages yet.</td></tr>`;
}


// ---- time axis shared by the trip telemetry chart --------------------------
// 30-minute x ticks labelled with three lines (SP / date / time), London time.

const ukParts = (ms) => {
  const d = new Date(ms);
  const hm = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
  const date = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", day: "2-digit", month: "2-digit", year: "numeric" }).format(d);
  const [h, m] = hm.split(":").map(Number);
  return { hm, date, sp: h * 2 + Math.floor(m / 30) + 1 };
};

function timeAxis() {
  return {
    type: "time", time: { unit: "minute", stepSize: 30, tooltipFormat: "yyyy-MM-dd HH:mm" },
    grid: { color: "#474847" },
    ticks: {
      color: "#d6d6d6", maxRotation: 0, autoSkip: true, maxTicksLimit: 12, source: "auto",
      callback: (value) => { const p = ukParts(value); return [`SP${p.sp}`, p.date, p.hm]; },
    },
  };
}

// ---- GridTrip-style trip chart ---------------------------------------------
// Design ported from the GridTrip Alert app TripChart: scheduled FPN as a
// dashed indigo area, delivered (adjusted) FPN as a solid emerald area, MEL as a
// red step line, and a tooltip that spells out the shortfall. Data comes from
// /api/trips/{id}/telemetry, so there is nothing to paste or upload.
const TT = { plan: "#4f46e5", delivered: "#10b981", mel: "#ff4d6d" };

// MEL is the line that explains a trip (it is where the plant itself says how much it can export), so
// it is the most prominent thing on the chart: a thick bright step line (no shading), a marker at every
// point where it changes, and a label with the new MW value at the biggest of those changes.
const MEL_CHANGE_MW = 0.5;
const MEL_LABELS_MAX = 6;

function melChangeIndices(pts) {
  const idx = [];
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1].mel, b = pts[i].mel;
    if (a != null && b != null && Math.abs(b - a) > MEL_CHANGE_MW) idx.push(i);
  }
  return idx;
}

function melLabelsPlugin(pts, changes) {
  // only the largest changes are labelled, so a noisy MEL cannot bury the chart in text
  const labelled = new Set([...changes].sort((i, j) => Math.abs(pts[j].mel - pts[j - 1].mel) - Math.abs(pts[i].mel - pts[i - 1].mel)).slice(0, MEL_LABELS_MAX));
  return {
    id: "melLabels",
    afterDatasetsDraw(chart) {
      const meta = chart.getDatasetMeta(2);              // the MEL dataset
      if (!meta || meta.hidden) return;
      const g = chart.ctx, area = chart.chartArea;
      g.save();
      g.font = "bold 11px Consolas, monospace";
      g.textAlign = "center";
      g.textBaseline = "middle";
      for (const i of labelled) {
        const el = meta.data[i];
        if (!el) continue;
        const text = `MEL ${Math.round(pts[i].mel)} MW`;
        const w = g.measureText(text).width + 12;
        const x = Math.min(Math.max(el.x, area.left + w / 2), area.right - w / 2);
        // above the point, unless it sits near the top of the chart
        const y = el.y - 18 < area.top + 10 ? el.y + 18 : el.y - 18;
        g.fillStyle = "rgba(40,8,18,0.92)";
        g.strokeStyle = TT.mel;
        g.lineWidth = 1.5;
        g.beginPath();
        g.roundRect(x - w / 2, y - 10, w, 20, 4);
        g.fill();
        g.stroke();
        g.fillStyle = "#ffffff";
        g.fillText(text, x, y);
      }
      g.restore();
    },
  };
}
let ttChart = null;

function ttMetric(label, value, cls = "") {
  return `<div class="bm-tile"><span class="label">${label}</span><span class="value ${cls}">${value}</span></div>`;
}

function renderTelemetryMetrics(data, trip) {
  const s = data.stats || {};
  const mw = (v) => (v == null ? "--" : `${v.toFixed(1)} MW`);
  const melNote = s.mel_limited_share == null ? "--"
    : s.mel_limited_share >= 0.5 ? "MEL cut (plant limit)" : "Output below plan, MEL intact";
  const remit = !trip ? "--" : trip.status === "resolved" ? "Resolved"
    : trip.remit_mrid ? `Confirmed${trip.event_end_time ? `, back ${fmtTime(trip.event_end_time)}` : ""}` : "Searching REMIT";
  document.getElementById("tt-metrics").innerHTML =
    ttMetric("Generation loss", s.loss_mw == null ? "--" : `-${s.loss_mw.toFixed(1)} MW`, "tt-loss") +
    ttMetric("Impact", s.impact_pct == null ? "--" : `${s.impact_pct.toFixed(1)}%`, "tt-loss") +
    ttMetric("Mean plan (FPN)", mw(s.mean_fpn)) + ttMetric("Mean delivered (ADJ)", mw(s.mean_adj)) +
    ttMetric("Peak shortfall", mw(s.peak_shortfall_mw)) + ttMetric("Constraint source", melNote) + ttMetric("REMIT", remit);
}

function verticalMarker(label, x, ymax, colour) {
  return { label, data: [{ x, y: 0 }, { x, y: ymax }], borderColor: colour, borderDash: [4, 4], borderWidth: 1, pointRadius: 0, order: 99 };
}

async function loadTelemetry(tripId, trip) {
  const empty = document.getElementById("tt-empty");
  let data;
  try {
    const res = await fetch(`/api/trips/${tripId}/telemetry`);
    if (!res.ok) throw new Error(res.status);
    data = await res.json();
  } catch (e) {
    empty.textContent = "Could not load the telemetry for this trip.";
    return;
  }
  if (tripId !== selectedTripId) return; // another trip was selected while this loaded
  trip = trip || data.trip;
  document.getElementById("tt-title").textContent = data.ongoing
    ? `${trip.bm_unit} · ongoing since SP${trip.settlement_period} ${trip.settlement_date} · ${trip.fuel_type || "--"} · figures for the current period`
    : `${trip.bm_unit} · SP${trip.settlement_period} · ${trip.settlement_date} · ${trip.fuel_type || "--"}`;
  renderTelemetryMetrics(data, trip);
  empty.textContent = data.has_data ? "" : "No telemetry stored for this trip yet (it is saved from the moment a plant is flagged as tripped).";
  if (ttChart) { ttChart.destroy(); ttChart = null; }
  const pts = data.points || [];
  if (!pts.length) return;

  const xy = (key) => pts.map((p) => ({ x: Date.parse(p.t), y: p[key] }));
  const ymax = Math.max(100, ...pts.map((p) => Math.max(p.fpn ?? 0, p.adj ?? 0, p.mel ?? 0))) * 1.08;
  const tMin = Date.parse(pts[0].t), tMax = Date.parse(pts[pts.length - 1].t);
  const changeList = melChangeIndices(pts);
  const melChanges = new Set(changeList);
  const datasets = [
    { label: "Scheduled (FPN)", data: xy("fpn"), borderColor: TT.plan, borderDash: [4, 4], borderWidth: 1.5,
      backgroundColor: "rgba(99,102,241,0.14)", fill: "origin", pointRadius: 0, tension: 0.2, order: 3 },
    { label: "Delivered (ADJ)", data: xy("adj"), borderColor: TT.delivered, borderWidth: 3,
      backgroundColor: "rgba(16,185,129,0.32)", fill: "origin", pointRadius: 0, tension: 0.2, order: 2 },
    { label: "Capability (MEL)", data: xy("mel"), borderColor: TT.mel, borderWidth: 4, stepped: "after",
      // a line only: no shading under or between it and the plan
      fill: false, spanGaps: false, order: 1,
      pointRadius: pts.map((_, i) => (melChanges.has(i) ? 6 : 0)), pointHoverRadius: 7,
      pointBackgroundColor: "#ffffff", pointBorderColor: TT.mel, pointBorderWidth: 3 },
    verticalMarker("now", Date.now(), ymax, "#ffffff"),
  ];
  // The detection time only belongs on the chart if it falls inside the charted window. For a plant
  // that has been down for days it sits far to the left, and a marker there used to stretch the time
  // axis back to it, squashing all the actual data into a sliver on the right.
  const detectedMs = Date.parse(data.detected_at);
  if (detectedMs >= tMin && detectedMs <= tMax) datasets.splice(3, 0, verticalMarker("detected", detectedMs, ymax, "#f43f5e"));
  ttChart = new Chart(document.getElementById("tt-chart"), {
    type: "line", data: { datasets }, plugins: [melLabelsPlugin(pts, changeList)],
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        // Ticks only on settlement-period boundaries (:00 / :30), taken from the data points.
        x: { ...timeAxis(), min: tMin, max: tMax, ticks: { ...timeAxis().ticks, source: "data", autoSkip: false },
             afterBuildTicks: (axis) => { axis.ticks = axis.ticks.filter((t) => new Date(t.value).getUTCMinutes() % 30 === 0); } },
        // A little room below zero: in a full trip MEL IS zero, and a line drawn exactly on the axis is
        // half hidden by it.
        y: { min: -ymax * 0.05, max: ymax, title: { display: true, text: "MW", color: "#d6d6d6" }, grid: { color: "#474847" },
             ticks: { color: "#d6d6d6", callback: (v) => (v < 0 ? "" : v) } },
      },
      plugins: {
        legend: { labels: { color: "rgb(185,185,185)", boxWidth: 12 } },
        tooltip: {
          filter: (item) => item.datasetIndex < 3,
          backgroundColor: "#0f172a", borderColor: "#334155", borderWidth: 1, titleColor: "#94a3b8", bodyColor: "#e2e8f0",
          callbacks: {
            label: (c) => {
              const name = ["PLAN (FPN)", "ACTUAL (ADJ)", "MEL (CAPABILITY LIMIT)"][c.datasetIndex];
              return c.parsed.y == null ? `${name}: --` : `${name}: ${c.parsed.y.toFixed(1)} MW`;
            },
            footer: (items) => {
              const get = (i) => items.find((it) => it.datasetIndex === i)?.parsed.y;
              const fpn = get(0), adj = get(1);
              return fpn != null && adj != null && fpn - adj > 0 ? `SHORTFALL: -${(fpn - adj).toFixed(1)} MW` : "";
            },
          },
        },
      },
    },
  });
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
  const ws = new WebSocket(`${proto}//${location.host}/ws/trips`);
  const status = document.getElementById("connection-status");
  ws.onopen = () => { status.textContent = "live"; status.className = "connected"; };
  ws.onclose = () => {
    status.textContent = "reconnecting…";
    status.className = "disconnected";
    setTimeout(connectWebSocket, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = () => {
    // Every message type (trip/remit_match/remit_revision/resolved) changes
    // something this page shows -- simplest correct thing is to just
    // refetch both panels rather than hand-patch each message shape twice.
    loadTrips();
    loadWorstBehavior();
    if (selectedTripId) { loadRevisions(selectedTripId); loadTelemetry(selectedTripId, allTrips.find((t) => t.id === selectedTripId)); }
  };
  setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send("ping"); }, 30000);
}

loadEnvBadge();
loadTrips();
loadWorstBehavior();
connectWebSocket();
setInterval(() => { if (selectedTripId) loadTelemetry(selectedTripId, allTrips.find((t) => t.id === selectedTripId)); }, 60000);
