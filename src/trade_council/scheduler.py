"""SQLite-backed scheduler. Per-instrument debate rows + cost ledger.

Cloud-portable: pure Python + sqlite3 + no platform-specific APIs. Runs
identically on Windows (Task Scheduler), Linux (cron / systemd timer),
GitHub Actions (workflow cron), or any other periodic-trigger mechanism.

Each row scopes to ONE instrument. EUR/USD reconvene fires for the EUR/USD
row only; it doesn't block AUD/USD's schedule.
"""
from __future__ import annotations
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# Default daily API spend cap. Override via env COST_CAP_USD_PER_DAY.
DEFAULT_DAILY_COST_CAP_USD = 20.0


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    topic TEXT NOT NULL,
    decisions TEXT NOT NULL,        -- JSON list
    specialists TEXT NOT NULL,      -- JSON list or "all"
    moderator TEXT NOT NULL DEFAULT 'fx_swing_chair',
    next_review_at TEXT NOT NULL,   -- ISO datetime UTC
    reconvene_reason TEXT,
    parent_debate_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    debate_dir TEXT,
    cost_usd REAL DEFAULT 0.0,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_schedule_due
    ON schedule (status, next_review_at);

CREATE INDEX IF NOT EXISTS idx_schedule_symbol
    ON schedule (symbol, status);

CREATE TABLE IF NOT EXISTS cost_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,             -- YYYY-MM-DD UTC
    debate_id INTEGER,
    cost_usd REAL NOT NULL,
    kind TEXT NOT NULL,             -- 'research' | 'debate' | 'plan' | 'overseer'
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_cost_date ON cost_ledger (date);
"""


@dataclass
class ScheduleRow:
    id: int
    symbol: str
    topic: str
    decisions: list[str]
    specialists: list[str] | str
    moderator: str
    next_review_at: str
    reconvene_reason: str | None
    parent_debate_id: int | None
    status: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    debate_dir: str | None
    cost_usd: float
    error: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Scheduler:
    """SQLite-backed per-instrument schedule + cost ledger."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)

    def close(self):
        self._conn.close()

    # ---------- Schedule ops ----------

    def add_debate(
        self,
        *,
        symbol: str,
        topic: str,
        decisions: list[str],
        specialists: list[str] | str = "all",
        moderator: str = "fx_swing_chair",
        next_review_at: str,
        reconvene_reason: str | None = None,
        parent_debate_id: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO schedule (symbol, topic, decisions, specialists,
               moderator, next_review_at, reconvene_reason, parent_debate_id,
               status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                symbol.upper(),
                topic,
                json.dumps(decisions),
                json.dumps(specialists) if isinstance(specialists, list) else specialists,
                moderator,
                next_review_at,
                reconvene_reason,
                parent_debate_id,
                _utc_now(),
            ),
        )
        return int(cur.lastrowid)

    def get_due(self, limit: int = 10) -> list[ScheduleRow]:
        """Return debates whose next_review_at has passed and status='pending'."""
        now = _utc_now()
        cur = self._conn.execute(
            """SELECT * FROM schedule
               WHERE status = 'pending' AND next_review_at <= ?
               ORDER BY next_review_at ASC LIMIT ?""",
            (now, limit),
        )
        return [_row_to_dc(r) for r in cur.fetchall()]

    def get_all(self, status: str | None = None) -> list[ScheduleRow]:
        sql = "SELECT * FROM schedule"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY next_review_at ASC"
        cur = self._conn.execute(sql, params)
        return [_row_to_dc(r) for r in cur.fetchall()]

    def mark_running(self, debate_id: int) -> None:
        self._conn.execute(
            "UPDATE schedule SET status='running', started_at=? WHERE id=?",
            (_utc_now(), debate_id),
        )

    def mark_completed(
        self,
        debate_id: int,
        cost_usd: float = 0.0,
        debate_dir: str | None = None,
    ) -> None:
        self._conn.execute(
            """UPDATE schedule
               SET status='completed', completed_at=?, cost_usd=?, debate_dir=?
               WHERE id=?""",
            (_utc_now(), cost_usd, debate_dir, debate_id),
        )

    def mark_error(self, debate_id: int, error: str) -> None:
        self._conn.execute(
            "UPDATE schedule SET status='error', completed_at=?, error=? WHERE id=?",
            (_utc_now(), error[:2000], debate_id),
        )

    def mark_cost_capped(self, debate_id: int) -> None:
        self._conn.execute(
            "UPDATE schedule SET status='cost_capped', completed_at=? WHERE id=?",
            (_utc_now(), debate_id),
        )

    # ---------- Cost ledger ----------

    def record_cost(self, debate_id: int | None, cost_usd: float, kind: str,
                    note: str = "") -> None:
        self._conn.execute(
            "INSERT INTO cost_ledger (date, debate_id, cost_usd, kind, note) VALUES (?,?,?,?,?)",
            (_utc_today(), debate_id, float(cost_usd), kind, note),
        )

    def get_today_cost_usd(self) -> float:
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger WHERE date = ?",
            (_utc_today(),),
        )
        return float(cur.fetchone()[0])


