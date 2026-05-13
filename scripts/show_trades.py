#!/usr/bin/env python
"""Summarize every paper-traded plan: enrollment, open positions, closed P&L.

Offline by default — reads the registry and per-plan ledgers, no network. Use
`--with-unrealized` to fetch current spot for plans with an open position so
unrealized P&L is shown alongside realized.

Examples:

    # All plans, compact
    python scripts/show_trades.py

    # All plans + live unrealized P&L (one yfinance call per open position)
    python scripts/show_trades.py --with-unrealized

    # Drill into one plan: full fill history
    python scripts/show_trades.py --plan eurgbp_2026_05_swing_short --detail
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# UTF-8 stdio (Windows console compat)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

from trade_council.paper_trader.registry import (  # noqa: E402
    PaperTradingRegistry, default_registry_path, RegistryEntry,
)
from trade_council.paper_trader.ledger import (  # noqa: E402
    Ledger, Position, unrealized_pnl_usd, quote_currency_of,
)


def _runtime_paths(project_root: Path, plan_id: str) -> tuple[Path, Path]:
    runtime = project_root / "runtime"
    return runtime / f"{plan_id}_ledger.json", runtime / f"{plan_id}_alerts.log"


def _read_ledger(project_root: Path, plan_id: str) -> Ledger | None:
    ledger_path, _ = _runtime_paths(project_root, plan_id)
    if not ledger_path.exists():
        return None
    try:
        return Ledger(ledger_path, plan_id=plan_id)
    except Exception as e:
        print(f"[show_trades] could not load ledger for {plan_id}: {e}", file=sys.stderr)
        return None


def _fmt_money(x: float) -> str:
    sign = "" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def _money_text(x: float) -> Text:
    color = "green" if x > 0 else ("red" if x < 0 else "white")
    return Text(_fmt_money(x), style=color)


def _status_text(status: str) -> Text:
    color = {
        "active": "green",
        "expired": "yellow",
        "invalidated": "red",
        "closed": "dim",
        "errored": "bold red",
    }.get(status, "white")
    return Text(status, style=color)


def _fetch_spot_and_rate(symbol: str) -> tuple[float | None, float | None]:
    """Best-effort fetch of (spot, quote_to_usd) for a symbol. (None, None) on failure.

    Returns the live spot price AND the USD value of 1 unit of the symbol's
    quote currency, both needed to compute correct unrealized P&L for cross
    or USDxxx pairs.
    """
    try:
        from trade_council.paper_trader.data.prices import PriceSource
        sym = symbol.upper()
        ticker = sym if sym.endswith("=X") else f"{sym}=X"
        ps = PriceSource()
        spot = ps.quote(ticker).price
        rate = ps.quote_currency_to_usd(quote_currency_of(symbol))
        return spot, rate
    except Exception:
        return None, None


def _render_plans_table(c: Console, entries: list[RegistryEntry]) -> None:
    tbl = Table(title=f"Plans ({len(entries)})", title_justify="left",
                show_edge=False, pad_edge=False)
    tbl.add_column("plan_id")
    tbl.add_column("symbol")
    tbl.add_column("dir")
    tbl.add_column("status")
    tbl.add_column("last cycle (UTC)")
    tbl.add_column("last summary", overflow="fold")
    tbl.add_column("error", overflow="fold")
    for e in entries:
        tbl.add_row(
            e.plan_id, e.symbol, e.direction or "—",
            _status_text(e.status),
            (e.last_cycle_at or "—").replace("T", " ").replace("+00:00", ""),
            e.last_cycle_summary or "—",
            (e.last_error or "")[:80],
        )
    c.print(tbl)


def _render_open_positions(
    c: Console, entries: list[RegistryEntry], project_root: Path, with_unrealized: bool,
) -> tuple[int, float]:
    """Returns (count, total_unrealized_usd)."""
    rows = []
    total_unrealized = 0.0
    have_unrealized = False
    for e in entries:
        ledger = _read_ledger(project_root, e.plan_id)
        if ledger is None or ledger.state.position is None:
            continue
        pos = ledger.state.position
        spot, rate = (_fetch_spot_and_rate(pos.symbol) if with_unrealized else (None, None))
        unrealized = None
        if spot is not None and rate is not None:
            try:
                unrealized = unrealized_pnl_usd(pos, spot, rate)
                total_unrealized += unrealized
                have_unrealized = True
            except Exception:
                unrealized = None
        rows.append((e, pos, spot, unrealized))

    if not rows:
        c.print("[dim]Open positions: (none)[/dim]")
        return 0, 0.0

    tbl = Table(title=f"Open positions ({len(rows)})", title_justify="left",
                show_edge=False, pad_edge=False)
    tbl.add_column("plan_id")
    tbl.add_column("entry")
    tbl.add_column("size (units)")
    tbl.add_column("stop")
    tbl.add_column("frac")
    tbl.add_column("realized")
    if have_unrealized or with_unrealized:
        tbl.add_column("spot")
        tbl.add_column("unrealized")
    for e, pos, spot, unrealized in rows:
        entry_str = f"{pos.direction.upper()} {pos.symbol} @ {pos.entry_price:.5f} ({pos.entry_id})"
        row = [
            e.plan_id, entry_str,
            f"{pos.remaining_units():,.0f}",
            f"{pos.stop_price:.5f}",
            f"{pos.open_fraction:.0%}",
            _money_text(pos.realized_pnl_usd),
        ]
        if have_unrealized or with_unrealized:
            row.append(f"{spot:.5f}" if spot is not None else "[dim]n/a[/dim]")
            row.append(_money_text(unrealized) if unrealized is not None else Text("n/a", style="dim"))
        tbl.add_row(*row)
    c.print(tbl)
    return len(rows), total_unrealized


def _render_closed_positions(
    c: Console, entries: list[RegistryEntry], project_root: Path, detail: bool,
) -> tuple[int, float]:
    rows: list[tuple[RegistryEntry, Position]] = []
    for e in entries:
        ledger = _read_ledger(project_root, e.plan_id)
        if ledger is None:
            continue
        for pos in ledger.state.closed_positions:
            rows.append((e, pos))

    if not rows:
        c.print("[dim]Closed positions: (none)[/dim]")
        return 0, 0.0

    total = sum(pos.realized_pnl_usd for _, pos in rows)
    tbl = Table(
        title=f"Closed positions ({len(rows)} total · realized {_fmt_money(total)})",
        title_justify="left", show_edge=False, pad_edge=False,
    )
    tbl.add_column("plan_id")
    tbl.add_column("entry")
    tbl.add_column("when opened")
    tbl.add_column("realized")
    tbl.add_column("fills")
    for e, pos in rows:
        when = pos.entry_when.replace("T", " ").replace("+00:00", "")
        entry_str = f"{pos.direction.upper()} {pos.symbol} @ {pos.entry_price:.5f}"
        fills_str = ",".join(f.kind for f in pos.fills)
        tbl.add_row(e.plan_id, entry_str, when, _money_text(pos.realized_pnl_usd), fills_str)
    c.print(tbl)

    if detail:
        for e, pos in rows:
            c.rule(f"{e.plan_id} :: {pos.direction.upper()} {pos.symbol} @ {pos.entry_price:.5f}")
            for f in pos.fills:
                c.print(f"  [{f.kind:>8s}] @ {f.price:.5f}  frac={f.fraction:.2f}  {f.when}  {f.note}")
    return len(rows), total


def _render_alerts_log_summary(c: Console, entries: list[RegistryEntry], project_root: Path) -> None:
    tbl = Table(title="Alerts logs", title_justify="left", show_edge=False, pad_edge=False)
    tbl.add_column("plan_id")
    tbl.add_column("path")
    tbl.add_column("lines")
    tbl.add_column("last modified (UTC)")
    for e in entries:
        _, log_path = _runtime_paths(project_root, e.plan_id)
        if not log_path.exists():
            tbl.add_row(e.plan_id, str(log_path.relative_to(project_root)), "[dim]none[/dim]", "—")
            continue
        try:
            with log_path.open("r", encoding="utf-8") as f:
                n = sum(1 for _ in f)
        except Exception:
            n = -1
        mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone.utc)
        tbl.add_row(
            e.plan_id,
            str(log_path.relative_to(project_root)),
            str(n),
            mtime.strftime("%Y-%m-%d %H:%M:%S"),
        )
    c.print(tbl)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Summarize all paper-traded plans.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--plan", help="Filter to a single plan_id.")
    p.add_argument(
        "--status", default=None,
        help="Filter by registry status (active | expired | invalidated | closed | errored).",
    )
    p.add_argument(
        "--with-unrealized", action="store_true",
        help="Fetch current spot for each open position to show unrealized P&L (one yfinance call per position).",
    )
    p.add_argument("--detail", action="store_true",
                   help="Show full fill history for each closed position.")
    p.add_argument("--no-logs", action="store_true",
                   help="Skip the alerts-log summary section.")
    args = p.parse_args()

    c = Console()
    project_root = PROJECT_ROOT
    reg_path = default_registry_path(project_root)

    if not reg_path.exists():
        c.print("[yellow]No paper-trading registry yet.[/yellow]  "
                "Run a debate or `python scripts/run_autonomous.py` to populate it.")
        return 0

    registry = PaperTradingRegistry(reg_path, project_root)
    entries = registry.list_all()
    if args.status:
        entries = [e for e in entries if e.status == args.status]
    if args.plan:
        entries = [e for e in entries if e.plan_id == args.plan]
    # Sort: active first, then by symbol
    status_order = {"active": 0, "errored": 1, "expired": 2, "invalidated": 3, "closed": 4}
    entries.sort(key=lambda e: (status_order.get(e.status, 9), e.symbol, e.plan_id))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    c.rule(f"[bold]trade_council :: paper trades[/bold] · {now}")

    if not entries:
        c.print("[dim]No plans match the given filters.[/dim]")
        return 0

    _render_plans_table(c, entries)
    c.print()
    n_open, unrealized_total = _render_open_positions(
        c, entries, project_root, with_unrealized=args.with_unrealized,
    )
    c.print()
    n_closed, realized_total = _render_closed_positions(
        c, entries, project_root, detail=args.detail,
    )

    c.print()
    c.rule("[bold]Roll-up[/bold]")
    c.print(f"  plans:               {len(entries)} shown "
            f"({sum(1 for e in entries if e.status == 'active')} active)")
    c.print(f"  open positions:      {n_open}")
    c.print(f"  closed positions:    {n_closed}")
    c.print(f"  realized P&L:        ", end="")
    c.print(_money_text(realized_total))
    if args.with_unrealized and n_open > 0:
        c.print(f"  unrealized P&L:      ", end="")
        c.print(_money_text(unrealized_total))
        c.print(f"  total (real+unreal): ", end="")
        c.print(_money_text(realized_total + unrealized_total))

    if not args.no_logs:
        c.print()
        _render_alerts_log_summary(c, entries, project_root)

    return 0


if __name__ == "__main__":
    sys.exit(main())
