"""Price feeds: spot quote + most-recent daily bars.

Uses yfinance under the hood — free, no auth, ~15-minute delayed for retail FX,
which is acceptable for swing-horizon paper trading. If a faster feed is added
later (Polygon, OANDA streaming, broker quote), implement the same protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# Currencies that yfinance / the market conventionally quote with USD as the
# BASE (e.g. USD/JPY = 155, not JPY/USD = 0.0064). To get "USD per 1 unit of
# currency" for these, fetch USDxxx and invert. Everything not in this set
# (EUR, GBP, AUD, NZD, ...) is quoted with USD as the QUOTE, so xxxUSD works
# directly. This list is intentionally narrow; expand as we hit new crosses.
USD_BASE_CURRENCIES = {
    "JPY", "CAD", "CHF", "MXN", "ZAR", "TRY", "CNY", "CNH",
    "HKD", "SGD", "INR", "BRL", "RUB", "SEK", "NOK", "DKK",
}


@dataclass
class DailyBar:
    date: str                       # YYYY-MM-DD in exchange-local terms (yfinance returns UTC)
    open: float
    high: float
    low: float
    close: float


@dataclass
class Quote:
    symbol: str                     # the yfinance ticker actually used
    price: float                    # last/regular-market price
    as_of: datetime                 # tz-aware UTC
    source: str = "yfinance"


class PriceSource:
    """Light wrapper around yfinance — kept thin so it's easy to swap.

    Spot is read from `fast_info.last_price` (or `info["regularMarketPrice"]`
    as fallback). Daily bars are pulled via `Ticker.history(period=..., interval="1d")`.
    """

    def __init__(self, lookback_days: int = 30):
        self.lookback_days = lookback_days
        try:
            import yfinance as yf
        except ImportError as e:
            raise RuntimeError(
                "yfinance not installed. Run: pip install -r requirements.txt"
            ) from e
        self._yf = yf

    def quote(self, ticker: str) -> Quote:
        t = self._yf.Ticker(ticker)

        price: float | None = None
        # fast_info is the cheap path. info[] is the expensive fallback.
        try:
            fi = t.fast_info
            price = float(fi["last_price"]) if "last_price" in fi else None
            if price is None and "lastPrice" in fi:
                price = float(fi["lastPrice"])
        except Exception:
            price = None

        if price is None:
            try:
                info = t.info
                price = info.get("regularMarketPrice") or info.get("previousClose")
                price = float(price) if price is not None else None
            except Exception:
                price = None

        if price is None or price <= 0:
            # Last-ditch: most recent 1m bar's close.
            hist = t.history(period="1d", interval="1m")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])

        if price is None or price <= 0:
            raise RuntimeError(f"Could not fetch quote for {ticker}")

        return Quote(symbol=ticker, price=price, as_of=datetime.now(timezone.utc))

    def daily_bars(self, ticker: str, lookback_days: int | None = None) -> list[DailyBar]:
        n = lookback_days or self.lookback_days
        t = self._yf.Ticker(ticker)
        hist = t.history(period=f"{n}d", interval="1d", auto_adjust=False)
        bars: list[DailyBar] = []
        for idx, row in hist.iterrows():
            bars.append(
                DailyBar(
                    date=idx.strftime("%Y-%m-%d"),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                )
            )
        return bars

    def last_daily_close(self, ticker: str) -> DailyBar | None:
        bars = self.daily_bars(ticker, lookback_days=5)
        return bars[-1] if bars else None

    def quote_currency_to_usd(self, currency: str) -> float:
        """USD value of 1 unit of `currency`.

        Examples (illustrative, rates fluctuate):
          quote_currency_to_usd('USD') -> 1.0
          quote_currency_to_usd('GBP') -> ~1.27   (1 GBP = $1.27)
          quote_currency_to_usd('JPY') -> ~0.0065 (1 JPY = $0.0065, via 1/USDJPY)
          quote_currency_to_usd('CAD') -> ~0.73   (1 CAD = $0.73, via 1/USDCAD)

        For currencies in `USD_BASE_CURRENCIES` (JPY, CAD, CHF, ...) we fetch
        USDxxx and invert, since that's the liquid quote. For everything else
        (EUR, GBP, AUD, NZD, ...) we fetch xxxUSD directly. Raises on failure
        so the engine can refuse to compute P&L this cycle.
        """
        c = currency.upper().strip()
        if c == "USD":
            return 1.0
        if c in USD_BASE_CURRENCIES:
            inv = self.quote(f"USD{c}=X").price
            if inv <= 0:
                raise RuntimeError(f"USD{c}=X returned non-positive price: {inv}")
            return 1.0 / inv
        direct = self.quote(f"{c}USD=X").price
        if direct <= 0:
            raise RuntimeError(f"{c}USD=X returned non-positive price: {direct}")
        return direct