def _row_to_dc(row: sqlite3.Row) -> ScheduleRow:
    decisions = json.loads(row["decisions"])
    sp_raw = row["specialists"]
    try:
        specialists = json.loads(sp_raw) if sp_raw.startswith("[") else sp_raw
    except Exception:
        specialists = sp_raw
    return ScheduleRow(
        id=row["id"],
        symbol=row["symbol"],
        topic=row["topic"],
        decisions=decisions,
        specialists=specialists,
        moderator=row["moderator"],
        next_review_at=row["next_review_at"],
        reconvene_reason=row["reconvene_reason"],
        parent_debate_id=row["parent_debate_id"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        debate_dir=row["debate_dir"],
        cost_usd=float(row["cost_usd"] or 0.0),
        error=row["error"],
    )


# ============================================================================
# next_review value resolution
# ============================================================================

# Time-relative patterns: "in 4 hours", "in 2 days"
_RELATIVE_RE = re.compile(r"^in\s+(\d+)\s*(hours?|days?|h|d)\s*$", re.IGNORECASE)


def resolve_next_review(value: str, calendar_source=None, now: datetime | None = None) -> str:
    """Convert a moderator's next_review decision value into an ISO UTC timestamp.

    Accepted forms:
      - ISO datetime: "2026-05-12T13:00:00Z" or "2026-05-12 13:00 UTC"
      - Relative:    "in 4 hours", "in 2 days"
      - Event:       "after USD CPI" (requires calendar_source to resolve)

    Falls back to "in 2 days" if value is unparseable (with a warning printed).
    """
    now = now or datetime.now(timezone.utc)
    v = (value or "").strip()
    if not v:
        return (now + timedelta(days=2)).isoformat()

    # ISO datetime
    try:
        # Accept variations
        cleaned = v.replace("Z", "+00:00").replace(" UTC", "+00:00").replace(" UTC", "")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass

    # Relative form
    m = _RELATIVE_RE.match(v)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("h"):
            return (now + timedelta(hours=n)).isoformat()
        else:
            return (now + timedelta(days=n)).isoformat()

    # Event form: "after <event>"
    after_match = re.match(r"^after\s+(.+)$", v, re.IGNORECASE)
    if after_match and calendar_source is not None:
        event_query = after_match.group(1).strip()
        try:
            event_dt = _next_event_match(event_query, calendar_source, now)
            if event_dt:
                return (event_dt + timedelta(minutes=30)).isoformat()
        except Exception as e:
            print(f"[scheduler] event lookup failed for {event_query!r}: {e}")

    # Unparseable - default to 2 days
    print(f"[scheduler] could not parse next_review value {value!r} - defaulting to +2 days")
    return (now + timedelta(days=2)).isoformat()


def _next_event_match(query: str, calendar_source, now: datetime) -> datetime | None:
    """Find the next event on the calendar that matches the query string.

    Accepts either currency-code phrasing ("USD CPI", "JPY GDP") or country-name
    phrasing ("US CPI", "Japan GDP"). Country names are translated to currency
    codes before the calendar lookup.
    """
    q = query.upper()

    # First, translate country names to currency codes so downstream code only
    # has to deal with the 3-letter form. Longer keys first to avoid partial
    # matches (e.g. "NEW ZEALAND" before "ZEALAND").
    country_to_ccy = [
        ("NEW ZEALAND", "NZD"),
        ("AUSTRALIAN",  "AUD"),
        ("AUSTRALIA",   "AUD"),
        ("CANADIAN",    "CAD"),
        ("CANADA",      "CAD"),
        ("SWITZERLAND", "CHF"),
        ("SWISS",       "CHF"),
        ("JAPANESE",    "JPY"),
        ("JAPAN",       "JPY"),
        ("BRITISH",     "GBP"),
        ("BRITAIN",     "GBP"),
        ("ENGLISH",     "GBP"),
        ("UK",          "GBP"),
        ("EUROZONE",    "EUR"),
        ("EUROPEAN",    "EUR"),
        ("EUROPE",      "EUR"),
        ("AMERICAN",    "USD"),
        ("USA",         "USD"),
        ("US",          "USD"),
    ]
    for name, ccy in country_to_ccy:
        if name in q:
            q = q.replace(name, ccy)
            break

    currency = None
    for ccy in ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"):
        if ccy in q:
            currency = ccy
            break
    # The keyword is the rest after stripping the currency
    keyword = q.replace(currency or "", "").strip() or q

    # Fetch the full event window from the calendar source. CalendarSource
    # exposes fetch() which returns ~2 weeks of high-impact CalendarEvent
    # dataclasses (NOT dicts) — earlier code that called .events_between()
    # and .get("title") was broken; this is the working API.
    try:
        events = calendar_source.fetch()
    except Exception as e:
        print(f"[scheduler] calendar fetch failed: {type(e).__name__}: {e}")
        return None

    horizon = now + timedelta(days=14)
    # Sort by .when so the first match returned is the soonest qualifying event.
    for ev in sorted(events, key=lambda e: e.when):
        if ev.when < now or ev.when > horizon:
            continue
        if currency and ev.country.upper() != currency:
            continue
        if keyword and keyword not in ev.title.upper():
            continue
        return ev.when
    return None
