"""Full-text search across SEC filings (2001 onward).

Uses the same search that powers the SEC's own "EDGAR Full-Text Search" page.
It searches the text of filings and their exhibits and returns which documents
matched, not their contents. Fetch a matching document with its `url`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Iterable, List, Optional
from urllib.parse import urlencode

from .client import EdgarClient
from .companies import format_cik
from .dates import check_date

SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{document}"
PAGE_SIZE = 100  # the SEC returns up to 100 matches per page
EARLIEST = "2001-01-01"  # full-text search does not cover older filings


@dataclass(frozen=True)
class SearchHit:
    company: str  # for example "Apple Inc."
    cik: str  # 10-digit CIK of the filer
    form: str  # form of the filing, for example "8-K"
    document_type: str  # which document matched, for example "EX-99.1" or "8-K"
    description: str
    filed: str  # YYYY-MM-DD
    period_ending: str  # may be empty
    accession_number: str
    url: str  # link to the matching document

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SearchResults:
    total: int  # how many documents matched in all (may exceed what is returned)
    page: int
    hits: List[SearchHit] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total": self.total, "page": self.page, "hits": [h.to_dict() for h in self.hits]}


def search_filings(
    query: str,
    ciks: Optional[Iterable] = None,
    form_types: Optional[Iterable[str]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    as_of: Optional[str] = None,
    limit: int = 20,
    page: int = 1,
    client: Optional[EdgarClient] = None,
) -> SearchResults:
    """Search the text of SEC filings and exhibits.

    `query` follows the SEC search syntax: words are joined by AND, put a
    phrase in double quotes for an exact match, for example
    '"Series D Mandatory Convertible"'.

    Narrow the search with `ciks` (one or more companies), `form_types`
    (for example ["8-K", "10-K"]) and a filing-date range. Nothing filed after
    `as_of` is returned.

    Each hit is one matching document, so one filing can appear more than once
    (for example the 8-K itself and its press-release exhibit EX-99.1).
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("query is empty")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if page < 1:
        raise ValueError("page must be at least 1")
    date_from = check_date(date_from, "date_from")
    date_to = check_date(date_to, "date_to")
    as_of = check_date(as_of, "as_of")

    end = min(d for d in (date_to, as_of) if d) if (date_to or as_of) else None
    start = date_from
    if start and end and start > end:
        raise ValueError(f"date_from {start} is after the end of the range {end}")

    params = {"q": query}
    if start or end:
        params["dateRange"] = "custom"
        params["startdt"] = start or EARLIEST
        params["enddt"] = end or date.today().isoformat()
    if form_types:
        params["forms"] = ",".join(f.strip().upper() for f in form_types if f.strip())
    if ciks:
        params["ciks"] = ",".join(format_cik(c) for c in ciks)
    if page > 1:
        params["from"] = (page - 1) * PAGE_SIZE

    client = client or EdgarClient()
    data = client.get_json(SEARCH_URL + "?" + urlencode(params))

    hits_block = data.get("hits", {})
    total_block = hits_block.get("total", 0)
    total = total_block.get("value", 0) if isinstance(total_block, dict) else int(total_block or 0)

    hits: List[SearchHit] = []
    for raw in hits_block.get("hits", []):
        hit = _parse_hit(raw)
        if hit is None:
            continue
        if as_of and hit.filed and hit.filed > as_of:  # belt and braces
            continue
        hits.append(hit)
        if len(hits) >= limit:
            break
    return SearchResults(total=total, page=page, hits=hits)


def _parse_hit(raw: dict) -> Optional[SearchHit]:
    source = raw.get("_source", {})
    accession = source.get("adsh", "")
    doc_id = raw.get("_id", "")
    document = doc_id.split(":", 1)[1] if ":" in doc_id else ""
    if not accession and ":" in doc_id:
        accession = doc_id.split(":", 1)[0]
    ciks = source.get("ciks") or []
    if not accession or not ciks:
        return None
    cik10 = format_cik(ciks[0])
    names = source.get("display_names") or []
    return SearchHit(
        company=_clean_name(names[0]) if names else "",
        cik=cik10,
        form=source.get("form", ""),
        document_type=source.get("file_type", ""),
        description=source.get("file_description", "") or "",
        filed=source.get("file_date", ""),
        period_ending=source.get("period_ending", "") or "",
        accession_number=accession,
        url=ARCHIVES_URL.format(
            cik_int=int(cik10), accession_nodash=accession.replace("-", ""), document=document
        ) if document else "",
    )


def _clean_name(display_name: str) -> str:
    """'Apple Inc.  (AAPL)  (CIK 0000320193)' -> 'Apple Inc.'"""
    name = display_name.split("(CIK", 1)[0]
    if "(" in name:
        name = name.split("(", 1)[0]
    return " ".join(name.split())
