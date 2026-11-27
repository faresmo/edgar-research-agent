"""Share prices: market data, NOT in SEC filings.

Offered to the agent only in extended source mode, where every figure from here
is recorded as a non-SEC source and must appear under "From non-SEC sources" in an
answer (SPEC §4). One provider: Tiingo, with the user's own API token (EDGAR_PRICE_API_KEY).
With no token there are no prices, and the tools say so; nothing else is asked instead.

22 Nov 2026 (publishing decisions 15 and 16): Stooq is gone. It had been the fallback when no
token was set or Tiingo failed, so prices were fetched from a provider the README never named.
The token travels in the request's Authorization header, never in the URL: the URL is written to
the cache with the response, and the token was found there. The SEC contact is not sent here
(edgar_core.client sends it to sec.gov hosts only).
Yahoo Finance is deliberately absent: its endpoints are unofficial and its terms
do not allow commercial use.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import List, Optional

from .client import EdgarClient, EdgarError, EdgarHTTPError

ONE_DAY = 24 * 3600.0

PROVIDERS = ("tiingo", "auto")  # auto: tiingo when a token is set, otherwise no prices
NO_KEY = "no share-price key is set (EDGAR_PRICE_API_KEY), so share prices cannot be fetched"


class PriceError(EdgarError):
    pass


@dataclass(frozen=True)
class PriceBar:
    date: str  # YYYY-MM-DD
    close: float
    adj_close: Optional[float] = None
    high: Optional[float] = None  # intraday high and low (7 Nov: 'highest possible price' questions need them)
    low: Optional[float] = None


@dataclass
class PriceSeries:
    provider: str
    ticker: str
    start: str
    end: str
    bars: List[PriceBar] = field(default_factory=list)
    source_url: str = ""

    def to_dict(self) -> dict:
        return {"provider": self.provider, "ticker": self.ticker, "start": self.start, "end": self.end,
                "source_url": self.source_url, "bars": [asdict(b) for b in self.bars]}

    def close_on_or_before(self, day: str) -> PriceBar:
        """The last trading day on or before `day` (weekends and holidays have no bar)."""
        candidates = [b for b in self.bars if b.date <= day]
        if not candidates:
            raise PriceError(f"no price for {self.ticker} on or before {day} (series starts {self.bars[0].date if self.bars else 'empty'})")
        return candidates[-1]


def _tiingo_url(ticker: str, start: str, end: str) -> str:
    return f"https://api.tiingo.com/tiingo/daily/{ticker.lower()}/prices?startDate={start}&endDate={end}"


def price_history(ticker: str, start: str, end: str, provider: str = "auto", api_key: str = "",
                  client: Optional[EdgarClient] = None) -> PriceSeries:
    """Daily closes for `ticker` between two dates (inclusive). Dates are YYYY-MM-DD."""
    if provider not in PROVIDERS:
        raise PriceError(f"unknown price provider {provider!r}; one of {', '.join(PROVIDERS)}")
    for d in (start, end):
        date.fromisoformat(d)  # raises ValueError on a bad date
    if not api_key or api_key.startswith("${"):
        raise PriceError(NO_KEY)  # before anything is sent anywhere
    client = client or EdgarClient()
    url = _tiingo_url(ticker, start, end)
    try:
        text = client.get_text(url, ttl=ONE_DAY, headers={"Authorization": "Token " + api_key})
    except EdgarHTTPError as exc:
        if exc.status in (401, 403):
            raise PriceError(f"Tiingo refused the share-price key (HTTP {exc.status}): it is mistyped, expired or its account is closed") from None
        if exc.status == 429:
            raise PriceError("Tiingo's request limit for this key has been reached (HTTP 429); prices are available again when the limit resets") from None
        if exc.status == 404:
            raise PriceError(f"Tiingo has no prices for {ticker.upper()} (HTTP 404); check the ticker") from None
        raise PriceError(f"Tiingo could not be reached or failed (HTTP {exc.status})") from None
    bars = _parse_tiingo(text)
    if not bars:
        raise PriceError(f"tiingo returned no prices for {ticker} between {start} and {end}; check the ticker")
    return PriceSeries(provider="tiingo", ticker=ticker.upper(), start=start, end=end, bars=bars, source_url=url)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_tiingo(text: str) -> List[PriceBar]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise PriceError("tiingo returned something that is not JSON") from None
    if isinstance(data, dict) and data.get("detail"):
        raise PriceError(f"tiingo: {data['detail']}")
    out = []
    for r in data or []:
        try:
            out.append(PriceBar(date=str(r["date"])[:10], close=float(r["close"]), adj_close=float(r.get("adjClose", r["close"])), high=_f(r.get("high")), low=_f(r.get("low"))))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda b: b.date)


def price_change(ticker: str, from_date: str, to_date: str, provider: str = "auto", api_key: str = "",
                 client: Optional[EdgarClient] = None) -> dict:
    """Closing price on (or last trading day before) each date, and the change between them."""
    start = (date.fromisoformat(from_date) - timedelta(days=10)).isoformat()  # room for weekends/holidays before from_date
    series = price_history(ticker, start, to_date, provider=provider, api_key=api_key, client=client)
    a, b = series.close_on_or_before(from_date), series.close_on_or_before(to_date)
    if a.close == 0:
        raise PriceError("starting price is zero")
    return {"provider": series.provider, "ticker": series.ticker, "source_url": series.source_url,
            "from": {"requested": from_date, "trading_day": a.date, "close": a.close},
            "to": {"requested": to_date, "trading_day": b.date, "close": b.close},
            "change": (b.close - a.close) / a.close, "change_pct": round((b.close - a.close) / a.close * 100, 2),
            "note": "market data, not from SEC filings: report under 'From non-SEC sources'"}
