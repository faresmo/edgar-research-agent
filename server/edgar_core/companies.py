"""Company lookup: ticker symbol to CIK number."""

from __future__ import annotations

from typing import Optional

from .client import EdgarClient, EdgarError

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class TickerNotFoundError(EdgarError):
    """No company with that ticker is listed by the SEC."""


def format_cik(cik) -> str:
    """Return a CIK as the 10-digit, zero-padded string EDGAR URLs use."""
    digits = str(cik).strip()
    if not digits.isdigit() or len(digits) > 10:
        raise ValueError(f"not a valid CIK: {cik!r}")
    return digits.zfill(10)


def ticker_to_cik(ticker: str, client: Optional[EdgarClient] = None) -> str:
    """Return the 10-digit CIK for a ticker, for example "AAPL" -> "0000320193"."""
    wanted = ticker.strip().upper()
    if not wanted:
        raise ValueError("ticker is empty")
    client = client or EdgarClient()
    data = client.get_json(TICKERS_URL)
    for entry in data.values():
        if str(entry.get("ticker", "")).upper() == wanted:
            return format_cik(entry["cik_str"])
    raise TickerNotFoundError(f"no company found for ticker {ticker!r}")
