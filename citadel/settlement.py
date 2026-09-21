"""Settlement date / settlement period helpers, Europe/London.

A settlement day is a sequence of 30-minute settlement periods (SP) starting
at local midnight -- 48 on a normal day, 46 the day clocks go forward, 50 the
day clocks go back. Derived from zoneinfo's own DST transitions rather than
hardcoded dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
UTC = ZoneInfo("UTC")
PERIOD_MINUTES = 30


def _local_midnight_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 0, 0, tzinfo=LONDON).astimezone(UTC)


def sp_start_utc(d: date, sp: int) -> datetime:
    if sp < 1:
        raise ValueError(f"settlement period must be >= 1, got {sp}")
    return _local_midnight_utc(d) + timedelta(minutes=PERIOD_MINUTES * (sp - 1))


def sp_end_utc(d: date, sp: int) -> datetime:
    return sp_start_utc(d, sp + 1)


def utc_to_settlement(dt_utc: datetime) -> tuple[date, int]:
    """Inverse of sp_start_utc: which (settlement date, period) a UTC instant falls in."""
    if dt_utc.tzinfo is None:
        raise ValueError("dt_utc must be timezone-aware")
    local = dt_utc.astimezone(LONDON)
    d = local.date()
    start = _local_midnight_utc(d)
    minutes = (dt_utc - start).total_seconds() / 60
    sp = int(minutes // PERIOD_MINUTES) + 1
    return d, sp


@dataclass(frozen=True)
class CurrentPeriod:
    settlement_date: date
    settlement_period: int


def current_period(now: datetime | None = None) -> CurrentPeriod:
    """The settlement period containing `now` (defaults to real current time)."""
    now = now or datetime.now(UTC)
    d, sp = utc_to_settlement(now)
    return CurrentPeriod(d, sp)


def window_around(sd: date, sp: int, before: int = 3, after: int = 2) -> list[tuple[date, int]]:
    """(settlement date, period) pairs from `before` periods before (sd, sp)
    to `after` periods after it -- mirrors the original notebook's
    `[current_sp-3 .. current_sp+2]` live window. Crosses settlement-day
    (and DST) boundaries correctly since it's computed from sp_start_utc,
    not by naively adding/subtracting from an integer SP. The engine needs
    this surrounding context even for a single target period: an
    acceptance ramp (or the "previous acceptance" a marginal-delta
    calculation looks up) can span into a neighbouring period.
    """
    anchor = sp_start_utc(sd, sp)
    return [utc_to_settlement(anchor + timedelta(minutes=PERIOD_MINUTES * offset)) for offset in range(-before, after + 1)]


def rolling_window(now: datetime | None = None, before: int = 5, after: int = 2) -> list[tuple[date, int]]:
    """window_around() anchored at the current settlement period.

    `before` is a bigger margin than window_around()'s own default (5 vs
    3) for a reason specific to the live runner: a period's OWN look-behind
    context (how many earlier periods' BOALF/PN data it gets fetched
    alongside) shrinks as it ages toward the trailing edge of this
    window, reaching zero on its very last refresh before it exits and
    stops being recomputed at all (see engine/runner.py's own comment on
    why this window doesn't reach further back than it does). A bigger
    `before` doesn't eliminate that zero-context final refresh -- it's
    inherent to any period at the window's own edge -- but it keeps a
    period in the actively-recomputed window, on whatever code is
    currently running, for two more cycles than before=3 did, which
    matters because the alternative is a period freezing at whatever
    value happened to be computed the last time it was in-window, however
    stale that computation's logic might be by comparison.
    """
    cur = current_period(now)
    return window_around(cur.settlement_date, cur.settlement_period, before, after)
