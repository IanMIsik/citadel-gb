"""Elexon REMIT (outage/unavailability message) fetchers -- public, no key
required, same base as elexon_rest.py. Verified live against the real API:

- GET /remit/list/by-event?from=&to=&latestRevisionOnly=&assetId= -- max
  7-day `from`/`to` window. A lightweight index: `{id, mrid,
  revisionNumber, createdTime, publishTime, url}` per message.
  `latestRevisionOnly=false` returns every revision as its own `id` -- the
  only way to see an outage's full revision history. `assetId` is a real
  server-side filter (confirmed live: narrows to exactly that unit's own
  events, not silently ignored) -- avoids paging through hundreds of
  unrelated events per poll.
- GET /remit/{id} -- full detail for one specific message id (one specific
  revision), including `assetId` (matches Citadel's own `bm_unit` format
  exactly, e.g. "E_LYNE2", "T_HUMR-1" -- a direct join key), `fuelType`,
  `eventStatus` ("Active"/"Inactive"/"Dismissed"), `eventStartTime`,
  `eventEndTime` (the expected-return-time field), `unavailableCapacity`,
  `mrid` (stable id shared by every revision of the same outage).

Returns raw list[dict]/dict (the API's own `data` array/object), not
DataFrames -- matches elexon_rest.py's own split between this thin HTTP
layer and engine-layer computation.
"""

from __future__ import annotations

from . import elexon_rest

BASE = elexon_rest.BASE


async def fetch_remit_by_event(
    client, from_iso: str, to_iso: str, latest_revision_only: bool = True, asset_id: str | None = None
) -> list[dict]:
    params = {"from": from_iso, "to": to_iso, "latestRevisionOnly": str(latest_revision_only).lower()}
    if asset_id:
        params["assetId"] = asset_id
    return await elexon_rest._get(client, f"{BASE}/remit/list/by-event", params)


async def fetch_remit_message(client, message_id: int) -> dict | None:
    data = await elexon_rest._get(client, f"{BASE}/remit/{message_id}", {})
    return data[0] if data else None
