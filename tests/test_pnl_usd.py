"""Unit tests for the unified FX P&L math in ledger._pnl_usd / unrealized_pnl_usd.

The math is intentionally a single code path for every pair shape:
    pnl_usd = (exit - entry) * units * quote_to_usd_rate    (long)
            = (entry - exit) * units * quote_to_usd_rate    (short)

For xxxUSD (quote = USD)   -> quote_to_usd_rate = 1.0
For USDxxx (base = USD)    -> quote_to_usd_rate = 1 / current_USDxxx
For crosses (e.g. EUR/GBP) -> quote_to_usd_rate = current_quoteUSD

The hand-computed expectations below pin the math: any code change that
silently shifts results will fail one of these.
"""
import math
import pytest

from trade_council.paper_trader.ledger import (
    Position, Fill, _pnl_usd, unrealized_pnl_usd, quote_currency_of,
)


def _approx(actual: float, expected: float, tol: float = 1e-6) -> bool:
    return math.isclose(actual, expected, rel_tol=tol, abs_tol=tol)


# ── quote_currency_of ───────────────────────────────────────────────────────


def test_quote_currency_of_variants():
    assert quote_currency_of("EURUSD") == "USD"
    assert quote_currency_of("eur/usd") == "USD"
    assert quote_currency_of("EURUSD=X") == "USD"
    assert quote_currency_of("USDJPY") == "JPY"
    assert quote_currency_of("EURGBP") == "GBP"
    assert quote_currency_of("AUDCAD") == "CAD"


def test_quote_currency_of_rejects_bad_input():
    with pytest.raises(ValueError):
        quote_currency_of("EUR")
    with pytest.raises(ValueError):
        quote_currency_of("EURUSDEXTRA")


# ── _pnl_usd: xxxUSD pairs ──────────────────────────────────────────────────


def test_pnl_eurusd_long_win():
    # Long 100,000 EUR/USD, entry 1.10 -> exit 1.12, USD-quoted (rate = 1.0)
    # pnl = (1.12 - 1.10) * 100_000 * 1.0 = $2,000
    pnl = _pnl_usd("long", 1.10, 1.12, 100_000, 1.0)
    assert _approx(pnl, 2_000.0)


def test_pnl_eurusd_short_win():
    # Short 100k EUR/USD, entry 1.10 -> exit 1.08, win
    pnl = _pnl_usd("short", 1.10, 1.08, 100_000, 1.0)
    assert _approx(pnl, 2_000.0)


def test_pnl_eurusd_long_loss():
    pnl = _pnl_usd("long", 1.10, 1.085, 100_000, 1.0)
    assert _approx(pnl, -1_500.0)


# ── _pnl_usd: USDxxx pairs (base = USD) ─────────────────────────────────────


def test_pnl_usdjpy_long_win():
    # Long 1,000 USD/JPY, entry 150 -> exit 155
    # quote_to_usd for JPY at exit = 1/155
    # pnl_quote = (155 - 150) * 1000 = 5000 JPY
    # pnl_usd   = 5000 * (1/155) = $32.258...
    rate_jpy_to_usd = 1.0 / 155.0
    pnl = _pnl_usd("long", 150.0, 155.0, 1_000, rate_jpy_to_usd)
    assert _approx(pnl, 5_000.0 / 155.0)


def test_pnl_usdjpy_short_win():
    # Short USD/JPY 150 -> 145, rate at exit = 1/145
    rate_jpy_to_usd = 1.0 / 145.0
    pnl = _pnl_usd("short", 150.0, 145.0, 1_000, rate_jpy_to_usd)
    assert _approx(pnl, 5_000.0 / 145.0)


# ── _pnl_usd: cross pairs ───────────────────────────────────────────────────


def test_pnl_eurgbp_long_win():
    # Long 100k EUR/GBP, 0.8600 -> 0.8650, GBPUSD = 1.27 at exit
    # pnl_gbp = (0.8650 - 0.8600) * 100_000 = 500 GBP
    # pnl_usd = 500 * 1.27 = $635
    pnl = _pnl_usd("long", 0.8600, 0.8650, 100_000, 1.27)
    assert _approx(pnl, 635.0)


