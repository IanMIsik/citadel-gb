"""fetch_window fans out N periods x 4 per-period datasets + M ranges x 2
MEL/MIL datasets as one asyncio.gather, then slices the flat results list
back apart by position -- easy to get the slice arithmetic wrong, so this
pins it down with monkeypatched fetchers instead of real HTTP calls.
"""

from __future__ import annotations

from datetime import date

import pytest

from citadel.ingest import elexon_rest


@pytest.mark.asyncio
async def test_fetch_window_slices_results_correctly(monkeypatch):
    periods = [(date(2026, 1, 1), 1), (date(2026, 1, 1), 2)]
    ranges = [("2026-01-01T00:00Z", "2026-01-01T01:00Z")]

    async def fake_boalf(client, sd, sp):
        return [{"kind": "boalf", "sp": sp}]

    async def fake_bod(client, sd, sp):
        return [{"kind": "bod", "sp": sp}]

    async def fake_pn(client, sd, sp):
        return [{"kind": "pn", "sp": sp}]

    async def fake_disbsad(client, sd, sp):
        return [{"kind": "disbsad", "sp": sp}]

    async def fake_mels(client, f, t):
        return [{"kind": "mel"}]

    async def fake_mils(client, f, t):
        return [{"kind": "mil"}]

    monkeypatch.setattr(elexon_rest, "fetch_boalf", fake_boalf)
    monkeypatch.setattr(elexon_rest, "fetch_bod", fake_bod)
    monkeypatch.setattr(elexon_rest, "fetch_pn", fake_pn)
    monkeypatch.setattr(elexon_rest, "fetch_disbsad", fake_disbsad)
    monkeypatch.setattr(elexon_rest, "fetch_mels", fake_mels)
    monkeypatch.setattr(elexon_rest, "fetch_mils", fake_mils)

    bundle = await elexon_rest.fetch_window(client=None, periods=periods, mel_mil_ranges=ranges)

    assert [r["kind"] for r in bundle.boalf] == ["boalf", "boalf"]
    assert [r["sp"] for r in bundle.boalf] == [1, 2]
    assert [r["kind"] for r in bundle.bod] == ["bod", "bod"]
    assert [r["kind"] for r in bundle.pn] == ["pn", "pn"]
    assert [r["kind"] for r in bundle.disbsad] == ["disbsad", "disbsad"]
    assert [r["kind"] for r in bundle.mel] == ["mel"]
    assert [r["kind"] for r in bundle.mil] == ["mil"]


@pytest.mark.asyncio
async def test_fetch_window_tolerates_partial_failures(monkeypatch):
    periods = [(date(2026, 1, 1), 1)]

    async def failing(*args, **kwargs):
        raise RuntimeError("boom")

    async def ok(*args, **kwargs):
        return [{"kind": "ok"}]

    monkeypatch.setattr(elexon_rest, "fetch_boalf", failing)
    monkeypatch.setattr(elexon_rest, "fetch_bod", ok)
    monkeypatch.setattr(elexon_rest, "fetch_pn", ok)
    monkeypatch.setattr(elexon_rest, "fetch_disbsad", ok)
    monkeypatch.setattr(elexon_rest, "fetch_mels", ok)
    monkeypatch.setattr(elexon_rest, "fetch_mils", ok)

    bundle = await elexon_rest.fetch_window(client=None, periods=periods, mel_mil_ranges=[])

    assert bundle.boalf == []  # failed fetch flattens to nothing, doesn't raise
    assert bundle.bod == [{"kind": "ok"}]
