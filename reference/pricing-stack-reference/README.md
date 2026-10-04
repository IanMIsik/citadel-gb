# Reference: the reference app pricing stack (predecessor Electron app)

Extracted from `reference-app-production-1.0.1.zip` (an NSIS-installer-wrapped
Electron app, 2020) for reference while building Citadel's own pricing
stack page. Not part of the Citadel build — kept here purely as design
reference, so the .exe/.zip doesn't need re-extracting.

- `index.html` / `styles.css` / `index.js` — the pricing-stack view itself.
  A near-empty shell (`<div id="ps-table">`) populated at runtime by JS,
  not a static template.
- `table-view-utils.js` / `table-data-utils.js` — the actual table-building
  logic: one continuous table per settlement period, columns
  `sp | Flag | Bmunit | vol | price | price_d | vol_d | vol_to_price`,
  with the F (unflagged)/T (flagged) groups collapsible via a toggle
  button whose state persists in `sessionStorage`.
- `custom.css` — the app's own bespoke dark-theme overrides on top of a
  generic third-party Bootstrap admin template (that template itself
  wasn't copied here or into Citadel — only genuinely bespoke choices
  were borrowed).

## What Citadel's own web UI borrowed from this

See `citadel/web/style.css`'s top-of-file comment for the exact borrowed
values: the `#0c0c0c` near-black background, the muted `#b9bbb3` table
text tone, the `#72c4f6` settlement-period accent colour, and the
`td[data-sign^="-"]` convention for colouring negative values red
(`#ff6666`) regardless of context.

Citadel's own structure (per-period cards with Unflagged/Flagged tabs,
compact enough to show 3+ settlement periods on a portrait screen) is a
deliberate departure from this app's single continuous collapsible table —
not a limitation, a different design decision made after this one was
reviewed.
