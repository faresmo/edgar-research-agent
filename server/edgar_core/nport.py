"""Fund portfolio reports (Form N-PORT) and the derivatives in them.

Registered funds (closed-end funds, ETFs, mutual funds) report every holding
monthly on Form N-PORT; the report for the last month of each fiscal quarter
is made public about 60 days later. The filing is an XML document that lists
each position, and for derivatives gives the counterparty, reference entity,
notional, termination date and the payment legs. That is where a fund's
credit default swaps, and so its liability if the reference companies
default, can be read contract by contract.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional
from xml.etree import ElementTree

from .client import FOREVER, EdgarClient, EdgarError
from .companies import format_cik
from .dates import check_date
from .filings import Filing, list_filings

_CREDIT_WORDS = re.compile(r"\b(credit\s*default|cds|cdx|itraxx|protection)\b", re.IGNORECASE)
_INDEX_WORDS = re.compile(r"\b(cdx|itraxx|index|series)\b", re.IGNORECASE)
_SUFFIXES = re.compile(
    r"[.,]|\b(inc|incorporated|corp|corporation|co|company|llc|l\.l\.c|ltd|limited|plc|holdings?|group|the)\b",
    re.IGNORECASE,
)


class ReportNotFoundError(EdgarError):
    """The fund has no public N-PORT report on or before the date asked for."""


@dataclass(frozen=True)
class CdsContract:
    reference: str  # company or index the swap is written on
    direction: str  # "sold" (fund owes if the reference defaults), "bought", or "unclear"
    counterparty: str
    notional: float  # absolute value
    currency: str
    termination_date: str  # when the protection ends
    fixed_rate: Optional[float]  # the running coupon, in percent
    upfront_paid: float
    upfront_received: float
    value_usd: Optional[float]  # current mark-to-market
    unrealized: Optional[float]
    payoff_profile: str  # the filing's own Long/Short flag
    notional_sign: int  # +1 / -1 as reported, kept for the reader's judgement
    notes: str = ""  # why the direction is "unclear", when it is

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class NportReport:
    fund_name: str
    series_name: str
    cik: str
    period_end: str  # the month the holdings are as of
    filed: str
    accession_number: str
    url: str
    total_assets: Optional[float]
    net_assets: Optional[float]
    positions: int
    cds: List[CdsContract] = field(default_factory=list)
    other_swaps: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cds"] = [c.to_dict() for c in self.cds]
        return d


@dataclass(frozen=True)
class CdsExposure:
    report: NportReport
    sold_notional: Dict[str, float]  # by currency: what the fund would owe if every reference defaulted
    bought_notional: Dict[str, float]
    unclear_notional: Dict[str, float]
    latest_termination: str
    earliest_termination: str
    sold_by_name: Dict[str, Dict[str, float]] = field(default_factory=dict)  # company -> {currency: notional}
    sold_indices: Dict[str, Dict[str, float]] = field(default_factory=dict)  # index -> {currency: notional}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["report"] = self.report.to_dict()
        return d


def normalize_name(name: str) -> str:
    """'International Business Machines Corporation', 'International Business
    Machines' and 'IBM' are three spellings the same filing can use for one
    company. Strip punctuation and corporate suffixes so the first two merge;
    abbreviations still have to be merged by the reader."""
    cleaned = _SUFFIXES.sub(" ", name)
    return " ".join(cleaned.split()).lower()


# -- finding the report ---------------------------------------------------

def latest_portfolio_report(cik, as_of: Optional[str] = None, client: Optional[EdgarClient] = None) -> Filing:
    """The most recent public N-PORT filing on or before `as_of`."""
    as_of = check_date(as_of, "as_of")
    filings = list_filings(cik, form_type="NPORT-P", limit=1, as_of=as_of, client=client)
    if not filings:
        raise ReportNotFoundError(f"no NPORT-P filing for CIK {format_cik(cik)}" + (f" on or before {as_of}" if as_of else ""))
    return filings[0]


def get_portfolio_report(cik, as_of: Optional[str] = None, client: Optional[EdgarClient] = None) -> NportReport:
    """Fetch and parse the fund's latest public N-PORT report."""
    client = client or EdgarClient()
    filing = latest_portfolio_report(cik, as_of=as_of, client=client)
    url = raw_xml_url(filing.url)
    xml = client.get_text(url, ttl=FOREVER)
    return parse_nport(xml, cik=format_cik(cik), filed=filing.filing_date,
                       accession_number=filing.accession_number, url=url)


