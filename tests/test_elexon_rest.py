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
async def test_fetch_window_skips_mel_mil_when_asked(monkeypatch):
    """engine/runner.py's Runner passes fetch_mel=False/fetch_mil=False
    when IRIS has delivered a fresh MELS/MILS message recently -- this
    must skip the HTTP calls entirely (not just discard the result) and
    return None, not [], so the caller can tell "didn't ask" apart from
    "asked, got nothing" and avoid wiping out IRIS's own buffer.
    """
    periods = [(date(2026, 1, 1), 1)]
    ranges = [("2026-01-01T00:00Z", "2026-01-01T01:00Z")]

    async def ok(*args, **kwargs):
        return [{"kind": "ok"}]

    async def boom(*args, **kwargs):
        raise AssertionError("should not be called when fetch_mel/fetch_mil is False")

    monkeypatch.setattr(elexon_rest, "fetch_boalf", ok)
    monkeypatch.setattr(elexon_rest, "fetch_bod", ok)
    monkeypatch.setattr(elexon_rest, "fetch_pn", ok)
    monkeypatch.setattr(elexon_rest, "fetch_disbsad", ok)
    monkeypatch.setattr(elexon_rest, "fetch_mels", boom)
    monkeypatch.setattr(elexon_rest, "fetch_mils", boom)

    bundle = await elexon_rest.fetch_window(
        client=None, periods=periods, mel_mil_ranges=ranges, fetch_mel=False, fetch_mil=False,
    )

    assert bundle.mel is None
    assert bundle.mil is None
    assert [r["kind"] for r in bundle.boalf] == ["ok"]


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
