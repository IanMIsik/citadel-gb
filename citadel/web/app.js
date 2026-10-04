const statusEl = document.getElementById("connection-status");
const periodsEl = document.getElementById("periods");
const ndSp = document.getElementById("nd-sp");
const ndNiv = document.getElementById("nd-niv");
const ndPrice = document.getElementById("nd-price");

// key "YYYY-MM-DD:SP" -> { view, collapsed: Set of "F"/"T" }
const periods = new Map();
// Reversal marks (see columnHtml() below) are a dev-only display aid, not
// validated enough to show on prod -- defaults hidden until loadEnvBadge()'s
// own health check confirms this isn't prod, matching that check's own
// "safe until known" default.
let isDev = false;
// "count" sent to /api/stack/recent: current period + this many before it
// (all completed/past). The server always adds 2 more beyond current too
// -- periods that have passed Gate Closure but haven't started delivering
// yet (see app.py's own docstring) -- so the actual number of cards is up
// to PAST_AND_CURRENT_COUNT + 2; the eviction cap below has to match that
// or we'd immediately drop the future ones the server just sent us.
const PAST_AND_CURRENT_COUNT = 4;
const MAX_CARDS = PAST_AND_CURRENT_COUNT + 2;

// The server marks exactly one period `is_current: true` (see
// broadcast.py/app.py) -- relying on that instead of "the newest key we
// happen to have" matters because the engine's rolling window reaches 2
// periods into the future (advance-notified data already exists for them),
// so "newest" isn't always "currently delivering".
function updateNowDelivering() {
  let currentKey = null;
  for (const [k, entry] of periods) {
    if (entry.view.is_current) currentKey = k;
  }
  if (!currentKey) currentKey = newestFirstKeys(periods.keys())[0]; // fallback before any is_current data has arrived
  if (!currentKey) return;
  const v = periods.get(currentKey).view;
  ndSp.textContent = `SP${v.settlement_period} · ${v.settlement_date}`;
  ndNiv.innerHTML = nivHtml(v.niv_mwh);
  ndPrice.textContent = v.total_misik_price_gbp_mwh != null ? `£${v.total_misik_price_gbp_mwh.toFixed(2)}/MWh` : "--";
}

// NIV rendered as its own bold stat with a short/long badge and sign-based
// colour (reusing the data-sign red-for-negative convention already used
// for table cells) -- more prominent than a plain inline label, per the
// user's request to make it stand out more. Shared by the "now delivering"
// banner and every period card's own header.
function nivHtml(nivMwh) {
  if (nivMwh == null) return "NIV --";
  const sign = nivMwh >= 0 ? "+" : "";
  return `NIV <span class="niv-value" data-sign="${nivMwh}">${sign}${nivMwh.toFixed(1)} MWh</span> ` +
    `<span class="niv-badge ${nivMwh >= 0 ? "niv-short" : "niv-long"}">${nivMwh >= 0 ? "SHORT" : "LONG"}</span>`;
}

function key(sd, sp) {
  return `${sd}:${sp}`;
}

// Newest-first ordering for "YYYY-MM-DD:SP" keys. A plain string sort
// breaks across the single/double-digit SP boundary (e.g. "...:9" sorts
// AFTER "...:10", since '9' > '1' as characters) -- SP is parsed back out
// and compared numerically here instead.
function newestFirstKeys(keys) {
  return [...keys].sort((a, b) => {
    const [aDate, aSp] = a.split(":");
    const [bDate, bSp] = b.split(":");
    if (aDate !== bDate) return aDate < bDate ? 1 : -1;
    return Number(bSp) - Number(aSp);
  });
}

// Cell/row styling below borrows directly from the predecessor Zapdos
// app's pricing stack view (reference/pricing-stack-reference/): the compact
// centred `.pricing-stack-text` cells and the bold total-volume figure.
// Unflagged and flagged are laid out as two side-by-side sub-columns per
// period (see style.css's .ps-columns) rather than stacked sections in one
// table, so both are visible at once. Collapse state lives in our own
// `periods` map (not sessionStorage, and not DOM classList) because a
// card's innerHTML gets rebuilt on every live update (see updateCard) --
// anything stored only in the DOM would be wiped out by the next price
// tick.
function columnHtml(k, flag, title, rows, collapsedSet) {
  const collapsed = collapsedSet.has(flag);
  const sum = rows.reduce((s, r) => s + r.delta_mwh, 0);
  const rowsHtml = rows.map((r) => {
    // No separate Type column -- the unit name itself carries the
    // offer/bid colour now (see style.css's .offer-row/.bid-row rules).
    const dirCls = r.direction === "Offer" ? "offer-row" : "bid-row";
    const isReversal = isDev && r.reversal === -1;
    const reversalMark = isReversal ? `<span class="reversal-mark" title="Flagged as a reversal of an earlier acceptance this period">&#8617;</span>` : "";
    return `<tr class="ps-row ${dirCls}${isReversal ? " reversal-row" : ""}">` +
      `<td class="pricing-stack-text">${reversalMark}${r.bm_unit}</td>` +
      `<td class="pricing-stack-text" data-sign="${r.delta_mwh}">${r.delta_mwh.toFixed(1)}</td>` +
      `<td class="pricing-stack-text" data-sign="${r.price_gbp_mwh}">£${r.price_gbp_mwh.toFixed(2)}</td>` +
      `<td class="pricing-stack-text">${r.cumulative_mwh.toFixed(1)}</td></tr>`;
  }).join("");
  return `
    <div class="ps-column">
      <button type="button" class="ps-col-header${collapsed ? " collapsed" : ""}" data-key="${k}" data-flag="${flag}">
        <span>${title}</span>
        <span class="ps-col-total" data-sign="${sum}">${sum.toFixed(1)} MWh (${rows.length})</span>
      </button>
      <table class="ps-table${collapsed ? " ps-hidden" : ""}">
        <thead><tr><th class="pricing-stack-text">Unit</th><th class="pricing-stack-text">MWh</th><th class="pricing-stack-text">£</th><th class="pricing-stack-text">Cum</th></tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>`;
}

