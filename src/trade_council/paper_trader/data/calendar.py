"""Economic calendar fetcher (ForexFactory free JSON mirror).

The Faire Economy mirror is the standard free source mirroring ForexFactory's
weekly calendar. Feed shape (one entry per event):

    {
        "title":   "Consumer Price Index m/m",
        "country": "USD",                   # currency code, not ISO country
        "date":    "2026-05-13T08:30:00-04:00",
        "impact":  "High",                  # "Low" | "Medium" | "High" | "Holiday"
        "forecast": "0.2%",
        "previous": "0.1%",
        "actual":  ""                       # filled in after release
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
import urllib.error
import urllib.request


FOREXFACTORY_THIS_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FOREXFACTORY_NEXT_WEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

USER_AGENT = "trade_council-paper-trader/0.1 (+https://localhost)"


@dataclass
class CalendarEvent:
    title: str
    country: str                    # currency code per the feed
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
    """Fetches and caches the next ~2 weeks of high-impact macro events.

    The mirror's JSON is small (~50KB), so we re-fetch each cycle by default
    and let an on-disk cache absorb transient network failures.
    """

    def __init__(self, cache_dir: Path | None = None, cache_ttl_minutes: int = 30):
        self.cache_dir = cache_dir
        self.cache_ttl = timedelta(minutes=cache_ttl_minutes)

    def fetch(self) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for url in (FOREXFACTORY_THIS_WEEK, FOREXFACTORY_NEXT_WEEK):
            data = self._fetch_with_cache(url)
            events.extend(self._parse(data))
        # De-dupe on (title, when) — feeds occasionally double-list when
        # the week boundary crosses an event.
        seen: set[tuple[str, datetime]] = set()
        unique: list[CalendarEvent] = []
        for e in events:
            key = (e.title, e.when)
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)
        unique.sort(key=lambda e: e.when)
        return unique

    def find_matching(
        self,
        events: list[CalendarEvent],
        title_contains: str,
        country: str | None = None,
        impact: str | None = None,
        within: timedelta | None = None,
        now: datetime | None = None,
    ) -> list[CalendarEvent]:
        """Filter events by substring/country/impact, optionally limited to a window.

        Substring match is case-insensitive on the title. `within` filters to
        events whose scheduled time is within `now ± within`.
        """
        now = now or datetime.now(timezone.utc)
        needle = title_contains.lower()
        out = []
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

    def _cache_path(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # one cache file per source URL
        safe = url.rsplit("/", 1)[-1]
        return self.cache_dir / f"calendar_{safe}"

    def _fetch_with_cache(self, url: str) -> bytes:
        cache = self._cache_path(url)
        if cache is not None and cache.exists():
            age = datetime.now(timezone.utc).timestamp() - cache.stat().st_mtime
            if age < self.cache_ttl.total_seconds():
                return cache.read_bytes()

        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if cache is not None and cache.exists():
                # Fall back to stale cache rather than crashing the cycle
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

        events = []
        for entry in raw:
            try:
                when = self._parse_when(entry.get("date", ""))
            except ValueError:
                continue
            events.append(
                CalendarEvent(
                    title=str(entry.get("title", "")),
                    country=str(entry.get("country", "")),
                    when=when,
                    impact=str(entry.get("impact", "")),
                    forecast=str(entry.get("forecast", "")),
                    previous=str(entry.get("previous", "")),
                    actual=str(entry.get("actual", "")),
                )
            )
        return events

    @staticmethod
    def _parse_when(s: str) -> datetime:
        # The feed uses ISO-8601 with offset (e.g. "2026-05-13T08:30:00-04:00").
        # Python's fromisoformat handles this from 3.11+; for 3.10 we strip "Z".
        if not s:
            raise ValueError("empty date")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
