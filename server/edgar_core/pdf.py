"""read_pdf: text of a PDF at any URL (7 Nov comparison with Claude alone: Ulta's investor-day deck held the new-store sales
ramp the question asked for; neither the plugin nor Claude's web fetch could read it, the file being compressed and over the
fetch size limit). Uses pypdf when installed (pip install 'edgar-agent[pdf]'); otherwise a small built-in reader that
inflates content streams and pulls text operators, which works for simple decks and fails soft on the rest."""
from __future__ import annotations

import io
import re
import urllib.request
import zlib
from typing import Dict, List, Optional

MAX_BYTES = 80 * 1024 * 1024
USER_AGENT = "edgar-research-agent/1.0 (+pdf reader)"
_SEC_HOSTS = ("sec.gov",)


def _download(url: str) -> bytes:
    import os
    ua = USER_AGENT
    if any(h in url for h in _SEC_HOSTS):
        ua = os.environ.get("EDGAR_USER_AGENT") or ua  # SEC asks for the declared user agent
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/pdf,*/*"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("the PDF is larger than 80 MB")
    return data


def _pages_pypdf(data: bytes) -> Optional[List[str]]:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return None
    reader = PdfReader(io.BytesIO(data))
    return [(p.extract_text() or "") for p in reader.pages]


_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_TEXT_OP = re.compile(rb"\((?:\\.|[^\\)])*\)\s*Tj|\[(?:[^\]]*)\]\s*TJ")
_STR = re.compile(rb"\((?:\\.|[^\\)])*\)")


def _pages_builtin(data: bytes) -> List[str]:
    """Rough fallback: one 'page' per content stream that holds text operators."""
    pages = []
    for m in _STREAM.finditer(data):
        raw = m.group(1)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        if b"Tj" not in raw and b"TJ" not in raw:
            continue
        parts = []
        for op in _TEXT_OP.finditer(raw):
            for s in _STR.findall(op.group(0)):
                parts.append(s[1:-1].replace(rb"\(", b"(").replace(rb"\)", b")").decode("latin-1", "ignore"))
        text = " ".join(parts).strip()
        if text:
            pages.append(text)
    return pages


def _page_set(spec: Optional[str], n: int) -> Optional[set]:
    if not spec:
        return None
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return {p for p in out if 1 <= p <= n}


def read_pdf(url: str, search: Optional[str] = None, pages: Optional[str] = None, max_chars: int = 12000) -> Dict:
    try:
        data = _download(url)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not download the PDF: {exc}", "url": url}
    if not data.startswith(b"%PDF"):
        return {"error": "the URL did not return a PDF", "url": url}
    texts = _pages_pypdf(data)
    engine = "pypdf"
    if texts is None:
        texts, engine = _pages_builtin(data), "built-in (install pypdf for better results: pip install 'edgar-agent[pdf]')"
    n = len(texts)
    wanted = _page_set(pages, n)
    words = [w for w in re.split(r"\s+", (search or "").lower()) if w]
    hits = []
    for i, t in enumerate(texts, 1):
        if wanted is not None and i not in wanted:
            continue
        if words and not all(w in t.lower() for w in words):
            continue
        hits.append({"page": i, "text": re.sub(r"[ \t]+", " ", t).strip()})
    out, used = [], 0
    for h in hits:
        room = max_chars - used
        if room <= 200:
            break
        h["text"] = h["text"][:room]
        used += len(h["text"])
        out.append(h)
    return {"url": url, "pages_in_file": n, "engine": engine, "matching_pages": [h["page"] for h in hits], "pages": out,
            "truncated": len(out) < len(hits),
            "note": "Non-SEC source: record figures with notes_add(source_type='web', url, quote) and show them under 'From non-SEC sources'."
                    + ("" if n else " No text could be extracted (scanned or unusual encoding).")}
