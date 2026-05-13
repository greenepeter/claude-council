"""Notifier — turns a `Cycle` into a console panel + line in a log file.

v1 is local-only: console (via rich) + append-only `alerts.log`. v2 hooks for
desktop toast, email, Slack, Pushover are intentionally not built yet — the
abstract Notifier.emit() boundary makes them drop-ins.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from trade_council.paper_trader.engine import Cycle


class Notifier:
    def __init__(self, log_path: Path | None = None, console: Console | None = None, quiet: bool = False):
        self.log_path = log_path
        self.console = console or Console()
        self.quiet = quiet

    def emit(self, cycle: "Cycle") -> None:
        if not self.quiet:
            self._print_console(cycle)
        if self.log_path is not None:
            self._append_log(cycle)

    # ── Console rendering ───────────────────────────────────────────────────

    def _print_console(self, cycle: "Cycle") -> None:
        c = self.console

        # Header
        ts = cycle.when.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        c.rule(f"[bold]paper_trader[/bold] · {cycle.plan_id} · {ts}")

        # Top row: spot + plan state
        spot_str = f"{cycle.spot.price:.5f}" if cycle.spot else "[red]no quote[/red]"
        spot_age = ""
        if cycle.spot:
            age = (datetime.now(timezone.utc) - cycle.spot.as_of).total_seconds()
            spot_age = f" (as of {age:.0f}s ago, {cycle.spot.source})"
        daily_str = (
            f"{cycle.last_daily.close:.5f} on {cycle.last_daily.date}"
            if cycle.last_daily else "[dim]n/a[/dim]"
        )
        state_color = {"active": "green", "invalidated": "red", "expired": "yellow"}.get(
            cycle.plan_state, "white"
        )
        c.print(
            f"  spot: [bold]{spot_str}[/bold]{spot_age}    "
            f"last daily close: {daily_str}    "
            f"plan: [{state_color}]{cycle.plan_state}[/{state_color}]"
        )

        # Gates
        if cycle.gates:
            tbl = Table(title="event gates", title_justify="left", show_edge=False, pad_edge=False)
            tbl.add_column("gate")
            tbl.add_column("state")
            tbl.add_column("next event")
            tbl.add_column("when (local)")
            tbl.add_column("forecast / actual")
            for g in cycle.gates:
                state_style = {
                    "blocked": "[red]BLOCKED[/red]",
                    "clear":   "[green]clear[/green]",
                    "no_match":"[dim]no match[/dim]",
                }.get(g.state, g.state)
                ev = g.matched_event
                when_local = ev.when.astimezone().strftime("%Y-%m-%d %H:%M") if ev else "—"
                fc_act = (
                    f"f:{ev.forecast or '—'}  a:{ev.actual or '[dim]pending[/dim]'}"
                    if ev else "—"
                )
                tbl.add_row(g.gate_title, state_style, ev.title if ev else "—", when_local, fc_act)
            c.print(tbl)

        # Position OR entry readiness
        if cycle.position_snapshot is not None:
            self._render_position(cycle)
        elif cycle.entry_readiness:
            tbl = Table(title="entry readiness", title_justify="left", show_edge=False, pad_edge=False)
            tbl.add_column("entry")
            tbl.add_column("state")
            tbl.add_column("reason")
            for r in cycle.entry_readiness:
                state_style = {
                    "armed":   "[bold green]ARMED[/bold green]",
                    "waiting": "[dim]waiting[/dim]",
                    "gated":   "[yellow]gated[/yellow]",
                    "invalid": "[red]invalid[/red]",
                }.get(r.state, r.state)
                tbl.add_row(r.entry_id, state_style, r.reason)
            c.print(tbl)

        # Actions
        for action in cycle.actions:
            color = {
                "opened":      "bold green",
                "partial":     "cyan",
                "closed":      "bold magenta",
                "trail":       "blue",
                "gated":       "yellow",
                "invalidated": "bold red",
                "noop":        "dim",
            }.get(action.kind, "white")
            c.print(Panel(Text(action.detail), title=action.kind.upper(), style=color))

        # Notes
        for note in cycle.notes:
            c.print(f"  [dim]note:[/dim] {note}")

    def _render_position(self, cycle: "Cycle") -> None:
        pos = cycle.position_snapshot
        assert pos is not None
        spot = cycle.spot.price if cycle.spot else pos.entry_price
        pnl = cycle.unrealized_pnl_usd or 0.0
        pnl_color = "green" if pnl >= 0 else "red"
        tbl = Table(title="open position", title_justify="left", show_edge=False, pad_edge=False)
        tbl.add_column("field")
        tbl.add_column("value")
        tbl.add_row("entry", f"{pos.direction.upper()} {pos.symbol} @ {pos.entry_price:.5f}  ({pos.entry_id})")
        tbl.add_row("size", f"{pos.remaining_units():,.0f} units (initial {pos.initial_size_units:,.0f}; "
                    f"open frac {pos.open_fraction:.0%})")
        tbl.add_row("stop", f"{pos.stop_price:.5f}")
        tbl.add_row("initial risk", f"${pos.initial_risk_usd:.2f}")
        tbl.add_row("realized P&L", f"${pos.realized_pnl_usd:.2f}")
        tbl.add_row("spot vs entry", f"{spot:.5f}  (Δ {spot - pos.entry_price:+.5f})")
        tbl.add_row("unrealized P&L", f"[{pnl_color}]${pnl:.2f}[/{pnl_color}]")
        self.console.print(tbl)

    # ── File log ────────────────────────────────────────────────────────────

    def _append_log(self, cycle: "Cycle") -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = cycle.when.isoformat(timespec="seconds")
        spot = f"{cycle.spot.price:.5f}" if cycle.spot else "n/a"
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] state={cycle.plan_state} spot={spot}\n")
            for g in cycle.gates:
                ev = g.matched_event
                when = ev.when.isoformat() if ev else "—"
                f.write(f"  gate {g.gate_title!r}: {g.state}  next={ev.title if ev else '—'}@{when}\n")
            for r in cycle.entry_readiness:
                f.write(f"  entry {r.entry_id}: {r.state}  ({r.reason})\n")
            for action in cycle.actions:
                f.write(f"  ACTION[{action.kind}]: {action.detail}\n")
            for note in cycle.notes:
                f.write(f"  note: {note}\n")
            if cycle.position_snapshot is not None:
                pos = cycle.position_snapshot
                f.write(
                    f"  POSITION {pos.direction} {pos.symbol} @ {pos.entry_price:.5f} "
                    f"stop={pos.stop_price:.5f} frac={pos.open_fraction:.2f} "
                    f"realized=${pos.realized_pnl_usd:.2f} "
                    f"unrealized=${cycle.unrealized_pnl_usd or 0.0:.2f}\n"
                )
