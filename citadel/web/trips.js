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

function statusLabel(trip) {
  if (trip.status === "resolved") return "Resolved";
  if (trip.status === "matched") return "REMIT matched";
  return "Open (searching REMIT)";
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
      `<td>${fmtTime(t.event_end_time)}</td>` +
      `<td>${capacity}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="trips-hint">No trips detected yet.</td></tr>`;

  tbody.querySelectorAll("tr.trip-row").forEach((row) => {
    row.addEventListener("click", () => {
      selectedTripId = Number(row.dataset.tripId);
      loadRevisions(selectedTripId);
      const unitSel = document.getElementById("mel-plan-unit");
      const unitName = row.firstElementChild.textContent.trim();
      if ([...unitSel.options].some((o) => o.value === unitName)) { unitSel.value = unitName; loadMelPlan(unitName); }
      renderTrips(trips);
    });
  });
}

function renderRevisions(tripId, mrid, startDate, startSp, revisions) {
  const el = document.getElementById("trip-revisions");
  if (!mrid) {
    el.innerHTML = `<p class="trips-hint">No REMIT match yet for this trip -- publish can lag a real trip by a few minutes.</p>`;
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

async function loadTrips() {
  const res = await fetch("/api/trips/recent");
  const data = await res.json();
  renderTrips(data.trips || []);
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


// ---- worst-behaviour graphs -----------------------------------------------
// Style follows Zapdos' "Worst Behaviour Plants" window: black canvas, one
// line per plant from its 15-colour palette, 30-minute x ticks labelled with
// three lines (SP / date / time).

const WB_COLORS = ["#df574f", "#f2d554", "#3dc77f", "#52a0d2", "#5666bf", "#00b2b6", "#e3ff7a", "#f39da3",
  "#a8dcf3", "#d2d2d0", "#d5927c", "#a67041", "#ffffff", "#ec4488", "#f5d3e4"];
const wbCharts = { wb: null, plan: null };

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

function nowMarker(ymax) {
  const now = Date.now();
  return { label: "now", data: [{ x: now, y: 0 }, { x: now, y: ymax }], borderColor: "#ffffff", borderDash: [4, 4], borderWidth: 1, pointRadius: 0, order: 99 };
}

async function loadWbGraphs() {
  const data = await (await fetch("/api/trips/worst-behaviour")).json();
  const units = data.units || [];
  document.getElementById("wb-empty").textContent = units.length ? "" : "No tripped or recently recovered plants right now.";

  const sel = document.getElementById("mel-plan-unit");
  const previous = sel.value;
  sel.innerHTML = units.map((u) => `<option value="${u.bm_unit}">${u.bm_unit} (${u.status})</option>`).join("");
  if (units.some((u) => u.bm_unit === previous)) sel.value = previous;
  sel.onchange = () => loadMelPlan(sel.value);

  const ymax = Math.max(100, ...units.flatMap((u) => u.series.map((p) => Math.max(p.vol, p.fpn)))) * 1.05;
  const datasets = units.map((u, i) => ({
    label: `${u.bm_unit}${u.status === "recovered" ? " (recovered)" : ""}`,
    data: u.series.map((p) => ({ x: Date.parse(p.t), y: p.vol })),
    borderColor: WB_COLORS[i % WB_COLORS.length], backgroundColor: WB_COLORS[i % WB_COLORS.length],
    borderWidth: 2, pointRadius: 1.5, fill: false, tension: 0,
  }));
  if (units.length) datasets.push(nowMarker(ymax));
  if (wbCharts.wb) wbCharts.wb.destroy();
  wbCharts.wb = new Chart(document.getElementById("wb-chart"), {
    type: "line", data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 3 },
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: timeAxis(),
        y: { min: 0, title: { display: true, text: "MW", color: "#d6d6d6" }, grid: { color: "#474847" }, ticks: { color: "#d6d6d6" } },
      },
      plugins: { legend: { labels: { color: "rgb(185,185,185)", filter: (l) => l.text !== "now" } } },
    },
  });
  if (units.length && !sel.value) sel.value = units[0].bm_unit;
  if (sel.value) loadMelPlan(sel.value);
}

async function loadMelPlan(unit) {
  const empty = document.getElementById("mel-plan-empty");
  if (!unit) { empty.textContent = ""; return; }
  const data = await (await fetch(`/api/trips/mel-plan?bm_unit=${encodeURIComponent(unit)}`)).json();
  const vs = data.vintages || [];
  empty.textContent = vs.length ? "" : "No MEL notifications held for this plant yet.";
  const n = vs.length;
  const datasets = vs.map((v, i) => {
    const t = n === 1 ? 1 : i / (n - 1);                       // 0 = oldest, 1 = newest
    const lightness = 32 + Math.round(t * 38);
    const newest = i === n - 1;
    return {
      label: `notified ${ukParts(Date.parse(v.notification_time)).hm}`,
      data: v.points.map((p) => ({ x: Date.parse(p.t), y: p.mel })),
      borderColor: `hsl(${newest ? 28 : 205}, ${newest ? 95 : 25}%, ${newest ? 58 : lightness}%)`,
      borderWidth: newest ? 3 : 1.5, pointRadius: 0, fill: false, stepped: true,
    };
  });
  const ymax = Math.max(100, ...vs.flatMap((v) => v.points.map((p) => p.mel)), ...data.fpn.map((p) => p.fpn)) * 1.05;
  if (data.fpn.length) {
    datasets.push({ label: "FPN", data: data.fpn.map((p) => ({ x: Date.parse(p.t), y: p.fpn })), borderColor: "#ffffff", borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, fill: false });
  }
  const tripX = Date.parse(data.trip_at);
  datasets.push({ label: "trip detected", data: [{ x: tripX, y: 0 }, { x: tripX, y: ymax }], borderColor: "#ff5353", borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0 });
  datasets.push(nowMarker(ymax));
  if (wbCharts.plan) wbCharts.plan.destroy();
  wbCharts.plan = new Chart(document.getElementById("mel-plan-chart"), {
    type: "line", data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 3 },
      scales: {
        x: timeAxis(),
        y: { min: 0, title: { display: true, text: "MEL (MW)", color: "#d6d6d6" }, grid: { color: "#474847" }, ticks: { color: "#d6d6d6" } },
      },
      plugins: { legend: { labels: { color: "rgb(185,185,185)", filter: (l) => l.text !== "now" } } },
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
    loadWbGraphs();
    if (selectedTripId) loadRevisions(selectedTripId);
  };
  setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send("ping"); }, 30000);
}

loadEnvBadge();
loadTrips();
loadWorstBehavior();
loadWbGraphs();
connectWebSocket();
setInterval(loadWbGraphs, 60000);
