"""The paper-trader engine: one cycle = one evaluation of plan vs. live data.

Decision conventions (v1 — match the EUR/USD swing plan as written):
  * Initial stop is evaluated on the most recent completed daily close, NOT
    intraday touch. The plan literally says "daily close below 1.1575".
  * Targets fire on intraday touch (take-profit convention).
  * Once a partial fires and the stop is trailed to entry, the trailed stop
    is evaluated on intraday touch (behavioral lock, not structural).
  * Breakout entry requires a confirmed daily close above the breakout level
    in any of the last N completed daily bars, then an intraday spot retest
    within `retest_tolerance_pips`.
  * Event-gate window blocks NEW entries only. An already-open position is
    not auto-closed pre-event — that's a separate plan decision.

A `Cycle` captures everything observed and decided in one evaluation, so the
notifier and any downstream tooling can render a complete picture without
re-querying.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from trade_council.paper_trader.data.calendar import CalendarEvent, CalendarSource
from trade_council.paper_trader.data.prices import DailyBar, PriceSource, Quote
from trade_council.paper_trader.ledger import (
    Fill, Ledger, Position, quote_currency_of, unrealized_pnl_usd,
)
from trade_council.paper_trader.notify import Notifier
from trade_council.paper_trader.plan import Entry, Plan, Target


PIP_SIZE_DEFAULT = 0.0001           # for non-JPY FX majors


@dataclass
class GateStatus:
    gate_title: str
    matched_event: CalendarEvent | None
    state: str                      # "clear" | "blocked" | "no_match"
    block_window_start: datetime | None = None
    block_window_end: datetime | None = None
    note: str = ""


@dataclass
class EntryReadiness:
    entry_id: str
    state: str                      # "armed" | "waiting" | "gated" | "invalid"
    reason: str
    would_fill_at: float | None = None


@dataclass
class Action:
    """An event the engine took (or considered taking) this cycle."""
    kind: str                       # "opened" | "partial" | "closed" | "trail" | "gated" | "invalidated" | "noop"
    detail: str


@dataclass
class Cycle:
    when: datetime
    plan_id: str
    spot: Quote | None
    last_daily: DailyBar | None
    plan_state: str                 # "active" | "expired" | "invalidated"
    gates: list[GateStatus] = field(default_factory=list)
    upcoming_events: list[CalendarEvent] = field(default_factory=list)
    entry_readiness: list[EntryReadiness] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    position_snapshot: Position | None = None
    unrealized_pnl_usd: float | None = None
    notes: list[str] = field(default_factory=list)
    # True when calendar.fetch() succeeded this cycle. False means event_gates
    # are operationally blind — surfaced into the registry summary so the
    # operator can see it without grepping logs.
    calendar_ok: bool = True


class Engine:
    def __init__(
        self,
        plan: Plan,
        ledger: Ledger,
        calendar: CalendarSource,
        prices: PriceSource,
        notifier: Notifier,
    ):
        self.plan = plan
        self.ledger = ledger
        self.calendar = calendar
        self.prices = prices
        self.notifier = notifier

    def _quote_to_usd(self, cycle: "Cycle") -> float | None:
        """USD value of 1 unit of the plan's QUOTE currency.

        Returns 1.0 for xxxUSD pairs without any network call. For everything
        else (USDxxx and crosses), fetches via PriceSource and caches on the
        Cycle so repeat callers within one evaluate() pass don't re-hit yfinance.

        On fetch failure, appends to cycle.notes and returns None — callers
        must check and skip any P&L-touching work this cycle. The position
        stays put; next cycle retries.
        """
        cached = getattr(cycle, "_quote_to_usd_cached", None)
        if cached is not None:
            return cached if cached > 0 else None

        try:
            ccy = quote_currency_of(self.plan.symbol)
        except ValueError as e:
            cycle.notes.append(f"Could not parse symbol for quote-currency lookup: {e}")
            cycle._quote_to_usd_cached = -1.0
            return None

        if ccy == "USD":
            cycle._quote_to_usd_cached = 1.0
            return 1.0

        try:
            rate = self.prices.quote_currency_to_usd(ccy)
        except Exception as e:
            cycle.notes.append(
                f"quote_to_usd fetch failed for {ccy}: {type(e).__name__}: {e}"
            )
            cycle._quote_to_usd_cached = -1.0
            return None

        cycle._quote_to_usd_cached = rate
        return rate

    # ── Main entry point ────────────────────────────────────────────────────

    def evaluate(self) -> Cycle:
        now = datetime.now(timezone.utc)
        cycle = Cycle(
            when=now,
            plan_id=self.plan.plan_id,
            spot=None,
            last_daily=None,
            plan_state="active",
        )

        # 1. Plan-level invalidation (cheap checks first)
        if self.plan.is_expired:
            cycle.plan_state = "expired"
            cycle.actions.append(Action("invalidated", "Plan horizon exceeded."))
            return self._finalize(cycle)

        # 2. Pull data
        try:
            cycle.spot = self.prices.quote(self.plan.yfinance_ticker)
        except Exception as e:
            cycle.notes.append(f"Spot fetch failed: {e}")

        try:
            cycle.last_daily = self.prices.last_daily_close(self.plan.yfinance_ticker)
        except Exception as e:
            cycle.notes.append(f"Daily-bar fetch failed: {e}")

        try:
            events = self.calendar.fetch()
        except Exception as e:
            cycle.notes.append(f"CALENDAR_DEAD: {type(e).__name__}: {e}")
            cycle.calendar_ok = False
            events = []
        cycle.upcoming_events = [
            e for e in events
            if e.when >= now and e.when <= now + timedelta(days=10)
            and e.impact.lower() in ("high", "medium")
        ][:20]

        # 3. Plan-level structural invalidation (needs daily bar)
        for rule in self.plan.invalidation:
            if rule.condition == "daily_close_below" and cycle.last_daily is not None:
                if cycle.last_daily.close < rule.level:
                    cycle.plan_state = "invalidated"
                    cycle.actions.append(Action(
                        "invalidated",
                        f"Daily close {cycle.last_daily.close:.4f} on {cycle.last_daily.date} "
                        f"is below invalidation level {rule.level:.4f}. {rule.note}"
                    ))
                    return self._finalize(cycle)

        # 4. Gate evaluation
        cycle.gates = self._evaluate_gates(events, now)

        # 5. Position-management branch
        if self.ledger.has_open_position():
            self._manage_open_position(cycle)
        else:
            self._evaluate_entries(cycle)

        return self._finalize(cycle)

    # ── Gate evaluation ─────────────────────────────────────────────────────

    def _evaluate_gates(self, events: list[CalendarEvent], now: datetime) -> list[GateStatus]:
        out: list[GateStatus] = []
        for gate in self.plan.event_gates:
            # Look ahead AND back: an event that already released is no longer
            # blocking once we're past the after-window.
            matches = self.calendar.find_matching(
                events,
                title_contains=gate.title_contains,
                country=gate.country,
                impact=gate.impact,
            )
            relevant = None
            window_start = None
            window_end = None
            state = "no_match"

            for ev in matches:
                w_start = ev.when - timedelta(hours=gate.block_before_hours)
                w_end = ev.when + timedelta(hours=gate.block_after_hours)
                if w_start <= now <= w_end:
                    relevant = ev
                    window_start, window_end = w_start, w_end
                    state = "blocked"
                    break
                if ev.when > now and relevant is None:
                    relevant = ev               # next upcoming match (informational)
                    window_start, window_end = w_start, w_end
                    state = "clear"

            out.append(GateStatus(
                gate_title=f"{gate.country} — {gate.title_contains} ({gate.impact})",
                matched_event=relevant,
                state=state,
                block_window_start=window_start,
                block_window_end=window_end,
                note=gate.note,
            ))
        return out

    def _any_gate_blocked(self, gates: list[GateStatus]) -> bool:
        return any(g.state == "blocked" for g in gates)

    # ── Entry evaluation ────────────────────────────────────────────────────

    def _evaluate_entries(self, cycle: Cycle) -> None:
        if cycle.spot is None:
            cycle.entry_readiness.append(
                EntryReadiness(entry_id="(all)", state="waiting", reason="no spot quote")
            )
            return

        spot = cycle.spot.price
        gated = self._any_gate_blocked(cycle.gates)

        for entry in self.plan.entries:
            readiness = self._entry_readiness(entry, spot, cycle.last_daily, gated, cycle)
            cycle.entry_readiness.append(readiness)

            if readiness.state == "armed" and readiness.would_fill_at is not None:
                self._fill_entry(entry, readiness.would_fill_at, cycle)
                return                  # one entry per cycle

    def _entry_readiness(
        self,
        entry: Entry,
        spot: float,
        last_daily: DailyBar | None,
        gated: bool,
        cycle: Cycle,
    ) -> EntryReadiness:
        if entry.type == "limit_zone":
            assert entry.zone is not None
            lo, hi = entry.zone
            if not (lo <= spot <= hi):
                return EntryReadiness(
                    entry.id, "waiting",
                    f"Spot {spot:.4f} outside zone {lo:.4f}–{hi:.4f}."
                )
            if gated:
                return EntryReadiness(
                    entry.id, "gated",
                    f"Spot in zone {lo:.4f}–{hi:.4f} but event gate is blocking new entries."
                )
            return EntryReadiness(entry.id, "armed", "Spot in zone; gates clear.", would_fill_at=spot)

        if entry.type == "breakout_retest":
            assert entry.breakout_level is not None
            assert entry.retest_tolerance_pips is not None
            # Check if any recent daily close was above the breakout level.
            try:
                bars = self.prices.daily_bars(self.plan.yfinance_ticker, lookback_days=15)
            except Exception as e:
                return EntryReadiness(
                    entry.id, "waiting",
                    f"Could not fetch daily bars for breakout check: {e}"
                )
            confirmed = any(b.close > entry.breakout_level for b in bars[-10:])
            if not confirmed:
                return EntryReadiness(
                    entry.id, "waiting",
                    f"No daily close above {entry.breakout_level:.4f} in last 10 sessions."
                )
            # Retest: spot within tolerance of breakout level (from above).
            tol = entry.retest_tolerance_pips * PIP_SIZE_DEFAULT
            if spot < entry.breakout_level - tol:
                return EntryReadiness(
                    entry.id, "waiting",
                    f"Breakout confirmed but spot {spot:.4f} fell below retest window "
                    f"({entry.breakout_level - tol:.4f}–{entry.breakout_level + tol:.4f})."
                )
            if spot > entry.breakout_level + tol:
                return EntryReadiness(
                    entry.id, "waiting",
                    f"Breakout confirmed but spot {spot:.4f} still above retest window — "
                    f"wait for pullback to {entry.breakout_level:.4f}±{entry.retest_tolerance_pips}p."
                )
            if gated:
                return EntryReadiness(
                    entry.id, "gated",
                    f"Retest in window but event gate is blocking new entries."
                )
            return EntryReadiness(entry.id, "armed", "Breakout confirmed; spot in retest window.", would_fill_at=spot)

        return EntryReadiness(entry.id, "invalid", f"Unknown entry type: {entry.type}")

    # ── Fill / position management ──────────────────────────────────────────

    def _fill_entry(self, entry: Entry, fill_price: float, cycle: Cycle) -> None:
        # Need quote→USD up front: sizing depends on it for any non-xxxUSD pair.
        quote_to_usd = self._quote_to_usd(cycle)
        if quote_to_usd is None:
            cycle.actions.append(Action(
                "noop",
                f"Entry {entry.id} armed but quote→USD rate unavailable; "
                f"refusing to fill (would size incorrectly). Will retry next cycle."
            ))
            return

        # Pick sizing pct: standalone vs correlated
        any_correlated_open = any(
            self.ledger.state.correlated_open.get(sym.upper(), False)
            for sym in self.plan.sizing.correlated_symbols
        )
        risk_pct = (
            self.plan.sizing.correlated_risk_pct
            if any_correlated_open
            else self.plan.sizing.standalone_risk_pct
        )
        risk_usd = self.plan.account.size_usd * (risk_pct / 100.0)

        stop_price = self._initial_stop_for(entry)
        if stop_price is None:
            cycle.actions.append(Action(
                "noop",
                f"Entry {entry.id} armed but has no defined initial stop — refusing to fill."
            ))
            return

        stop_dist = abs(fill_price - stop_price)
        if stop_dist <= 0:
            cycle.actions.append(Action(
                "noop",
                f"Entry {entry.id} stop distance is zero — refusing to fill."
            ))
            return
        # Units sizing — correct for every FX pair:
        #   per-unit risk in USD = stop_dist (in quote ccy) * quote_to_usd
        #   units                = risk_usd / per-unit-risk-in-USD
        size_units = risk_usd / (stop_dist * quote_to_usd)

        sizing_label = "correlated" if any_correlated_open else "standalone"
        pos = self.ledger.open_position(
            symbol=self.plan.symbol,
            direction=self.plan.direction,
            entry_id=entry.id,
            entry_price=fill_price,
            size_units=size_units,
            risk_usd=risk_usd,
            stop_price=stop_price,
            note=(
                f"{sizing_label} sizing @ {risk_pct:.3f}% -> ${risk_usd:.2f} risk; "
                f"stop dist {stop_dist:.4f} quote; quote_to_usd={quote_to_usd:.4f}"
            ),
        )
        cycle.actions.append(Action(
            "opened",
            f"OPEN {self.plan.direction.upper()} {self.plan.symbol} @ {fill_price:.4f} "
            f"({sizing_label} sizing, ${risk_usd:.2f} risk, "
            f"{size_units:,.0f} units, stop {stop_price:.4f})"
        ))
        cycle.position_snapshot = pos

    def _initial_stop_for(self, entry: Entry) -> float | None:
        if entry.type == "limit_zone":
            return entry.stop
        if entry.type == "breakout_retest":
            return entry.stop_on_daily_close_below
        return None

    def _manage_open_position(self, cycle: Cycle) -> None:
        pos = self.ledger.state.position
        assert pos is not None
        cycle.position_snapshot = pos

        if cycle.spot is None:
            cycle.notes.append("Position open but no spot quote — cannot evaluate stops/targets this cycle.")
            return

        spot = cycle.spot.price

        # Quote->USD rate gates all P&L-realizing actions. Without it we can't
        # stamp a correct USD P&L on fills, so we refuse to close/partial this
        # cycle. The position stays put; we retry next tick. Stops not firing
        # for one cycle is preferable to filling with bogus P&L.
        quote_to_usd = self._quote_to_usd(cycle)
        if quote_to_usd is None:
            cycle.notes.append(
                "Position open but quote→USD rate unavailable; deferring stop/target "
                "evaluation to next cycle to avoid stamping wrong P&L."
            )
            return

        # Unrealized P&L (informational)
        cycle.unrealized_pnl_usd = unrealized_pnl_usd(pos, spot, quote_to_usd)

        # 1. Stop check (initial stop = daily close; trailed stop = intraday)
        is_trailed = any(f.kind == "trail" for f in pos.fills)
        if is_trailed:
            stop_hit = (pos.direction == "long" and spot <= pos.stop_price) or \
                       (pos.direction == "short" and spot >= pos.stop_price)
            if stop_hit:
                self.ledger.close_position(
                    price=pos.stop_price,
                    quote_to_usd=quote_to_usd,
                    kind="stop",
                    note=f"Trailed stop hit intraday at {pos.stop_price:.4f}."
                )
                cycle.actions.append(Action(
                    "closed",
                    f"STOP (trailed) at {pos.stop_price:.4f}, realized ${pos.realized_pnl_usd:.2f}"
                ))
                return
        else:
            if cycle.last_daily is not None:
                daily_close = cycle.last_daily.close
                stop_hit = (pos.direction == "long" and daily_close < pos.stop_price) or \
                           (pos.direction == "short" and daily_close > pos.stop_price)
                if stop_hit:
                    self.ledger.close_position(
                        price=daily_close,
                        quote_to_usd=quote_to_usd,
                        kind="stop",
                        note=f"Initial stop hit on daily close {daily_close:.4f} ({cycle.last_daily.date})."
                    )
                    cycle.actions.append(Action(
                        "closed",
                        f"STOP (daily close) at {daily_close:.4f}, "
                        f"realized ${pos.realized_pnl_usd:.2f}"
                    ))
                    return
                # Informational only: spot below stop intraday but daily not yet closed.
                if pos.direction == "long" and spot < pos.stop_price:
                    cycle.notes.append(
                        f"Spot {spot:.4f} is intraday below initial stop {pos.stop_price:.4f} — "
                        f"awaiting daily close to confirm."
                    )

        # 2. Target check (intraday touch, in order)
        for target in self.plan.targets:
            if self._target_already_taken(pos, target):
                continue
            hit = (pos.direction == "long" and spot >= target.level) or \
                  (pos.direction == "short" and spot <= target.level)
            if not hit:
                # Targets are checked in order — if first pending is not hit, stop.
                break
            # Take the partial/full
            if target.fraction >= pos.open_fraction - 1e-9:
                # Closes the whole remaining position
                self.ledger.close_position(
                    price=target.level,
                    quote_to_usd=quote_to_usd,
                    kind="full",
                    note=f"Target {target.id} @ {target.level:.4f} closed remainder."
                )
                cycle.actions.append(Action(
                    "closed",
                    f"TARGET {target.id} HIT at {target.level:.4f}, position closed, "
                    f"realized ${self.ledger.state.closed_positions[-1].realized_pnl_usd:.2f}"
                ))
                return
            self.ledger.take_partial(
                price=target.level,
                fraction=target.fraction,
                quote_to_usd=quote_to_usd,
                note=f"Target {target.id} @ {target.level:.4f}",
            )
            cycle.actions.append(Action(
                "partial",
                f"PARTIAL {target.id} at {target.level:.4f}: took {target.fraction:.0%} "
                f"of position, realized so far ${pos.realized_pnl_usd:.2f}"
            ))
            # Trail stop if specified
            if target.on_fill.trail_stop_to is not None:
                trail_to = target.on_fill.trail_stop_to
                new_stop = pos.entry_price if trail_to == "entry" else float(trail_to)
                self.ledger.update_stop(
                    new_stop, note=f"Trailed to {new_stop:.4f} on {target.id}"
                )
                cycle.actions.append(Action(
                    "trail",
                    f"Trailed stop to {new_stop:.4f} (was {pos.stop_price:.4f})"
                ))
            return  # one action per cycle

    def _target_already_taken(self, pos: Position, target: Target) -> bool:
        return any(target.id in (f.note or "") and f.kind in ("partial", "full") for f in pos.fills)

    # ── Finalize ────────────────────────────────────────────────────────────

    def _finalize(self, cycle: Cycle) -> Cycle:
        # Record release facts for any event we now have an "actual" on, so the
        # CPI print is captured even if the engine cycles after the event window.
        seen = {(e["title"], e["when"]) for e in self.ledger.state.observed_events}
        for ev in cycle.upcoming_events:
            if ev.is_released and (ev.title, ev.when.isoformat()) not in seen:
                self.ledger.record_event({
                    "title": ev.title,
                    "country": ev.country,
                    "when": ev.when.isoformat(),
                    "impact": ev.impact,
                    "actual": ev.actual,
                    "forecast": ev.forecast,
                    "previous": ev.previous,
                })
        self.notifier.emit(cycle)
        return cycle
