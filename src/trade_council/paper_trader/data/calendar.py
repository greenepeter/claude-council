"""Economic calendar fetcher — Finnhub-backed.

Replaces the dead Fair Economy mirror. Uses Finnhub's /calendar/economic
endpoint (free tier: 60 calls/min, requires API key in FINNHUB_API_KEY env).

Finnhub event shape that we translate to CalendarEvent:

    {
        "actual": null,
        "country": "US",                    # ISO country code; we map to currency
        "estimate": null,
        "event": "Initial Jobless Claims",  # used as title
        "impact": "medium",                 # case-normalized
        "prev": 250000,
        "time": "2026-05-15 12:30:00",      # UTC; mapped to `when`
        "unit": ""
    }

The engine cares about two things per cycle:

    1. Is the plan currently inside a "blocked" window around any matching event?
       (e.g. CPI -24h to +2h.)
    2. Has the event released yet, and if so, what was the actual print?
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import json
import os
import urllib.error
import urllib.parse
import urllib.request


FINNHUB_BASE_URL = "https://finnhub.io/api/v1/calendar/economic"
USER_AGENT = "trade_council-paper-trader/0.1 (+https://localhost)"

# Finnhub returns ISO country codes; our event_gates use currency codes.
# Eurozone country codes all collapse to EUR (consistent with how the
# moderator phrases gates: "EUR CPI" not "DE CPI"). For events specific
# to one Eurozone country, callers should filter on event title rather
# than the country code.
ISO_COUNTRY_TO_CURRENCY = {
    "US":  "USD", "USA": "USD",
    "JP":  "JPY",
    "GB":  "GBP", "UK":  "GBP",
    "EU":  "EUR",
    "DE":  "EUR", "FR":  "EUR", "IT":  "EUR", "ES":  "EUR", "NL": "EUR",
    "BE":  "EUR", "AT":  "EUR", "IE":  "EUR", "PT":  "EUR", "GR": "EUR",
    "FI":  "EUR", "LU":  "EUR", "SK":  "EUR", "EE":  "EUR", "LV": "EUR",
    "LT":  "EUR", "SI":  "EUR", "CY":  "EUR", "MT":  "EUR",
    "AU":  "AUD",
    "NZ":  "NZD",
    "CA":  "CAD",
    "CH":  "CHF",
    "CN":  "CNY", "CNH": "CNY",
}

# Finnhub impact values are lowercase; our internal convention is title-case
# to match the legacy Fair-Economy feed's casing (and how plan event_gates
# spell it: "High" / "Medium" / "Low").
IMPACT_NORMALIZE = {
    "low":     "Low",
    "medium":  "Medium",
    "high":    "High",
    "holiday": "Holiday",
}


@dataclass
class CalendarEvent:
    title: str
    country: str                    # currency code (translated from Finnhub ISO country)
    when: datetime                  # tz-aware UTC
    impact: str                     # "Low" | "Medium" | "High" | "Holiday"
    forecast: str
    previous: str
    actual: str                     # empty until release

    @property
    def is_released(self) -> bool:
        return bool(self.actual and self.actual.strip())

    def time_until(self, now: datetime) -> timedelta:
        return self.when - now


class CalendarSource:
    """Finnhub-backed calendar source.

    Same public API as the previous Fair-Economy implementation — only the
    backing service changed:
      * fetch() -> list[CalendarEvent]
      * find_matching(events, title_contains, country=None, impact=None,
                       within=None, now=None) -> list[CalendarEvent]

    Free tier: 60 calls/min. With the default 4h cache TTL and a single-tick
    hourly cron, we make at most 6 calls/day for the engine plus a handful
    for resolver lookups — well within bounds.

    Robustness:
      * On HTTP/network failure during a fresh fetch, falls back to the cache
        even if expired (rather than failing the whole cycle).
      * Cache key is date-range-stamped so re-fetching the same window hits
        the cache.
    """

    def __init__(self, cache_dir: Path | None = None, cache_ttl_minutes: int = 240):
        self.cache_dir = cache_dir
        self.cache_ttl = timedelta(minutes=cache_ttl_minutes)
        self.api_key = os.environ.get("FINNHUB_API_KEY", "").strip()

    def fetch(self) -> list[CalendarEvent]:
        if not self.api_key:
            raise RuntimeError(
                "FINNHUB_API_KEY not set in environment. "
                "Sign up for a free key at https://finnhub.io and put it in .env."
            )

        # 21-day window so the 14-day resolver lookup always has headroom for
        # event-form next_review values (e.g., "after USD CPI" 18 days out).
        now = datetime.now(timezone.utc)
        params = {
            "token": self.api_key,
            "from":  now.strftime("%Y-%m-%d"),
            "to":    (now + timedelta(days=21)).strftime("%Y-%m-%d"),
        }
        url = f"{FINNHUB_BASE_URL}?{urllib.parse.urlencode(params)}"

        # Cache key is range-stamped, NOT token-stamped (no secrets on disk).
        cache_key = f"finnhub_{params['from']}_to_{params['to']}.json"

        data = self._fetch_with_cache(url, cache_key)
        return self._parse(data)

    def find_matching(
        self,
        events: list[CalendarEvent],
        title_contains: str,
        country: str | None = None,
        impact: str | None = None,
        within: timedelta | None = None,
        now: datetime | None = None,
    ) -> list[CalendarEvent]:
        """Substring + country + impact filter; optional time-distance window."""
        now = now or datetime.now(timezone.utc)
        needle = title_contains.lower()
        out: list[CalendarEvent] = []
        for e in events:
            if needle not in e.title.lower():
                continue
            if country and e.country.upper() != country.upper():
                continue
            if impact and e.impact.lower() != impact.lower():
                continue
            if within is not None:
                if abs((e.when - now).total_seconds()) > within.total_seconds():
                    continue
            out.append(e)
        return out

    # ── internals ──────────────────────────────────────────────────────────

    def _cache_path(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / key

    def _fetch_with_cache(self, url: str, cache_key: str) -> bytes:
        cache = self._cache_path(cache_key)
        if cache is not None and cache.exists():
            age = datetime.now(timezone.utc).timestamp() - cache.stat().st_mtime
            if age < self.cache_ttl.total_seconds():
                return cache.read_bytes()

        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            # Stale-cache fallback: if a fresh fetch fails (including HTTP
            # 4xx/5xx from urllib.error.HTTPError, which is a URLError
            # subclass), return whatever cache we have even if expired.
            # Better stale data than no data — keeps event_gates working
            # through transient API issues.
            if cache is not None and cache.exists():
                return cache.read_bytes()
            raise RuntimeError(f"Calendar fetch failed and no cache available: {e}") from e

        if cache is not None:
            cache.write_bytes(payload)
        return payload

    def _parse(self, payload: bytes) -> list[CalendarEvent]:
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Calendar payload was not valid JSON: {e}") from e

        if not isinstance(raw, dict):
            raise RuntimeError(f"Calendar payload unexpected shape: {type(raw).__name__}")

        raw_events = raw.get("economicCalendar") or []
        events: list[CalendarEvent] = []
        for entry in raw_events:
            if not isinstance(entry, dict):
                continue
            try:
                when = self._parse_when(entry.get("time", ""))
            except ValueError:
                continue

            iso_country = str(entry.get("country") or "").upper().strip()
            currency = ISO_COUNTRY_TO_CURRENCY.get(iso_country, iso_country)

            impact_raw = str(entry.get("impact") or "").lower().strip()
            impact = IMPACT_NORMALIZE.get(impact_raw, impact_raw.title() or "Low")

            events.append(CalendarEvent(
                title=str(entry.get("event") or "").strip(),
                country=currency,
                when=when,
                impact=impact,
                forecast=_str_or_empty(entry.get("estimate")),
                previous=_str_or_empty(entry.get("prev")),
                actual=_str_or_empty(entry.get("actual")),
            ))
        events.sort(key=lambda e: e.when)
        return events

    @staticmethod
    def _parse_when(s: Any) -> datetime:
        """Finnhub returns UTC time as 'YYYY-MM-DD HH:MM:SS' (no tz suffix).

        Accept a few common variants defensively.
        """
        s = (str(s) if s is not None else "").strip()
        if not s:
            raise ValueError("empty time field")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        # Last resort: try ISO with Z suffix
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"could not parse time {s!r}")


def _str_or_empty(v: Any) -> str:
    """Finnhub uses None freely for missing fields. Coerce to empty string
    so downstream string ops don't choke."""
    if v is None:
        return ""
    return str(v).strip()