def test_pnl_eurgbp_short_win():
    # Short EUR/GBP 0.8685 -> 0.8600, GBPUSD = 1.27
    # pnl_gbp = (0.8685 - 0.8600) * 100_000 = 850 GBP
    # pnl_usd = 850 * 1.27 = $1079.50
    pnl = _pnl_usd("short", 0.8685, 0.8600, 100_000, 1.27)
    assert _approx(pnl, 1_079.50)


def test_pnl_audcad_short_win():
    # Short 100k AUD/CAD, 0.9933 -> 0.9726, CADUSD = 1/USDCAD = 1/1.36 ≈ 0.7353
    # pnl_cad = (0.9933 - 0.9726) * 100_000 = 2,070 CAD
    # pnl_usd = 2070 * (1/1.36) ≈ $1,522.06
    rate_cad_to_usd = 1.0 / 1.36
    pnl = _pnl_usd("short", 0.9933, 0.9726, 100_000, rate_cad_to_usd)
    assert _approx(pnl, 2_070.0 / 1.36, tol=1e-4)


def test_pnl_audcad_long_loss():
    # Long AUD/CAD 0.9933 -> 0.9800, CADUSD = 1/1.36
    rate_cad_to_usd = 1.0 / 1.36
    pnl = _pnl_usd("long", 0.9933, 0.9800, 100_000, rate_cad_to_usd)
    expected = (0.9800 - 0.9933) * 100_000 * rate_cad_to_usd
    assert _approx(pnl, expected)


# ── input validation ────────────────────────────────────────────────────────


def test_pnl_rejects_bad_direction():
    with pytest.raises(ValueError):
        _pnl_usd("sideways", 1.0, 1.1, 100, 1.0)


def test_pnl_rejects_nonpositive_rate():
    with pytest.raises(ValueError):
        _pnl_usd("long", 1.0, 1.1, 100, 0.0)
    with pytest.raises(ValueError):
        _pnl_usd("long", 1.0, 1.1, 100, -1.0)


# ── unrealized_pnl_usd: end-to-end on a Position ────────────────────────────


def _mk_pos(direction: str, entry: float, symbol: str, units: float = 100_000) -> Position:
    return Position(
        plan_id="test_plan",
        symbol=symbol,
        direction=direction,
        entry_id="e1",
        entry_price=entry,
        entry_when="2026-05-11T00:00:00+00:00",
        initial_size_units=units,
        initial_risk_usd=1_000.0,
        stop_price=entry * 0.99 if direction == "long" else entry * 1.01,
        open_fraction=1.0,
        fills=[Fill(kind="entry", when="2026-05-11T00:00:00+00:00",
                    price=entry, fraction=1.0)],
    )


def test_unrealized_eurusd_long():
    pos = _mk_pos("long", 1.10, "EURUSD")
    assert _approx(unrealized_pnl_usd(pos, spot=1.115, quote_to_usd=1.0), 1_500.0)


def test_unrealized_eurgbp_short():
    pos = _mk_pos("short", 0.8685, "EURGBP")
    # Spot 0.8600, GBPUSD = 1.27
    # pnl_gbp = (0.8685 - 0.8600) * 100_000 = 850
    # pnl_usd = 850 * 1.27 = 1079.50
    assert _approx(unrealized_pnl_usd(pos, spot=0.8600, quote_to_usd=1.27), 1_079.50)


def test_unrealized_audcad_short():
    pos = _mk_pos("short", 0.9933, "AUDCAD")
    rate = 1.0 / 1.36
    expected = (0.9933 - 0.9726) * 100_000 * rate
    assert _approx(unrealized_pnl_usd(pos, spot=0.9726, quote_to_usd=rate), expected, tol=1e-4)


def test_unrealized_respects_open_fraction():
    pos = _mk_pos("long", 1.10, "EURUSD")
    pos.open_fraction = 0.5            # took a 50% partial earlier
    # remaining = 50k; spot 1.12 -> pnl = 0.02 * 50_000 = $1,000
    assert _approx(unrealized_pnl_usd(pos, spot=1.12, quote_to_usd=1.0), 1_000.0)
