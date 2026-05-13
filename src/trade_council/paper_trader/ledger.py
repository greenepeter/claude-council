"""Paper-position ledger — persistent state stored as JSON.

A ledger holds:
  * the open simulated position for a plan (if any)
  * the fill history (entry, partial, full, stop)
  * external positions the user told us about (correlated_symbols presence flag)

The file is the source of truth. The engine reads/writes it on every cycle.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json


@dataclass
class Fill:
    kind: str                       # "entry" | "partial" | "full" | "stop" | "trail"
    when: str                       # ISO-8601 UTC
    price: float
    fraction: float                 # fraction of the original position that this fill affected
    note: str = ""
    pnl_usd: float = 0.0            # realized USD P&L at this fill (0.0 for entry/trail)
    quote_to_usd_rate: float = 0.0  # the quote→USD rate used to stamp pnl_usd; 0.0 if N/A


@dataclass
class Position:
    plan_id: str
    symbol: str
    direction: str                  # "long" | "short"
    entry_id: str                   # which Entry rule produced the fill
    entry_price: float
    entry_when: str                 # ISO-8601 UTC
    initial_size_units: float       # base-currency units at open (e.g. EUR for EUR/USD)
    initial_risk_usd: float         # account-currency $ risk at open
    stop_price: float
    open_fraction: float = 1.0      # remaining fraction of initial size
    fills: list[Fill] = field(default_factory=list)
    closed: bool = False
    realized_pnl_usd: float = 0.0

    def remaining_units(self) -> float:
        return self.initial_size_units * self.open_fraction


@dataclass
class LedgerState:
    plan_id: str
    started: str                    # ISO-8601 UTC
    last_updated: str               # ISO-8601 UTC
    position: Position | None = None
    closed_positions: list[Position] = field(default_factory=list)
    # User-declared open positions in correlated instruments (toggled via CLI).
    # The engine reads this to pick standalone vs correlated sizing.
    correlated_open: dict[str, bool] = field(default_factory=dict)
    # Event-release facts the engine has observed (e.g. CPI actual vs forecast).
    observed_events: list[dict[str, Any]] = field(default_factory=list)


class Ledger:
    """JSON-backed paper ledger. Loaded on init, saved on every mutation."""

    def __init__(self, path: Path, plan_id: str):
        self.path = path
        self.plan_id = plan_id
        self.state: LedgerState = self._load_or_init()

    # ── Loading / saving ───────────────────────────────────────────────────

    def _load_or_init(self) -> LedgerState:
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if raw.get("plan_id") != self.plan_id:
                raise RuntimeError(
                    f"Ledger at {self.path} belongs to plan_id={raw.get('plan_id')!r}, "
                    f"but this run is for plan_id={self.plan_id!r}. "
                    f"Use a different --ledger path, or delete the old file."
                )
            return _state_from_dict(raw)

        now = _utc_now()
        state = LedgerState(plan_id=self.plan_id, started=now, last_updated=now)
        self._write(state)
        return state

    def _write(self, state: LedgerState) -> None:
        state.last_updated = _utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(_state_to_dict(state), f, indent=2, default=str)
        tmp.replace(self.path)

    def save(self) -> None:
        self._write(self.state)

    # ── Position lifecycle ─────────────────────────────────────────────────

    def open_position(
        self,
        *,
        symbol: str,
        direction: str,
        entry_id: str,
        entry_price: float,
        size_units: float,
        risk_usd: float,
        stop_price: float,
        note: str = "",
    ) -> Position:
        if self.state.position is not None and not self.state.position.closed:
            raise RuntimeError("Position already open — refusing to open another (no_pyramiding).")
        now = _utc_now()
        pos = Position(
            plan_id=self.plan_id,
            symbol=symbol,
            direction=direction,
            entry_id=entry_id,
            entry_price=entry_price,
            entry_when=now,
            initial_size_units=size_units,
            initial_risk_usd=risk_usd,
            stop_price=stop_price,
            fills=[Fill(kind="entry", when=now, price=entry_price, fraction=1.0, note=note)],
        )
        self.state.position = pos
        self.save()
        return pos

    def take_partial(
        self,
        price: float,
        fraction: float,
        quote_to_usd: float,
        note: str = "",
    ) -> float:
        """Realize a partial. Returns the USD P&L stamped onto this fill."""
        pos = self._require_open()
        if fraction <= 0 or fraction > pos.open_fraction:
            raise ValueError(
                f"Bad partial fraction: requested {fraction}, currently open {pos.open_fraction}."
            )
        closed_units = pos.initial_size_units * fraction
        pnl = _pnl_usd(pos.direction, pos.entry_price, price, closed_units, quote_to_usd)
        pos.realized_pnl_usd += pnl
        pos.open_fraction -= fraction
        pos.fills.append(
            Fill(
                kind="partial", when=_utc_now(), price=price, fraction=fraction,
                note=note, pnl_usd=pnl, quote_to_usd_rate=quote_to_usd,
            )
        )
        self.save()
        return pnl

    def close_position(
        self,
        price: float,
        quote_to_usd: float,
        kind: str = "full",
        note: str = "",
    ) -> float:
        """Close remaining position. Returns the USD P&L stamped onto this fill."""
        pos = self._require_open()
        closed_units = pos.initial_size_units * pos.open_fraction
        pnl = _pnl_usd(pos.direction, pos.entry_price, price, closed_units, quote_to_usd)
        pos.realized_pnl_usd += pnl
        pos.fills.append(
            Fill(
                kind=kind, when=_utc_now(), price=price, fraction=pos.open_fraction,
                note=note, pnl_usd=pnl, quote_to_usd_rate=quote_to_usd,
            )
        )
        pos.open_fraction = 0.0
        pos.closed = True
        self.state.closed_positions.append(pos)
        self.state.position = None
        self.save()
        return pnl

    def update_stop(self, new_stop: float, note: str = "") -> None:
        pos = self._require_open()
        pos.stop_price = new_stop
        pos.fills.append(
            Fill(kind="trail", when=_utc_now(), price=new_stop, fraction=0.0, note=note)
        )
        self.save()

    def record_event(self, event_dict: dict[str, Any]) -> None:
        self.state.observed_events.append(event_dict)
        self.save()

    def set_correlated_open(self, symbol: str, is_open: bool) -> None:
        self.state.correlated_open[symbol.upper()] = is_open
        self.save()

    def has_open_position(self) -> bool:
        return self.state.position is not None and not self.state.position.closed

    def _require_open(self) -> Position:
        if not self.has_open_position():
            raise RuntimeError("No open position.")
        return self.state.position  # type: ignore[return-value]


# ── Helpers ─────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pnl_usd(
    direction: str,
    entry: float,
    exit_price: float,
    units: float,
    quote_to_usd: float,
) -> float:
    """USD P&L on `units` base-currency units of an FX position.

    The math is identical for every pair (xxxUSD, USDxxx, cross):

        pnl_quote = (exit - entry) * units   (long)
                  = (entry - exit) * units   (short)
        pnl_usd   = pnl_quote * quote_to_usd

    where `quote_to_usd` is the USD value of 1 unit of the QUOTE currency at
    the time of the fill (caller supplies — see PriceSource.quote_currency_to_usd).

    For xxxUSD pairs (EUR/USD, GBP/USD, AUD/USD, ...) the quote currency IS USD
    so `quote_to_usd = 1.0`. For USD/JPY, JPY's USD rate is ~1/155 ≈ 0.0065.
    For EUR/GBP, GBP's USD rate is ~1.27. For AUD/CAD, CAD's USD rate is ~0.73.

    Callers MUST pass `quote_to_usd`; defaulting it would mask the bug class
    that this rewrite exists to fix.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if quote_to_usd <= 0:
        raise ValueError(f"quote_to_usd must be positive, got {quote_to_usd!r}")
    diff = (exit_price - entry) if direction == "long" else (entry - exit_price)
    return diff * units * quote_to_usd


