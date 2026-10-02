"""EPEX day-ahead GB auction results -- an HTML page scrape (EPEX has no
public JSON API for this), ported from Fundies.ipynb's own
fetch_da_prices()/parse_auction_data()/should_fetch_da_prices(). Feeds
engine/fundies.py's day-ahead table `da_price`/`da_volume` columns.

The 15:31 cutoff below is CET as a **fixed** UTC+1 offset (not DST-aware),
matching the notebook's own `pytz.timezone('CET')` behavior exactly --
kept literal rather than "corrected" to a DST-aware Europe/Paris zone,
same reasoning engine/stack.py documents for other ported arithmetic: the
goal is parity with a known-working reference, not a redesign.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup

CET = timezone(timedelta(hours=1), name="CET")
CUTOFF_HOUR, CUTOFF_MINUTE = 15, 31

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}


def should_fetch_da_prices(existing_delivery_dates: set[date], now: datetime | None = None) -> bool:
    """Whether to bother scraping right now: before the cutoff we want
    today's delivery-date row, after it we want tomorrow's -- once that
    date is already in `existing_delivery_dates` (the caller's own
    `fundies_epex_da` table contents), skip the scrape entirely rather
    than re-hitting EPEX every cycle for a delivery date whose auction has
    already cleared and been stored.
    """
    now = now or datetime.now(CET)
    today_cet = now.astimezone(CET).date()
    tomorrow_cet = today_cet + timedelta(days=1)
    wants_today = (now.hour, now.minute) < (CUTOFF_HOUR, CUTOFF_MINUTE)
    target = today_cet if wants_today else tomorrow_cet
    return target not in existing_delivery_dates


def _trading_and_delivery_dates(now: datetime) -> tuple[str, date]:
    now_cet = now.astimezone(CET)
    cutoff = now_cet.replace(hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE, second=0, microsecond=0)
    if now_cet > cutoff:
        trading_date = now_cet.date()
        delivery_date = now_cet.date() + timedelta(days=1)
    else:
        trading_date = now_cet.date() - timedelta(days=1)
        delivery_date = now_cet.date()
    return trading_date.isoformat(), delivery_date


async def fetch_da_prices(client: httpx.AsyncClient, now: datetime | None = None) -> tuple[str, date]:
    """Returns (raw HTML, delivery_date) for the auction currently
    relevant per the trading/delivery-date cutoff logic above.
    """
    now = now or datetime.now(CET)
    trading_date, delivery_date = _trading_and_delivery_dates(now)
    return await fetch_da_prices_for_delivery_date(client, delivery_date)


async def fetch_da_prices_for_delivery_date(client: httpx.AsyncClient, delivery_date: date) -> tuple[str, date]:
    """Same scrape as `fetch_da_prices()`, for an EXPLICIT delivery date
    (trading date = the day before it) rather than the cutoff-derived
    "current" one -- used to backfill yesterday's own auction, which the
    cutoff logic never reaches on its own (it only ever looks at today's
    or tomorrow's delivery date, whichever the time of day currently
    means, never something already in the past).
    """
    trading_date = (delivery_date - timedelta(days=1)).isoformat()
    url = (
        "https://www.epexspot.com/en/market-results"
        f"?market_area=GB&auction=GB&trading_date={trading_date}"
        f"&delivery_date={delivery_date.isoformat()}"
        "&underlying_year=&modality=Auction&sub_modality=DayAhead"
        "&technology=&data_mode=table&period=&production_period="
    )
    resp = await client.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text, delivery_date


def parse_auction_data(html: str) -> dict:
    """Ported verbatim from the notebook's own BeautifulSoup selectors --
    EPEX's page structure, not ours, so this is as fragile as the
    notebook's own scrape (a page redesign breaks it either way).
    """
    soup = BeautifulSoup(html, "html.parser")

    table_container = soup.find("div", class_="table-container")
    h2_tag = table_container.find("h2")
    metadata = [part.strip() for part in h2_tag.get_text(strip=True).split(">")]
    auction_date = datetime.strptime(metadata[-1], "%d %B %Y").strftime("%Y-%m-%d")

    hours_div = soup.find("div", class_="fixed-column js-table-times")
    hours = [li.text.strip() for li in hours_div.find("ul").find_all("li")]

    table = soup.find("table", class_="table-01")
    baseload_price = float(table.find("div", class_="flex day-1").find("span").text)
    peakload_price = float(table.find("div", class_="flex day-2").find("span").text)

    hourly_data = []
    rows = table.find("tbody").find_all("tr")
    for hour, row in zip(hours, rows):
        tds = row.find_all("td")
        hourly_data.append({
            "delivery_date": auction_date,
            "hour_range": hour,
            "buy_volume": float(tds[0].text.replace(",", "")),
            "sell_volume": float(tds[1].text.replace(",", "")),
            "volume": float(tds[2].text.replace(",", "")),
            "price": float(tds[3].text),
        })

    return {
        "hours": hours,
        "baseload_price": baseload_price,
        "peakload_price": peakload_price,
        "hourly_data": hourly_data,
    }
