"""Scope: does the company a question is about file with the SEC? (8 Nov 2026 user decision: answer only about SEC
filers, US companies and foreign issuers that file 20-F/40-F/6-K. Valeo and Porsche, which trade in the US only as
over-the-counter receipts and file nothing, are outside it.)"""
from __future__ import annotations

import re
from typing import Dict, Optional

from .client import EdgarClient
from .companies import TICKERS_URL, format_cik
from .entities import EntityNotFoundError, resolve_entity
from .filings import filing_history

REPORTING_FORMS = ["10-K", "10-Q", "20-F", "40-F", "6-K", "10-K/A", "20-F/A", "40-F/A", "N-CSR", "N-CSRS", "NPORT-P", "N-CEN", "10-KT"]
_STOP = {"inc", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "sa", "se", "ag", "nv", "the", "group", "holdings", "holding", "companies"}


def _tokens(name: str):
    """The words that identify a company: lower case, without legal-form words and without EDGAR's state or status
    suffixes ('BANK OF AMERICA CORP /DE/', 'WELLS FARGO & COMPANY/MN', 'NESTLE SA /FI')."""
    name = re.sub(r"\s*/[A-Za-z]{2,4}/?\s*$", " ", name or "")
    name = re.sub(r"['\u2019`]", "", name)  # "McDonald's" and EDGAR's "MCDONALDS CORP" are one name (12 Nov)
    return [t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in _STOP]


class _Cand:
    def __init__(self, cik, name, kind="exact", listed=False):
        self.cik, self.name, self.kind, self.listed = cik, name, kind, listed


def _match_kind(want, name):
    """How an EDGAR name matches the words asked for: 'exact' (the same words), 'longer' (all of them, and more),
    'prefix' (a word of the name only begins with one asked for), or None."""
    names = _tokens(name)
    if not want or not names:
        return None
    if set(names) == set(want):
        return "exact"
    if all(t in names for t in want):
        return "longer"
    if all(any(n.startswith(t) for n in names) for t in want):
        return "prefix"
    return None


def _listed_companies(want, client):
    """Companies in the SEC's ticker file whose name contains every word asked for. The ticker file lists the active
    registrant; EDGAR's name lookup alone can return only dead namesakes (10 Nov: 'JPMorgan Chase', 'Bank of America',
    'Wells Fargo' and 'Morgan Stanley' were all declared non-filers that way)."""
    out, seen = [], set()
    try:
        entries = client.get_json(TICKERS_URL).values()
    except Exception:  # noqa: BLE001
        return out
    for e in entries:
        title = str(e.get("title") or "")
        kind = _match_kind(want, title)
        if kind:
            cik = format_cik(e["cik_str"])
            if cik not in seen:
                seen.add(cik)
                out.append(_Cand(cik, title, kind=kind, listed=True))
    return out