def unrealized_pnl_usd(position: Position, spot: float, quote_to_usd: float) -> float:
    """Public helper for callers (e.g. show_trades.py) that need to compute
    mark-to-market P&L on the current open position."""
    return _pnl_usd(
        position.direction,
        position.entry_price,
        spot,
        position.remaining_units(),
        quote_to_usd,
    )


def quote_currency_of(symbol: str) -> str:
    """Extract the quote currency from a 6-letter FX symbol.

    'EURUSD' -> 'USD'      'eur/usd' -> 'USD'      'EURUSD=X' -> 'USD'
    'EURGBP' -> 'GBP'      'USDJPY'  -> 'JPY'      'AUDCAD'   -> 'CAD'
    """
    sym = symbol.upper().replace("/", "").replace("=X", "").strip()
    if len(sym) != 6:
        raise ValueError(
            f"Cannot extract quote currency from {symbol!r}: "
            f"expected a 6-letter FX pair after stripping separators, got {sym!r}"
        )
    return sym[3:6]


def _state_to_dict(state: LedgerState) -> dict[str, Any]:
    return {
        "plan_id": state.plan_id,
        "started": state.started,
        "last_updated": state.last_updated,
        "position": asdict(state.position) if state.position else None,
        "closed_positions": [asdict(p) for p in state.closed_positions],
        "correlated_open": state.correlated_open,
        "observed_events": state.observed_events,
    }


def _state_from_dict(raw: dict[str, Any]) -> LedgerState:
    pos_raw = raw.get("position")
    pos = _position_from_dict(pos_raw) if pos_raw else None
    closed = [_position_from_dict(p) for p in raw.get("closed_positions", [])]
    return LedgerState(
        plan_id=raw["plan_id"],
        started=raw["started"],
        last_updated=raw["last_updated"],
        position=pos,
        closed_positions=closed,
        correlated_open=raw.get("correlated_open", {}),
        observed_events=raw.get("observed_events", []),
    )


def _position_from_dict(raw: dict[str, Any]) -> Position:
    fills_raw = raw.get("fills", [])
    fills = []
    for f in fills_raw:
        # Forward-compat: tolerate fills written before pnl_usd / quote_to_usd_rate
        # were added to the Fill dataclass.
        f.setdefault("pnl_usd", 0.0)
        f.setdefault("quote_to_usd_rate", 0.0)
        fills.append(Fill(**f))
    data = {k: v for k, v in raw.items() if k != "fills"}
    return Position(**data, fills=fills)
