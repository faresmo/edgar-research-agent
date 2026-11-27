"""The documents inside a filing (main form and exhibits), and reading them.

A filing is a bundle. An 8-K announcing earnings is a two-paragraph cover
document; the results, guidance and non-GAAP reconciliations are in its
press-release exhibit, usually EX-99.1. These functions list a filing's
documents, pick one by type, and search or page through its text.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List, Optional

from .client import FOREVER, EdgarClient, EdgarError
from .companies import format_cik
from .tables import extract_tables
from .text import html_to_text

FOLDER_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/"
INDEX_URL = FOLDER_URL + "{accession}-index.htm"
DEFAULT_MAX_CHARS = 12000


class DocumentNotFoundError(EdgarError):
    """The filing has no document of the requested type."""


@dataclass(frozen=True)
class FilingDocument:
    seq: str  # position in the filing, "1" is the main form; may be empty
    description: str
    filename: str
    type: str  # for example "8-K", "EX-99.1", "GRAPHIC"
    size: Optional[int]  # bytes
    url: str

    def to_dict(self) -> dict:
        return asdict(self)


def list_documents(cik, accession_number: str, client: Optional[EdgarClient] = None) -> List[FilingDocument]:
    """Return the documents in a filing, in filing order, from its index page."""
    cik10 = format_cik(cik)
    accession = accession_number.strip()
    nodash = accession.replace("-", "")
    if not (nodash.isdigit() and len(nodash) == 18):
        raise ValueError(f"not a valid accession number: {accession_number!r}")
    if "-" not in accession:
        accession = f"{nodash[:10]}-{nodash[10:12]}-{nodash[12:]}"
    folder = FOLDER_URL.format(cik_int=int(cik10), accession_nodash=nodash)
    client = client or EdgarClient()
    html = client.get_text(INDEX_URL.format(cik_int=int(cik10), accession_nodash=nodash, accession=accession), ttl=FOREVER)

    documents: List[FilingDocument] = []
    for table in extract_tables(html):
        names = [h.strip().lower() for h in table.headers]
        if "document" not in names or "type" not in names:
            continue
        col = {name: names.index(name) for name in names}
        for row in table.rows:
            cells = row + [""] * (len(names) - len(row))
            filename = cells[col["document"]].split(" ")[0] if cells[col["document"]] else ""
            if not filename:
                continue
            size_text = cells[col["size"]].replace(",", "") if "size" in col else ""
            documents.append(
                FilingDocument(
                    seq=cells[col["seq"]] if "seq" in col else "",
                    description=cells[col["description"]] if "description" in col else "",
                    filename=filename,
                    type=cells[col["type"]],
                    size=int(size_text) if size_text.isdigit() else None,
                    url=folder + filename,
                )
            )
    return documents


def find_document(documents: List[FilingDocument], doc_type: str) -> FilingDocument:
    """Pick a document by type. An exact match wins ("EX-99.1"); otherwise the
    first sub-numbered type of what was asked ("EX-99" finds "EX-99.1", but
    "EX-10" does not find "EX-101.SCH")."""
    wanted = doc_type.strip().upper()
    for doc in documents:
        if doc.type.upper() == wanted:
            return doc
    for doc in documents:
        if doc.type.upper().startswith(wanted + "."):
            return doc
    types = ", ".join(sorted({d.type for d in documents if d.type})) or "none"
    raise DocumentNotFoundError(f"no {doc_type} document in this filing; types found: {types}")


def get_exhibit(cik, accession_number: str, doc_type: str = "EX-99.1",
                client: Optional[EdgarClient] = None) -> FilingDocument:
    """Find one document in a filing by type. Defaults to EX-99.1, the usual
    earnings press release attached to an 8-K."""
    return find_document(list_documents(cik, accession_number, client=client), doc_type)


def _text(url: str, client: Optional[EdgarClient]) -> str:
    client = client or EdgarClient()
    return html_to_text(client.get_text(url, ttl=FOREVER))


def find_in_document(url: str, query: str, max_hits: int = 5, context_chars: int = 300,
                     client: Optional[EdgarClient] = None) -> dict:
    """Find a phrase in a document's text (ignoring case).

    Returns {"total_chars", "total_hits", "hits": [{"offset", "context"}]}.
    Read more around a hit with read_document(url, offset=...).
    """
    phrase = (query or "").strip().lower()
    if not phrase:
        raise ValueError("query is empty")
    if max_hits < 1 or context_chars < 0:
        raise ValueError("max_hits must be at least 1 and context_chars not negative")
    text = _text(url, client)
    lowered = text.lower()
    offsets, start = [], 0
    while True:
        found = lowered.find(phrase, start)
        if found < 0:
            break
        offsets.append(found)
        start = found + len(phrase)
    hits = [
        {"offset": o, "context": text[max(0, o - context_chars // 2): o + len(phrase) + context_chars // 2]}
        for o in offsets[:max_hits]
    ]
    return {"total_chars": len(text), "total_hits": len(offsets), "hits": hits}


def read_document(url: str, offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS,
                  client: Optional[EdgarClient] = None) -> dict:
    """Return one page of a document's text.

    Returns {"text", "offset", "total_chars", "next_offset"}; next_offset is
    None at the end.
    """
    if offset < 0 or max_chars < 1:
        raise ValueError("offset cannot be negative and max_chars must be at least 1")
    text = _text(url, client)
    page = text[offset: offset + max_chars]
    end = offset + len(page)
    return {"text": page, "offset": offset, "total_chars": len(text),
            "next_offset": end if end < len(text) else None}
