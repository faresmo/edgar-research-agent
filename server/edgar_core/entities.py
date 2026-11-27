"""Find any EDGAR filer (company, fund, trust) and describe it.

`ticker_to_cik` only knows operating companies listed in the SEC's ticker
file. Closed-end funds, ETFs and many trusts are missing from it, so this
module adds a second route: EDGAR's own entity lookup, the one that powers the
"company name" box on the SEC's full-text search page. It knows every filer
and the tickers EDGAR has on record for them.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional
from urllib.parse import urlencode

from .client import EdgarClient, EdgarError
from .companies import TICKERS_URL, format_cik

LOOKUP_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


class EntityNotFoundError(EdgarError):
    """No filer matched the name or ticker."""


@dataclass(frozen=True)
class EntityMatch:
    cik: str  # 10 digits
    name: str
    tickers: List[str] = field(default_factory=list)
    matched_on: str = ""  # "ticker" or "name"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EntityProfile:
    cik: str
    name: str
    tickers: List[str]
    exchanges: List[str]
    entity_type: str  # for example "operating", or an investment-company code
    sic: str
    sic_description: str
    fiscal_year_end: str  # "MM-DD" as EDGAR records it, or "". Self-reported: the month is
    # reliable, the day is not (one fund says "10-30" but reports to 31 October). Confirm
    # the exact date from a filing's period of report before using it in a calculation.
    state_of_incorporation: str
    former_names: List[dict]  # [{"name", "from", "to"}]

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_entity(query: str, limit: int = 5, client: Optional[EdgarClient] = None) -> List[EntityMatch]:
    """Find filers by ticker or name. Best match first.

    A ticker match (exact, any case) comes first. Then EDGAR's entity lookup
    is asked for the text, which finds funds and trusts that the ticker file
    does not list, and names such as "Saba Capital".
    """
    text = (query or "").strip()
    if not text:
        raise ValueError("query is empty")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    client = client or EdgarClient()
    wanted = text.upper()
    matches: List[EntityMatch] = []
    seen = set()

    if re.fullmatch(r"[A-Z0-9.\-]{1,10}", wanted):
        for entry in client.get_json(TICKERS_URL).values():
            if str(entry.get("ticker", "")).upper() == wanted:
                cik = format_cik(entry["cik_str"])
                matches.append(EntityMatch(cik=cik, name=entry.get("title", ""), tickers=[wanted], matched_on="ticker"))
                seen.add(cik)
                break

    data = client.get_json(LOOKUP_URL + "?" + urlencode({"keysTyped": text}))
    for raw in data.get("hits", {}).get("hits", []):
        cik_raw = str(raw.get("_id", "")).strip()
        if not cik_raw.isdigit():
            continue
        cik = format_cik(cik_raw)
        source = raw.get("_source", {})
        tickers = _tickers(source.get("tickers"))
        name = _strip_parens(str(source.get("entity", "")))
        matched_on = "ticker" if wanted in tickers else "name"
        if cik in seen:
            continue
        seen.add(cik)
        matches.append(EntityMatch(cik=cik, name=name, tickers=tickers, matched_on=matched_on))

    matches.sort(key=lambda m: 0 if m.matched_on == "ticker" else 1)
    tickerish = bool(re.fullmatch(r"[A-Z0-9.\-]{1,6}", wanted))
    if not matches and not tickerish:
        matches = _name_matches_from_ticker_file(text, client, seen)  # "Victoria's Secret": the lookup chokes on punctuation
    if tickerish and not any(m.matched_on == "ticker" for m in matches):
        # No filer carries this ticker in the SEC's file (Victoria's Secret is listed there as VSXY, not its NYSE
        # symbol VSCO; Seagen's SGEN left when it was acquired). A name that merely starts with the letters
        # ("VScoop Inc." for VSCO) must not win: look for the symbol on cover pages first.
        former = _former_ticker_matches(wanted, client, limit)
        former_ciks = {m.cik for m in former}
        matches = former + [m for m in matches if m.cik not in former_ciks]
    if not matches:
        raise EntityNotFoundError(
            f"no EDGAR filer matches {query!r}"
            + (". Not a current ticker: the company may have been acquired, renamed or delisted; search by company name."
               if re.fullmatch(r"[A-Z0-9.\-]{1,6}", wanted) else ""))
    return matches[:limit]


def _simplify(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("&", " and ")).split(" inc")[0].strip()


def _name_matches_from_ticker_file(text: str, client: EdgarClient, seen: set) -> List[EntityMatch]:
    """Match a company name against the titles in the SEC ticker file, ignoring punctuation and
    suffixes, so "Victoria's Secret & Co." finds "Victoria's Secret & Co." / VSCO."""
    want = _simplify(text)
    if len(want) < 3:
        return []
    out = []
    for entry in client.get_json(TICKERS_URL).values():
        title = _simplify(str(entry.get("title", "")))
        if title == want or title.startswith(want + " ") or want.startswith(title + " ") or (len(want) >= 8 and want in title):
            cik = format_cik(entry["cik_str"])
            if cik in seen:
                continue
            seen.add(cik)
            out.append(EntityMatch(cik=cik, name=entry.get("title", ""), tickers=[str(entry.get("ticker", "")).upper()], matched_on="name (ticker file)"))
    return out[:5]


def _former_ticker_matches(ticker: str, client: EdgarClient, limit: int) -> List[EntityMatch]:
    """A ticker that is no longer in the SEC's list (Seagen's SGEN after the Pfizer deal) still
    appears on the cover pages of the company's own filings, which the full-text search finds.
    The filer that most of those documents belong to is the company; a near-miss name such as
    "Sage Therapeutics" for SGEN is never substituted (eval question 15, 30 Oct)."""
    from .search import search_filings  # local import: search imports companies, not entities
    try:
        results = search_filings(f'"{ticker}"', form_types=["10-K", "10-Q", "8-K", "20-F"], limit=40, client=client)
    except EdgarError:
        return []
    counts: dict = {}
    names: dict = {}
    for hit in results.hits:
        if not hit.cik:
            continue
        counts[hit.cik] = counts.get(hit.cik, 0) + 1
        names.setdefault(hit.cik, hit.company)
    ranked = sorted(counts, key=lambda c: -counts[c])
    out = []
    for cik in ranked[:limit]:
        if counts[cik] < 2 and len(ranked) > 1:
            continue  # a stray mention of the symbol in someone else's filing
        out.append(EntityMatch(cik=format_cik(cik), name=names[cik], tickers=[],
                               matched_on=f"former ticker {ticker} on {counts[cik]} of its filings (full-text search); confirm the name"))
    return out


def get_entity_profile(cik, client: Optional[EdgarClient] = None) -> EntityProfile:
    """Describe a filer from its EDGAR submissions record: tickers, fiscal
    year end, former names and so on."""
    cik10 = format_cik(cik)
    client = client or EdgarClient()
    data = client.get_json(SUBMISSIONS_URL.format(cik=cik10))
    fye = str(data.get("fiscalYearEnd") or "").strip()
    return EntityProfile(
        cik=cik10,
        name=data.get("name", ""),
        tickers=[t for t in data.get("tickers", []) if t],
        exchanges=[e for e in data.get("exchanges", []) if e],
        entity_type=str(data.get("entityType") or ""),
        sic=str(data.get("sic") or ""),
        sic_description=data.get("sicDescription", "") or "",
        fiscal_year_end=f"{fye[:2]}-{fye[2:]}" if len(fye) == 4 and fye.isdigit() else "",
        state_of_incorporation=data.get("stateOfIncorporation", "") or "",
        former_names=[
            {"name": f.get("name", ""), "from": (f.get("from") or "")[:10], "to": (f.get("to") or "")[:10]}
            for f in data.get("formerNames", []) or []
        ],
    )


def _tickers(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,\s]+", str(value))
    return [t.strip().upper() for t in items if t and t.strip()]


def _strip_parens(name: str) -> str:
    """'Saba Capital Income & Opportunities Fund (BRW)' -> without the ticker."""
    return re.sub(r"\s*\([A-Z0-9.,\- ]{1,30}\)\s*$", "", name).strip()