_XSL_SEGMENT = re.compile(r"/xsl[^/]*/", re.IGNORECASE)


def raw_xml_url(url: str) -> str:
    """EDGAR lists an N-PORT's main document as, for example,
    ".../xslFormNPORT-P_X01/primary_doc.xml": that path returns the report
    rendered as an HTML page. The raw XML is the same file name one folder up."""
    return _XSL_SEGMENT.sub("/", url)


def cds_exposure(cik, as_of: Optional[str] = None, client: Optional[EdgarClient] = None) -> CdsExposure:
    """What the fund owes if every company it sold protection on defaulted,
    contract by contract, from its latest N-PORT report."""
    report = get_portfolio_report(cik, as_of=as_of, client=client)
    return summarize_cds(report)


def summarize_cds(report: NportReport) -> CdsExposure:
    sold: Dict[str, float] = {}
    bought: Dict[str, float] = {}
    unclear: Dict[str, float] = {}
    for c in report.cds:
        bucket = {"sold": sold, "bought": bought}.get(c.direction, unclear)
        bucket[c.currency] = round(bucket.get(c.currency, 0.0) + c.notional, 2)
    dates = sorted(c.termination_date for c in report.cds if c.termination_date)

    # Per-name totals for protection sold, with spelling variants merged and
    # index contracts kept apart (they reference a basket, not a company).
    by_name: Dict[str, Dict[str, float]] = {}
    indices: Dict[str, Dict[str, float]] = {}
    labels: Dict[str, str] = {}
    for c in report.cds:
        if c.direction != "sold":
            continue
        if _INDEX_WORDS.search(c.reference):
            bucket = indices.setdefault(c.reference, {})
        else:
            key = normalize_name(c.reference)
            label = labels.setdefault(key, c.reference)
            bucket = by_name.setdefault(label, {})
        bucket[c.currency] = round(bucket.get(c.currency, 0.0) + c.notional, 2)

    return CdsExposure(
        report=report, sold_notional=sold, bought_notional=bought, unclear_notional=unclear,
        latest_termination=dates[-1] if dates else "", earliest_termination=dates[0] if dates else "",
        sold_by_name=by_name, sold_indices=indices,
    )


# -- parsing --------------------------------------------------------------