function cardHtml(k, entry) {
  const v = entry.view;
  const price = v.total_misik_price_gbp_mwh != null ? `£${v.total_misik_price_gbp_mwh.toFixed(2)}` : "--";
  return `
    <div class="period-header">
      <span class="sp-label">SP${v.settlement_period}</span>
      <span class="sp-date">${v.settlement_date}</span>
      <span class="sp-niv">${nivHtml(v.niv_mwh)}</span>
      <span class="sp-price">${price}</span>
    </div>
    <div class="ps-columns">
      ${columnHtml(k, "F", "Unflagged", v.unflagged, entry.collapsed)}
      ${columnHtml(k, "T", "Flagged", v.so_flagged, entry.collapsed)}
    </div>`;
}

// Rebuilds a single card's own innerHTML in place (preserving its scroll
// rather than tearing down the whole page -- IRIS can push updates for one
// period every second or so, and re-rendering every card on every message
// would interrupt a click on any card, including ones that didn't even
// change. Cards no longer scroll internally (the full stack renders at
// once, per the user's request), so there's no scroll position to
// preserve here any more -- just the collapse state, already tracked in
// `periods` rather than the DOM.
function updateCard(k) {
  const entry = periods.get(k);
  const el = periodsEl.querySelector(`[data-key="${CSS.escape(k)}"]`);
  if (!entry || !el) return false;
  el.innerHTML = cardHtml(k, entry);
  return true;
}

// Full reconcile -- only for startup and the periodic resync, where cards
// can appear/disappear/reorder (a period rolling out of the recent window).
function renderAll() {
  const orderedKeys = newestFirstKeys(periods.keys());
  for (const el of [...periodsEl.children]) {
    if (!periods.has(el.dataset.key)) el.remove();
  }
  let afterEl = null;
  for (const k of orderedKeys) {
    let el = periodsEl.querySelector(`[data-key="${CSS.escape(k)}"]`);
    if (!el) {
      el = document.createElement("section");
      el.className = "period-card";
      el.dataset.key = k;
      el.innerHTML = cardHtml(k, periods.get(k));
    }
    periodsEl.insertBefore(el, afterEl ? afterEl.nextSibling : periodsEl.firstChild);
    afterEl = el;
  }
  updateNowDelivering();
}

periodsEl.addEventListener("click", (e) => {
  const btn = e.target.closest(".ps-col-header");
  if (!btn) return;
  const entry = periods.get(btn.dataset.key);
  if (!entry) return;
  const flag = btn.dataset.flag;
  if (entry.collapsed.has(flag)) entry.collapsed.delete(flag);
  else entry.collapsed.add(flag);
  updateCard(btn.dataset.key);
});

function upsertPeriod(view) {
  const k = key(view.settlement_date, view.settlement_period);
  const existing = periods.get(k);
  periods.set(k, { view, collapsed: existing ? existing.collapsed : new Set() });
  // Keep only the most recent MAX_CARDS periods in view.
  const ordered = newestFirstKeys(periods.keys());
  for (const staleKey of ordered.slice(MAX_CARDS)) periods.delete(staleKey);
  return k;
}

async function loadRecent() {
  const resp = await fetch(`/api/stack/recent?count=${PAST_AND_CURRENT_COUNT}`);
  const data = await resp.json();
  for (const view of data.periods) upsertPeriod(view);
  renderAll();
}

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/stack`);

  ws.onopen = () => {
    statusEl.textContent = "live";
    statusEl.className = "connected";
  };
  ws.onclose = () => {
    statusEl.textContent = "reconnecting…";
    statusEl.className = "disconnected";
    setTimeout(connectWebSocket, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type !== "stack_update") return;
    const k = upsertPeriod(msg);
    if (!updateCard(k)) renderAll(); // new period (not yet a card) -- needs the full reconcile
    updateNowDelivering();
  };
  setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send("ping"); }, 30000);
}

function loadEnvBadge() {
  fetch("/api/health").then(r => r.json()).then(data => {
    isDev = data.environment_label !== "prod";
    renderAll(); // redraw with reversal marks now that the environment is known
    if (!isDev) return;
    const badge = document.getElementById("env-badge");
    badge.textContent = data.environment_label.toUpperCase();
    badge.classList.remove("hidden");
  });
}

loadEnvBadge();
loadRecent();
connectWebSocket();
setInterval(loadRecent, 30000); // periodic resync -- picks up newly-completed periods rolling into the window
