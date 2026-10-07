// Plant trip alerts shared by the FPN and Plant Trips pages: a short WebAudio alarm (no binary
// asset needed) plus an in-page toast, no OS Notification permission. See engine/fpn.py's
// detect_trips() for what counts as a trip (>50MW single-unit MIL/MEL drop, edge-triggered) and
// engine/fpn_runner.py's _remit_poll_loop for the REMIT match/revision/resolved follow-ups the
// same toast is updated in place with.
//
// Browsers only let a page make sound after the person has clicked or pressed a key on it at
// least once since it loaded. A tab left open untouched is therefore silent when a trip arrives,
// so one AudioContext is shared, resumed on the first interaction, and a small banner asks for
// that click for as long as the context is still suspended.

let tripAudioCtx = null;

function tripAudio() {
  if (!tripAudioCtx) {
    try { tripAudioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { return null; }
    tripAudioCtx.onstatechange = updateAudioBanner;
  }
  return tripAudioCtx;
}

function updateAudioBanner() {
  const ctx = tripAudio();
  let el = document.getElementById("trip-audio-banner");
  if (!ctx || ctx.state === "running") { if (el) el.remove(); return; }
  if (el) return;
  el = document.createElement("button");
  el.id = "trip-audio-banner";
  el.type = "button";
  el.textContent = "🔔 Click here once to enable trip alert sounds";
  el.addEventListener("click", () => { unlockTripAudio(); playRecoveryChime(); });
  document.body.appendChild(el);
}

function unlockTripAudio() {
  const ctx = tripAudio();
  if (ctx && ctx.state === "suspended") ctx.resume().then(updateAudioBanner, () => {});
  updateAudioBanner();
}

["pointerdown", "keydown", "touchstart"].forEach((evt) => document.addEventListener(evt, unlockTripAudio, { passive: true }));
document.addEventListener("DOMContentLoaded", updateAudioBanner);
if (document.readyState !== "loading") updateAudioBanner();

function playTones(tones, type, peak, length) {
  const ctx = tripAudio();
  if (!ctx) return;
  if (ctx.state === "suspended") ctx.resume().catch(() => {});
  tones.forEach(([delay, freq]) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type;
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(peak, ctx.currentTime + delay);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + delay + length);
    osc.connect(gain).connect(ctx.destination);
    osc.start(ctx.currentTime + delay);
    osc.stop(ctx.currentTime + delay + length);
  });
}

function playTripAlarm() {
  playTones([[0, 880], [0.18, 660], [0.36, 880]], "square", 0.15, 0.15);
}

// "Trip going away" pop-up (green, softer rising chime): `partial` once the drop has halved from
// its peak, `full` once it's cleared. Fires once per stage per trip -- see detect_trips().
function playRecoveryChime() {
  playTones([[0, 523], [0.15, 784]], "sine", 0.12, 0.25);
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

// One /ws/trips message -> the matching toast/sound. Pages call this from their own socket.
function handleTripAlertMessage(msg) {
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
}