def parse_nport(xml: str, cik: str = "", filed: str = "", accession_number: str = "", url: str = "") -> NportReport:
    """Parse an N-PORT XML document. Only derivatives are read in detail."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise EdgarError(f"could not parse N-PORT XML: {exc}") from exc

    cds: List[CdsContract] = []
    other_swaps = 0
    positions = 0
    for inv in _iter(root, "invstOrSec"):
        positions += 1
        swap = next(_iter(inv, "swapDeriv"), None)
        if swap is None:
            continue
        contract = _parse_swap(inv, swap)
        if contract is None:
            other_swaps += 1
        else:
            cds.append(contract)

    return NportReport(
        fund_name=_text(root, "regName"),
        series_name=_text(root, "seriesName"),
        cik=cik or format_cik(_text(root, "regCik") or "0"),
        period_end=_text(root, "repPdDate"),
        filed=filed,
        accession_number=accession_number,
        url=url,
        total_assets=_num(_text(root, "totAssets")),
        net_assets=_num(_text(root, "netAssets")),
        positions=positions,
        cds=cds,
        other_swaps=other_swaps,
    )


def _parse_swap(inv, swap) -> Optional[CdsContract]:
    rec_fixed = next(_iter(swap, "fixedRecDesc"), None)
    pay_fixed = next(_iter(swap, "fixedPmntDesc"), None)
    rec_other = next(_iter(swap, "otherRecDesc"), None)
    pay_other = next(_iter(swap, "otherPmntDesc"), None)
    rec_float = next(_iter(swap, "floatingRecDesc"), None)
    pay_float = next(_iter(swap, "floatingPmntDesc"), None)

    # Is this a credit default swap at all?
    words = " ".join([
        _text(inv, "name"), _text(inv, "title"),
        _text(swap, "issuerName"), _text(swap, "issueTitle"), _text(swap, "indexName"),
        _attr(rec_other, "otherDesc"), _attr(pay_other, "otherDesc"),
        _text(rec_other, "otherDesc"), _text(pay_other, "otherDesc"),
    ])
    if not _CREDIT_WORDS.search(words):
        return None

    reference = _text(swap, "issuerName") or _text(swap, "indexName") or _text(inv, "title") or _text(inv, "name")
    payoff = _text(inv, "payoffProfile")
    notional_raw = _num(_text(swap, "notionalAmt"))
    notional = abs(notional_raw) if notional_raw is not None else 0.0
    sign = -1 if (notional_raw or 0) < 0 else 1

    # Direction. The seller of protection receives the fixed coupon and owes
    # a contingent payment; the buyer pays the coupon.
    if rec_fixed is not None and pay_fixed is None:
        direction, rate = "sold", _num(_attr(rec_fixed, "fixedRt"))
    elif pay_fixed is not None and rec_fixed is None:
        direction, rate = "bought", _num(_attr(pay_fixed, "fixedRt"))
    else:
        direction, rate = "unclear", None
    notes = []
    if direction == "sold" and payoff.lower() == "short":
        notes.append("fund receives the fixed leg (seller) but the filing marks the position Short")
    if direction == "bought" and payoff.lower() == "long":
        notes.append("fund pays the fixed leg (buyer) but the filing marks the position Long")
    if direction == "unclear":
        notes.append("fixed leg not identifiable; check the schedule of investments in the N-CSR")
    if rec_float is not None or pay_float is not None:
        notes.append("has a floating leg; may not be a plain credit default swap")
    if notes and direction != "unclear" and any("marks the position" in n for n in notes):
        direction = "unclear"

    return CdsContract(
        reference=reference,
        direction=direction,
        counterparty=_text(swap, "counterpartyName"),
        notional=notional,
        currency=_text(swap, "curCd") or _text(inv, "curCd") or "USD",
        termination_date=_text(swap, "terminationDt"),
        fixed_rate=rate,
        upfront_paid=_num(_text(swap, "upfrontPmnt")) or 0.0,
        upfront_received=_num(_text(swap, "upfrontRcpt")) or 0.0,
        value_usd=_num(_text(inv, "valUSD")),
        unrealized=_num(_text(swap, "unrealizedAppr")),
        payoff_profile=payoff,
        notional_sign=sign,
        notes="; ".join(notes),
    )


# -- XML helpers (namespace-agnostic) -------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter(element, name: str):
    if element is None:
        return
    for child in element.iter():
        if _local(child.tag) == name:
            yield child


def _text(element, name: str) -> str:
    node = next(_iter(element, name), None)
    return (node.text or "").strip() if node is not None else ""


def _attr(element, name: str) -> str:
    return (element.get(name) or "").strip() if element is not None else ""


def _num(text: str) -> Optional[float]:
    text = (text or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def portfolio_report_for_period(cik, period_end: str, as_of: Optional[str] = None, client: Optional[EdgarClient] = None) -> Optional[NportReport]:
    """The fund's N-PORT for a given period end (the latest filed one if amended), or None when there is none."""
    from .filings import filing_history
    client = client or EdgarClient()
    for filing in filing_history(cik, forms=["NPORT-P", "NPORT-P/A"], as_of=as_of, client=client):
        if filing.report_date == period_end:
            url = raw_xml_url(filing.url)
            return parse_nport(client.get_text(url, ttl=FOREVER), cik=format_cik(cik), filed=filing.filing_date,
                               accession_number=filing.accession_number, url=url)
    return None
