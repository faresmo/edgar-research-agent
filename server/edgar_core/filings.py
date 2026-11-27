"""List a company's recent filings."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List, Optional

from .client import EdgarClient
from .companies import format_cik
from .dates import check_date, on_or_before

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{document}"


@dataclass(frozen=True)
class Filing:
    form: str  # for example "10-K"
    filing_date: str  # YYYY-MM-DD
    report_date: str  # period the filing covers; may be empty
    accession_number: str  # for example "0000320193-24-000123"
    description: str
    url: str  # link to the main document
    items: str = ""  # 8-K item numbers ("2.02,9.01") when EDGAR lists them; empty otherwise

    def to_dict(self) -> dict:
        return asdict(self)


def list_filings(
    cik,
    form_type: Optional[str] = None,
    limit: int = 10,
    as_of: Optional[str] = None,
    client: Optional[EdgarClient] = None,
) -> List[Filing]:
    """Return a company's most recent filings, newest first.

    `form_type` filters to one form, for example "10-K" (exact match, so
    "10-K" does not include "10-K/A" amendments).

    Filings made after `as_of` (YYYY-MM-DD) are left out.

    Only the SEC's "recent" list is searched (roughly the last 1,000 filings
    or one year, whichever is more). Older filings are not returned.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    as_of = check_date(as_of, "as_of")
    cik10 = format_cik(cik)
    client = client or EdgarClient()
    data = client.get_json(SUBMISSIONS_URL.format(cik=cik10))
    recent = data.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    wanted = form_type.strip().upper() if form_type else None
    results: List[Filing] = []
    for i, form in enumerate(forms):
        if wanted and form.upper() != wanted:
            continue
        if not on_or_before(_at(recent, "filingDate", i), as_of):
            continue
        accession = recent["accessionNumber"][i]
        document = _at(recent, "primaryDocument", i)
        results.append(
            Filing(
                form=form,
                filing_date=_at(recent, "filingDate", i),
                report_date=_at(recent, "reportDate", i),
                accession_number=accession,
                description=_at(recent, "primaryDocDescription", i),
                url=ARCHIVES_URL.format(
                    cik_int=int(cik10),
                    accession_nodash=accession.replace("-", ""),
                    document=document,
                ),
            )
        )
        if len(results) >= limit:
            break
    return results


def _at(recent: dict, key: str, index: int) -> str:
    values = recent.get(key, [])
    return values[index] if index < len(values) else ""


OLDER_URL = "https://data.sec.gov/submissions/{name}"


def filing_history(cik, forms=None, as_of: Optional[str] = None, client: Optional[EdgarClient] = None) -> List[Filing]:
    """Every filing the company has on EDGAR, newest first, including the older pages that `list_filings`
    does not search (EDGAR keeps about the last 1,000 filings in the main list and the rest in separate
    files; Microsoft's 1996 filings are only there). `forms` limits the result to those exact form types.
    Filings made after `as_of` are left out. Used to verify a claim that a form was never filed."""
    as_of = check_date(as_of, "as_of")
    cik10 = format_cik(cik)
    client = client or EdgarClient()
    data = client.get_json(SUBMISSIONS_URL.format(cik=cik10))
    pages = [data.get("filings", {}).get("recent", {})]
    for older in data.get("filings", {}).get("files", []) or []:
        if older.get("name"):
            pages.append(client.get_json(OLDER_URL.format(name=older["name"])))
    wanted = {f.strip().upper() for f in forms} if forms else None
    out: List[Filing] = []
    for page in pages:
        for i, form in enumerate(page.get("form", [])):
            if wanted and form.upper() not in wanted:
                continue
            if not on_or_before(_at(page, "filingDate", i), as_of):
                continue
            accession = page["accessionNumber"][i]
            out.append(Filing(form=form, filing_date=_at(page, "filingDate", i), report_date=_at(page, "reportDate", i),
                              accession_number=accession, description=_at(page, "primaryDocDescription", i),
                              url=ARCHIVES_URL.format(cik_int=int(cik10), accession_nodash=accession.replace("-", ""),
                                                      document=_at(page, "primaryDocument", i)),
                              items=_at(page, "items", i)))
    out.sort(key=lambda f: f.filing_date, reverse=True)
    return out
