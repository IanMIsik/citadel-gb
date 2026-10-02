"""Unit tests for Runner's IRIS-freshness gating of REST MEL/MIL fetches
(engine/runner.py): when IRIS has delivered a fresh MELS/MILS message
recently, the REST poll loop should stop re-fetching that dataset every
cycle (it was hammering Elexon's MELS endpoint hard enough to exhaust its
call-volume quota, confirmed live) -- and should revert to fetching it
every cycle again the moment IRIS goes quiet, no restart needed either way.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from citadel.config import Settings
from citadel.engine.runner import IRIS_FRESHNESS_SECONDS, Runner


def _runner(iris_configured: bool) -> Runner:
    settings = Settings(
        database_url="postgresql://unused/unused",
        iris_client_id="x" if iris_configured else "",
        iris_client_secret="y" if iris_configured else "",
        iris_queue_name="z" if iris_configured else "",
    )
    return Runner(settings, pool=None, broadcaster=None)


def test_iris_dataset_fresh_false_when_iris_not_configured():
    runner = _runner(iris_configured=False)
    now = datetime.now(timezone.utc)
    assert runner._iris_dataset_fresh(now, now) is False


def test_iris_dataset_fresh_false_when_never_received():
    runner = _runner(iris_configured=True)
    now = datetime.now(timezone.utc)
    assert runner._iris_dataset_fresh(None, now) is False


def test_iris_dataset_fresh_true_shortly_after_a_message():
    runner = _runner(iris_configured=True)
    now = datetime.now(timezone.utc)
    last_at = now - timedelta(seconds=5)
    assert runner._iris_dataset_fresh(last_at, now) is True


def test_iris_dataset_fresh_false_once_past_the_freshness_window():
    runner = _runner(iris_configured=True)
    now = datetime.now(timezone.utc)
    last_at = now - timedelta(seconds=IRIS_FRESHNESS_SECONDS + 1)
    assert runner._iris_dataset_fresh(last_at, now) is False


async def test_on_iris_message_records_mel_and_mil_timestamps_separately():
    runner = _runner(iris_configured=True)
    assert runner._last_iris_mel_at is None
    assert runner._last_iris_mil_at is None

    await runner._on_iris_message("MELS", {"data": [{"bmUnit": "T_TEST-1"}]})
    assert runner._last_iris_mel_at is not None
    assert runner._last_iris_mil_at is None

    await runner._on_iris_message("MILS", {"data": [{"bmUnit": "T_TEST-1"}]})
    assert runner._last_iris_mil_at is not None
