// Fundies dashboard -- see fundies.html's own comment for the design
// split (your Fundies.xlsx for row order/arrangement, the reference app's real
// fundies.html for column/heatmap mechanics, Fundies.ipynb's own working
// pipeline for the data). SP = settlement period, 1-48 across a day (the
// rare 46/50-period DST-transition days are rendered the same simplified
// way the real reference app table always did -- SP 1-48 fixed).

const SETTLEMENT_PERIODS = Array.from({ length: 48 }, (_, i) => i + 1);

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function addDaysIso(iso, days) {
  const d = new Date(`${iso}T00:00:00`);
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

let selectedDate = todayIso();

// One row per metric, in the exact order of the user's own Fundies.xlsx
// "Fundies" sheet (column A, top to bottom). `source` says which API
// response the value comes from ("rt" = /api/fundies/real-time, "da" =
// /api/fundies/day-ahead, "calc" = derived client-side from both).
// `rt` rows get the light "real-time" row banding the xlsx itself uses
// (alternating fill between real-time and day-ahead/derived rows).
// `digits`: decimal places to display (1dp everywhere, by request);
// `native` rows skip the /1000 kilo-conversion
// (prices and the two auction-volume rows, same exception-list idea as
// the real source's own `datasetsNotInKiloValues`).
const ROWS = [
  { key: "da_ndf", label: "da_ndf", source: "da", digits: 1 },
  { key: "indo", label: "indo", source: "rt", rt: true, digits: 1 },
  { key: "indo_da_ndf_delta", label: "delta", source: "calc", heat: "indo_da_ndf_delta", digits: 1 },
  { key: "latest_ndf", label: "latest_ndf", source: "rt", rt: true, heat: "ndf_resid", digits: 1 },

  { key: "da_windfor", label: "da_winfor", source: "da", heat: "winfor", digits: 1 },
  { key: "latestwindfor", label: "latest_winfor", source: "rt", rt: true, heat: "winfor", digits: 1 },
  { key: "total_wind_outturn", label: "wind_outturn (fake)", source: "rt", rt: true, digits: 1 },
  { key: "fake_wind_ot_da_winfor_delta", label: "delta", source: "calc", heat: "fake_wind_ot_da_winfor_delta", digits: 1 },

  { key: "uk_ifa_net", label: "IFA", source: "da", heat: "interconnector", digits: 1, spacerBefore: true },
  { key: "uk_ifa2_net", label: "IFA2", source: "da", heat: "interconnector", digits: 1 },
  { key: "eleclink_net", label: "Eleclink", source: "da", heat: "interconnector", digits: 1 },
  { key: "uk_nl_net", label: "nl_to_uk", source: "da", heat: "interconnector", digits: 1 },
  { key: "uk_be_net", label: "be_to_uk", source: "da", heat: "interconnector", digits: 1 },
  { key: "intgrnl_net", label: "irl_to_uk (greenlink)", source: "da", heat: "interconnector", digits: 1 },
  { key: "intew_net", label: "irl_to_uk (east-west)", source: "da", heat: "interconnector", digits: 1 },
  { key: "intmoyle_net", label: "n.irl_to_uk (Moyle)", source: "da", heat: "interconnector", digits: 1 },
  { key: "uk_norway_net", label: "nor_to_uk", source: "da", heat: "interconnector", digits: 1 },
  { key: "uk_dk_net", label: "dk_to_uk", source: "da", heat: "interconnector", digits: 1 },
  { key: "ng_vol", label: "natgrid", source: "rt", rt: true, digits: 1 },
  { key: "interconnector_ng", label: "interconnector + NG", source: "calc", heat: "interconnector_ng", digits: 1 },

  { key: "nuke_214", label: "nuke_214", source: "da", digits: 1, spacerBefore: true },
  { key: "latest_resid", label: "latest resid", source: "calc", heat: "ndf_resid", digits: 1, spacerBefore: true },
  { key: "domestic_tight_delta", label: "domestic tight delta", source: "calc", heat: "domestic_tight_delta", digits: 1 },

  { key: "pv_live", label: "pv_live", source: "rt", rt: true, digits: 1, spacerBefore: true },
  { key: "embedded_solar_forecast", label: "natgrid_sol_for", source: "da", digits: 1 },

  { key: "net_imbalance_volume", label: "NIV (MWh)", source: "rt", rt: true, native: true, digits: 1, spacerBefore: true },
  { key: "system_buy_price", label: "Imbalance Price (£/MWh)", source: "rt", rt: true, native: true, prefix: "£", digits: 1 },
  { key: "market_index_price", label: "Market Index Price (£/MWh)", source: "rt", rt: true, native: true, prefix: "£", digits: 1, spacerBefore: true },
  // Market/auction volumes are settlement-period energy quantities (MWh),
  // not an instantaneous MW reading -- same /1000 kilo-scaling as every
  // other row here, landing on GWh (not GW) for that reason.
  { key: "market_index_volume", label: "Market Index Volume (GWh)", source: "rt", rt: true, digits: 1 },
  // Shortened from "Average Market spread" -- the full label overflowed
  // the row-label column's fixed 8.5rem width under table-layout:fixed
  // (the longest label of any row here), clipping per .fundies-table's
  // overflow:hidden/text-overflow:clip rule; this fits with room to spare.
  { key: "avg_market_spread", label: "Avg spread (£/MWh)", source: "calc", native: true, prefix: "£", heat: "spread", digits: 1, spacerBefore: true },

  { key: "embedded_wind_forecast", label: "natgrid_wind_for", source: "da", digits: 1, spacerBefore: true },
  { key: "da_price", label: "DA price (£/MWh)", source: "da", native: true, prefix: "£", digits: 1, spacerBefore: true },
  { key: "da_volume", label: "DA Volume (GWh)", source: "da", digits: 1 },
  { key: "imbalngc", label: "imbal_ngc (GW)", source: "rt", rt: true, digits: 1 },
];

function fmtCell(value, native, digits, prefix) {
  if (value === null || value === undefined || Number.isNaN(value)) return "";
  const scaled = native ? value : value / 1000;
  const text = scaled.toFixed(digits);
  // Prefix goes after a leading minus sign ("-£12.3", not "£-12.3") --
  // only the three £/MWh price rows set this.
  if (!prefix) return text;
  return text.startsWith("-") ? `-${prefix}${text.slice(1)}` : `${prefix}${text}`;
}

// Percentile-heatmap CSS custom properties (--percentile/--value/--opacity),
// consumed by style.css's `td[data-heat=...]` rules -- formulas ported
// verbatim from the real reference app's custom.css. Tagged here by a shared
// `heat` concept (several of our columns map to one formula the source
// only ever named once, e.g. every interconnector-net column reuses its
// own `[data-name='fr'|'nl'|'be'|'irl']` formula under one `interconnector`
// tag) rather than guessing a formula for a row the reference app never had.
// The real source's own updateRow() caps the raw (already kilo-scaled)
// value fed into a `--value`-based hsla formula before using it, per row
// -- otherwise a big settlement period's flow pushes the hue calc outside
// its intended range. Percentile-based formulas (ndf_resid/winfor) don't
// need this -- percentile is already clamped to [-70,70] regardless of scale.
// Only the 3 delta rows use a fixed cap now -- interconnector rows moved
// to a row-relative scale instead (see DIVERGING_ROW_RELATIVE below):
// individual pairs routinely sit in the 0.1-0.7 GW range, nowhere near
// the reference app's own [-18,40] cap (tuned for its own 4 cables' typical
// magnitude), so alpha was always ~0 and the colouring read as basically
// switched off. A row-relative scale (each row's own observed min/max
// that day) gives every pair a meaningful, visible spread regardless of
// its typical size.
const HEAT_VALUE_CAPS = {
  indo_da_ndf_delta: [-4.3, 4.3],
  fake_wind_ot_da_winfor_delta: [-4.3, 4.3],
  domestic_tight_delta: [-4.3, 4.3],
};

// The interconnector/delta rows spend most of their time near zero, and
// the real source's own lightness-based formula for them goes toward
// WHITE at zero (100% lightness, only darkening as |value| grows) -- fine
// against the reference app's own light-mode table, but on this page's dark theme it
// meant most of these rows read as a wash of white cells. Redesigned as a
// translucent blue(positive)/red(negative) overlay instead: alpha scales
// with magnitude (0 at zero, fading the dark background through), hue is
// fixed by sign -- by request, replacing the ported formula rather than
// tuning it further.
const DIVERGING_HEAT_TAGS = new Set([
  "interconnector", "interconnector_ng",
  "indo_da_ndf_delta", "fake_wind_ot_da_winfor_delta", "domestic_tight_delta",
]);
const DIVERGING_ROW_RELATIVE = new Set(["interconnector", "interconnector_ng"]);
const POSITIVE_HUE = 206; // blue
const NEGATIVE_HUE = 4; // red

// Average Market spread is an absolute difference (always >= 0, no
// "direction" to it), so the blue/red diverging scheme above doesn't fit
// -- a single-hue (amber) intensity scale instead, row-relative like the
// interconnector rows (a spread's own typical size varies day to day).
const SEQUENTIAL_HEAT_TAGS = new Set(["spread"]);
const SEQUENTIAL_HUE = 38; // amber

function computeHeatStyle(heat, displayValues) {
  const nums = displayValues.filter((v) => typeof v === "number" && !Number.isNaN(v));
  if (!nums.length) return () => "";
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const range = max - min;
  const cap = HEAT_VALUE_CAPS[heat];
  const diverging = DIVERGING_HEAT_TAGS.has(heat);
  const sequential = SEQUENTIAL_HEAT_TAGS.has(heat);
  const rowRelativeMax = Math.max(Math.abs(min), Math.abs(max)) || 1;

  return (displayValue) => {
    if (typeof displayValue !== "number" || Number.isNaN(displayValue)) return "";
    const cappedValue = cap ? Math.min(Math.max(cap[0], displayValue), cap[1]) : displayValue;

    if (sequential) {
      const alpha = rowRelativeMax ? Math.min(Math.abs(cappedValue) / rowRelativeMax, 1) * 0.55 : 0;
      return `--heat-hue:${SEQUENTIAL_HUE};--heat-alpha:${alpha};`;
    }

    if (diverging) {
      const capMax = DIVERGING_ROW_RELATIVE.has(heat)
        ? rowRelativeMax
        : Math.max(Math.abs(cap[0]), Math.abs(cap[1]));
      // Interconnector rows get a slightly higher alpha ceiling than the
      // fixed-cap delta rows (0.55 vs 0.5) -- pulled back from an earlier
      // 0.68 (paired with a darker cell colour now, see style.css's own
      // td[data-heat="interconnector"] override) after that combination
      // read as a pale, washed-out "whitish" cell rather than a clearly
      // legible one.
      const maxAlpha = DIVERGING_ROW_RELATIVE.has(heat) ? 0.55 : 0.5;
      const alpha = capMax ? Math.min(Math.abs(cappedValue) / capMax, 1) * maxAlpha : 0;
      const hue = cappedValue >= 0 ? POSITIVE_HUE : NEGATIVE_HUE;
      return `--heat-hue:${hue};--heat-alpha:${alpha};`;
    }

    let percentile = range ? ((displayValue - min) / range) * 100 : 0;
    percentile = Math.min(Math.max(-70, percentile), 70);
    const opacity = range ? 1 : 0;
    // Dark text only once a cell actually has a coloured (opacity>0)
    // background -- otherwise the heatmap rule's own dark text would be
    // unreadable against this page's dark background.
    const textColor = opacity ? "#111" : "var(--text)";
    return `--percentile:${percentile};--value:${cappedValue};--opacity:${opacity};--heat-text:${textColor};`;
  };
}

function renderDateHeader(rowId) {
  const el = document.getElementById(rowId);
  if (!el) return;
  const yesterday = addDaysIso(todayIso(), -1);
  const today = todayIso();
  const tomorrow = addDaysIso(todayIso(), 1);
  // Actual calendar dates in the dropdown (same raw-ISO convention app.js's
  // own .sp-date uses elsewhere), not "Yesterday"/"Today"/"Tomorrow" text --
  // by request, so the picker reads unambiguously regardless of which day
  // you're looking at it.
  const opt = (iso) => `<option value="${iso}" ${iso === selectedDate ? "selected" : ""}>${iso}</option>`;
  el.innerHTML = `<th class="fundies-row-label">
      <select id="fundies-date-select">
        ${opt(yesterday)}
        ${opt(today)}
        ${opt(tomorrow)}
      </select>
    </th>` +
    SETTLEMENT_PERIODS.map((sp) => {
      const totalMinutes = (sp - 1) * 30;
      const hh = String(Math.floor(totalMinutes / 60)).padStart(2, "0");
      const mm = totalMinutes % 60 === 0 ? "00" : "30";
      return `<th class="fundies-hour">${hh}:${mm}</th>`;
    }).join("");
  document.getElementById("fundies-date-select").addEventListener("change", (e) => {
    selectedDate = e.target.value;
    loadAll();
  });
}

function renderSpHeader(rowId) {
  const el = document.getElementById(rowId);
  if (!el) return;
  el.innerHTML = `<th class="fundies-row-label">sp</th>` +
    SETTLEMENT_PERIODS.map((sp) => `<th class="sp-number">${sp}</th>`).join("");
}

function computeDerived(row) {
  // "calc" rows the API doesn't return directly -- indo_da_ndf_delta,
  // fake_wind_ot_da_winfor_delta, domestic_tight_delta, interconnector_ng,
  // latest_resid already come pre-computed from fundies_day_ahead (see
  // engine/fundies.py:build_derived_rows); only avg_market_spread is
  // genuinely computed here, client-side, from two already-present columns
  // (ported from the xlsx's own `=ROUND(ABS(market_index_price-imbalance_price),1)`).
  const mip = row.market_index_price;
  const imb = row.system_buy_price;
  row.avg_market_spread = (mip == null || imb == null) ? null : Math.round(Math.abs(mip - imb) * 10) / 10;
  return row;
}

function renderFundiesTable(rtRows, daRows) {
  const rtByPeriod = new Map(rtRows.map((r) => [r.settlement_period, r]));
  const daByPeriod = new Map(daRows.map((r) => [r.settlement_period, r]));
  const merged = new Map();
  SETTLEMENT_PERIODS.forEach((sp) => {
    merged.set(sp, computeDerived({ ...(rtByPeriod.get(sp) || {}), ...(daByPeriod.get(sp) || {}) }));
  });

  const tbody = document.getElementById("fundies-body");
  if (!tbody) return;

  tbody.innerHTML = ROWS.map((cfg) => {
    const values = SETTLEMENT_PERIODS.map((sp) => {
      const raw = merged.get(sp)[cfg.key];
      return raw === null || raw === undefined ? NaN : Number(raw);
    });
    // Heat formulas consume the same DISPLAY-scale number the cell itself
    // shows (kilo-scaled for non-native rows) -- both the row's own
    // min/max percentile basis and any `--value` cap are calibrated
    // against that displayed scale, not the raw MW figure.
    const displayValues = values.map((v) => (Number.isNaN(v) ? v : (cfg.native ? v : v / 1000)));
    const heatFn = cfg.heat ? computeHeatStyle(cfg.heat, displayValues) : null;
    const cells = values.map((v, i) => {
      const text = fmtCell(v, cfg.native, cfg.digits, cfg.prefix);
      const style = heatFn ? heatFn(displayValues[i]) : "";
      const sign = Number.isNaN(v) ? "" : String(v);
      const heatAttr = cfg.heat ? ` data-heat="${cfg.heat}"` : "";
      return `<td data-name="${cfg.key}"${heatAttr} data-sign="${sign}" style="${style}">${text}</td>`;
    }).join("");
    const rowClass = cfg.rt ? ' class="fundies-rt-row"' : "";
    // A blank, borderless spacer row between metric groups (interconnectors,
    // nuke_214, latest resid, pv_live, NIV, natgrid_wind_for, DA price) --
    // by request, to visually separate groups without changing row heights
    // or column widths. colspan covers the label column + all 48 SPs.
    const spacer = cfg.spacerBefore
      ? `<tr class="fundies-spacer-row"><td colspan="${SETTLEMENT_PERIODS.length + 1}"></td></tr>`
      : "";
    return `${spacer}<tr${rowClass}><td class="fundies-row-label">${cfg.label}</td>${cells}</tr>`;
  }).join("");
}

async function loadAll() {
  renderDateHeader("fundies-date-header");
  renderSpHeader("fundies-sp-header");
  const [rtResp, daResp] = await Promise.all([
    fetch(`/api/fundies/real-time?settlement_date=${selectedDate}`),
    fetch(`/api/fundies/day-ahead?settlement_date=${selectedDate}`),
  ]);
  const rt = (await rtResp.json()).rows || [];
  const da = (await daResp.json()).rows || [];
  renderFundiesTable(rt, da);
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
  const ws = new WebSocket(`${proto}//${location.host}/ws/fundies`);
  const status = document.getElementById("connection-status");
  ws.onopen = () => {
    status.textContent = "live";
    status.className = "connected";
  };
  ws.onclose = () => {
    status.textContent = "reconnecting…";
    status.className = "disconnected";
    setTimeout(connectWebSocket, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type !== "fundies_update") return;
    loadAll();
  };
  setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send("ping"); }, 30000);
}

loadEnvBadge();
loadAll();
connectWebSocket();
