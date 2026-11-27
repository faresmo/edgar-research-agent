"""Pull one section (an "Item") out of a 10-K annual report.

A 10-K runs to hundreds of pages, far more than a model should be handed at
once. These functions return a single Item, in pages of limited size.

Only Form 10-K is supported. A 10-Q reuses the same Item numbers in Part I
and Part II, so it needs different handling.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from .client import FOREVER, EdgarClient, EdgarError
from .text import html_to_text

# The standard Items of a Form 10-K, in the order they appear.
SECTIONS_10K: Dict[str, str] = {
    "1": "Business",
    "1A": "Risk Factors",
    "1B": "Unresolved Staff Comments",
    "1C": "Cybersecurity",
    "2": "Properties",
    "3": "Legal Proceedings",
    "4": "Mine Safety Disclosures",
    "5": "Market for Registrant's Common Equity and Related Stockholder Matters",
    "6": "Reserved (formerly Selected Financial Data)",
    "7": "Management's Discussion and Analysis (MD&A)",
    "7A": "Quantitative and Qualitative Disclosures About Market Risk",
    "8": "Financial Statements and Supplementary Data",
    "9": "Changes in and Disagreements with Accountants",
    "9A": "Controls and Procedures",
    "9B": "Other Information",
    "9C": "Disclosure Regarding Foreign Jurisdictions that Prevent Inspections",
    "10": "Directors, Executive Officers and Corporate Governance",
    "11": "Executive Compensation",
    "12": "Security Ownership of Certain Beneficial Owners and Management",
    "13": "Certain Relationships and Related Transactions",
    "14": "Principal Accountant Fees and Services",
    "15": "Exhibits and Financial Statement Schedules",
    "16": "Form 10-K Summary",
}

DEFAULT_MAX_CHARS = 12000  # roughly 3,000 tokens: safe for a small local model

# A heading is a short line that starts with "Item 1A" (any capitalisation).
_HEADING = re.compile(r"^item\s+(\d{1,2}[abc]?)(?![0-9a-z])", re.IGNORECASE)
_MAX_HEADING_LENGTH = 150  # longer lines are sentences that happen to start with "Item"
_PART = re.compile(r"^part\s+[ivx]+\.?$", re.IGNORECASE)
_END_OF_ITEMS = re.compile(r"^signatures?$", re.IGNORECASE)


class SectionNotFoundError(EdgarError):
    """The requested Item is not in the document."""


@dataclass(frozen=True)
class FilingSection:
    item: str  # for example "1A"
    title: str  # for example "Risk Factors"
    text: str  # this page of the section
    offset: int  # where this page starts within the section, in characters
    total_chars: int  # length of the whole section
    truncated: bool  # True if more text follows; ask again with next_offset
    next_offset: Optional[int]
    url: str

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_item(item) -> str:
    """Accept "1A", "1a", "Item 1A.", 7 ... and return "1A" / "7"."""
    cleaned = re.sub(r"^item\s*", "", str(item).strip(), flags=re.IGNORECASE).rstrip(".: ").upper()
    if cleaned not in SECTIONS_10K:
        raise ValueError(
            f"{item!r} is not a 10-K item. Valid items: {', '.join(SECTIONS_10K)}"
        )
    return cleaned


def split_sections(text: str) -> Dict[str, str]:
    """Split the plain text of a 10-K into {item: section text}.

    Every Item is named at least twice: once in the table of contents and
    once as the real heading. For each Item we take every place it is named,
    measure the text up to the next *different* Item heading, and keep the
    longest. The table-of-contents entry is always the short one.
    """
    lines = text.split("\n")
    headings: List[Tuple[int, str]] = []
    for index, line in enumerate(lines):
        if len(line) > _MAX_HEADING_LENGTH:
            continue
        match = _HEADING.match(line)
        if match and match.group(1).upper() in SECTIONS_10K:
            headings.append((index, match.group(1).upper()))

    # The Items end at the signature page, if there is one after the last heading.
    document_end = len(lines)
    if headings:
        for index in range(headings[-1][0] + 1, len(lines)):
            if _END_OF_ITEMS.match(lines[index]):
                document_end = index
                break

    order = list(SECTIONS_10K)
    best: Dict[str, str] = {}
    for position, (start, item) in enumerate(headings):
        end = document_end
        for later_start, later_item in headings[position + 1:]:
            if later_item != item:
                # If the next heading is an *earlier* Item, the numbering has
                # started again: this is the last line of the table of
                # contents, and what follows it is not this Item's text.
                restarted = order.index(later_item) < order.index(item)
                end = start + 1 if restarted else later_start
                break
        body = lines[start:end]
        while body and _PART.match(body[-1]):  # drop a trailing "PART II" divider
            body.pop()
        section = "\n".join(body)
        if len(section) > len(best.get(item, "")):
            best[item] = section
    return {item: best[item] for item in SECTIONS_10K if item in best}


def _load_sections(url: str, client: Optional[EdgarClient]) -> Dict[str, str]:
    client = client or EdgarClient()
    return split_sections(html_to_text(client.get_text(url, ttl=FOREVER)))


def list_sections(url: str, client: Optional[EdgarClient] = None) -> List[dict]:
    """Return the Items found in a 10-K: [{"item", "title", "chars"}, ...].

    Use this first to see what is there and how long each part is.
    A very short section usually means the company wrote "not applicable" or
    pointed to another document.
    """
    sections = _load_sections(url, client)
    return [
        {"item": item, "title": SECTIONS_10K[item], "chars": len(text)}
        for item, text in sections.items()
    ]


def get_filing_section(
    url: str,
    item,
    max_chars: int = DEFAULT_MAX_CHARS,
    offset: int = 0,
    client: Optional[EdgarClient] = None,
) -> FilingSection:
    """Return one Item of a 10-K, for example item="1A" for Risk Factors.

    `url` is the document link from list_filings(). At most `max_chars`
    characters are returned. If `truncated` is True, call again with
    offset=next_offset to read the next page.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    if offset < 0:
        raise ValueError("offset cannot be negative")
    wanted = normalize_item(item)
    sections = _load_sections(url, client)
    if wanted not in sections:
        found = ", ".join(sections) or "none"
        raise SectionNotFoundError(
            f"Item {wanted} was not found in this document. Items found: {found}. "
            "Only Form 10-K documents are supported."
        )
    full = sections[wanted]
    page = full[offset:offset + max_chars]
    end = offset + len(page)
    truncated = end < len(full)
    return FilingSection(
        item=wanted,
        title=SECTIONS_10K[wanted],
        text=page,
        offset=offset,
        total_chars=len(full),
        truncated=truncated,
        next_offset=end if truncated else None,
        url=url,
    )
