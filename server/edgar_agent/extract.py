"""The extract worker (SPEC §7.4, §11 step 5): whole-document reading by a cheap model.

The main agent sees documents through keyholes (search hits, one table, a window),
which is right for cost but misses passages whose wording the search did not guess.
`extract` reads the WHOLE document (or one section) in chunks with a second, cheap
model whose only job is to return every passage relevant to the question, verbatim.
Quotes are checked against the text before they are returned, so a hallucinated
passage cannot come back, and the main agent still decides what the quotes mean.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

WORKER_SYSTEM = """You read one chunk of an SEC filing for an analyst. Return every passage in the chunk that DIRECTLY answers the analyst's question: the specific facts, figures, names (laws, products, counterparties), definitions, reasons, drivers or terms the question is about. Skip passages that are only generally related (boilerplate, other topics, introductions). Quote each passage VERBATIM from the chunk (copy the exact words, 1 to 4 sentences each; never paraphrase, never add anything). Prefer passages with numbers, names and specifics. Reply with JSON only: {"quotes": ["...", "..."]}. If nothing in the chunk directly answers the question, reply {"quotes": []}."""

DEFAULT_CHUNK = 24000   # characters, about 6k tokens
MAX_CHUNKS = 14         # a 60k-word 10-K section is about 14 chunks
MAX_QUOTES = 40


def _normalize(text: str) -> str:
    text = text.replace("’", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def chunk_text(text: str, size: int = DEFAULT_CHUNK) -> List[Dict[str, Any]]:
    """Split on paragraph boundaries where possible. Returns [{"offset", "text"}]."""
    chunks, start, n = [], 0, len(text)
    while start < n:
        end = min(n, start + size)
        if end < n:
            cut = text.rfind("\n", start + size // 2, end)
            if cut > start:
                end = cut
        chunks.append({"offset": start, "text": text[start:end]})
        start = end
    return chunks


def _parse_quotes(reply_text: str) -> List[str]:
    m = re.search(r"\{.*\}", reply_text or "", re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    quotes = data.get("quotes") if isinstance(data, dict) else None
    return [str(q).strip() for q in (quotes or []) if str(q).strip()]


def extract_passages(text: str, question: str, worker, chunk_size: int = DEFAULT_CHUNK,
                     max_chunks: int = MAX_CHUNKS) -> Dict[str, Any]:
    """Run the worker over every chunk; keep only quotes that are really in the text."""
    chunks = chunk_text(text, chunk_size)
    truncated = len(chunks) > max_chunks
    chunks = chunks[:max_chunks]
    found: List[Dict[str, Any]] = []
    seen = set()
    tokens_in = tokens_out = 0
    norm_text = _normalize(text)
    for c in chunks:
        reply = worker.complete(WORKER_SYSTEM, [{"role": "user", "content": f"Question: {question}\n\nChunk (characters {c['offset']}-{c['offset'] + len(c['text'])}):\n{c['text']}"}], [])
        tokens_in += reply.input_tokens
        tokens_out += reply.output_tokens
        for q in _parse_quotes(reply.text):
            nq = _normalize(q)
            if len(nq) < 20 or nq in seen:
                continue
            pos = norm_text.find(nq)
            if pos < 0:
                continue  # not verbatim: dropped
            seen.add(nq)
            found.append({"quote": q, "approx_offset": c["offset"] + max(0, _normalize(c["text"]).find(nq))})
            if len(found) >= MAX_QUOTES:
                break
        if len(found) >= MAX_QUOTES:
            break
    found.sort(key=lambda f: f["approx_offset"])
    return {"quotes": found, "chunks_read": len(chunks), "document_chars": len(text), "truncated": truncated,
            "worker_tokens": {"input": tokens_in, "output": tokens_out}}