def check(company: str, as_of: Optional[str] = None, client: Optional[EdgarClient] = None) -> Dict:
    """{'in_scope': bool, 'cik', 'name', 'reason'}; errors leave the company in scope (never refuse on a lookup failure)."""
    client = client or EdgarClient()
    q = (company or "").strip()
    if not q:
        return {"in_scope": True, "reason": "no company given"}
    as_cik = re.fullmatch(r"(?:CIK\s*#?\s*)?0*(\d{1,10})", q, re.I)
    if as_cik:
        # A company number settles it directly, with no name matching: this is how a wrong verdict on a name is appealed.
        cik = as_cik.group(1).zfill(10)
        try:
            hist = filing_history(cik, forms=REPORTING_FORMS, as_of=as_of, client=client)
        except Exception as exc:  # noqa: BLE001
            return {"in_scope": True, "cik": cik, "reason": f"history unavailable ({exc}); treated as in scope"}
        if hist:
            return {"in_scope": True, "cik": cik, "reason": f"CIK {int(cik)} files periodic reports with the SEC (latest {hist[0].form} on {hist[0].filing_date})"}
        return {"in_scope": False, "cik": cik, "reason": f"CIK {int(cik)} is on EDGAR but has filed no periodic reports (10-K, 10-Q, 20-F, 40-F, 6-K)"}
    try:
        matches = resolve_entity(q, limit=10, client=client)
    except EntityNotFoundError:
        # 12 Nov: "nothing on EDGAR under this name" was handled like a failed lookup, so a name with no hits at all
        # (Lidl, Ikea, Rolex) was treated as in scope. No hit is an answer, not an error; the ticker file is still tried.
        matches = []
    except Exception as exc:  # noqa: BLE001
        return {"in_scope": True, "reason": f"lookup failed ({exc}); treated as in scope"}
    want = _tokens(q)
    # A symbol counts as a symbol only when the user typed it as one. 12 Nov, real EDGAR data: "Lego" is the ticker of
    # Legato Merger Corp. IV and "BYD" that of Boyd Gaming, and both non-filers were let in as those companies.
    typed_as_ticker = q.isupper() and len(q) <= 6
    cands, seen = [], set()
    for m in matches:
        kind = _match_kind(want, m.name)
        if m.matched_on == "ticker" and kind != "exact":
            kind = "symbol" if typed_as_ticker else "ticker"
        if kind and kind != "prefix" and m.cik not in seen:  # a name that only begins like the one asked for must be a listed company
            seen.add(m.cik)
            cands.append(_Cand(m.cik, m.name, kind=kind))
    for c in _listed_companies(want, client):
        if c.cik not in seen:
            seen.add(c.cik)
            cands.append(c)
    if cands:
        # EDGAR holds several entities under one name (old shells, trusts, subsidiaries), and a wrong "out of scope"
        # refuses a real company outright. So: in scope if ANY candidate files periodic reports, trying first the
        # entities that carry exactly the name asked for, then a symbol typed as one, then the rest: longer names
        # ('JPMorgan' -> 'JPMORGAN CHASE & CO'), listed companies whose name begins like it ('Pepsi' -> 'PEPSICO INC'),
        # a symbol typed as a word. `match` says which; anything but 'exact' is a guess the caller has Claude confirm
        # ('Mars' also begins 'MARSH & MCLENNAN'; 'BYD' is Boyd Gaming's symbol; Volkswagen's lease trusts file 10-Ks).
        has_exact_name = any(c.kind == "exact" for c in cands)
        order = {"exact": 0, "symbol": 1, "longer": 2, "prefix": 3, "ticker": 4}
        group = sorted(cands, key=lambda c: (c.kind != "exact", c.kind != "symbol", not c.listed, order.get(c.kind, 9)))  # among guesses, a listed company first
        group = [c for c in group if c.kind == "exact"] + [c for c in group if c.kind != "exact"][:8]
        for c in group[:12]:
            try:
                hist = filing_history(c.cik, forms=REPORTING_FORMS, as_of=as_of, client=client)
            except Exception:  # noqa: BLE001
                return {"in_scope": True, "cik": c.cik, "name": c.name, "match": c.kind, "reason": "history unavailable; treated as in scope"}
            if hist:
                # a symbol typed as one is as good as the name, unless another entity carries the name itself (BYD Co Ltd)
                match = "exact" if c.kind == "symbol" and not has_exact_name else ("ticker" if c.kind == "symbol" else c.kind)
                return {"in_scope": True, "cik": c.cik, "name": c.name, "match": match,
                        "reason": f"{c.name} (CIK {int(c.cik)}) files periodic reports with the SEC (latest {hist[0].form} on {hist[0].filing_date})"}
        c = group[0]
        also = f" (nor has any of the {len(group)} EDGAR entities matching that name)" if len(group) > 1 else ""
        return {"in_scope": False, "cik": c.cik, "name": c.name,
                "reason": f"{c.name} (CIK {int(c.cik)}) is on EDGAR but has filed no periodic reports (10-K, 10-Q, 20-F, 40-F, 6-K){also}"}
    return {"in_scope": False, "reason": f"no SEC filer matching '{q}' was found on EDGAR (ticker file and entity lookup)"}
