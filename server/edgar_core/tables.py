"""Pull tables out of a filing with their structure intact.

Flattening a filing to plain text turns a financial statement into a line like
"Revenue 391,035 383,285 394,328", with the year headings far away and the
"in millions" note lost. These functions keep each table's column headings,
row labels and scale note together.

SEC tables are messy: dollar signs sit in their own cells, closing brackets
and percent signs are split off, headings span several columns, and empty
spacer columns are everywhere. The cleaning steps below undo that.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from .client import FOREVER, EdgarClient, EdgarError

_SKIP = {"script", "style", "head", "ix:header"}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "section", "hr"}
_SPACES = re.compile(r"[ \t\r\n\f\v  -​ 　]+")
_NUMBER = re.compile(r"^\(?-?\$?\s*\d[\d,]*(\.\d+)?\s*%?\)?$|^\(?-?\$?\s*\.\d+\s*%?\)?$")
_SCALE = re.compile(
    r"\(?\s*(?:[a-z$ ]{0,20})?\bin\s+(?:thousands|millions|billions)\b[^)\n]{0,80}\)?",
    re.IGNORECASE,
)
_MAX_COLSPAN = 50
_YEAR = re.compile(r"^(19|20)\d{2}$")
_BULLETS = {"•", "·", "◦", "●", "▪", "■", "-", "–", "—", "*", "o", ""}
_LOOKBACK = 600  # characters of text before a table searched for its caption and scale


class TableNotFoundError(EdgarError):
    """No table matched the request."""


@dataclass(frozen=True)
class Table:
    index: int  # position among the tables in the document, from 0
    caption: str  # the heading just above the table, if any
    scale_note: str  # for example "(In millions, except per share amounts)"; may be empty
    headers: List[str] = field(default_factory=list)  # one per column; first is the label column
    rows: List[List[str]] = field(default_factory=list)  # each row: label, then one value per column

    def to_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self) -> str:
        """The table as Markdown, with caption and scale note above it."""
        width = max([len(self.headers)] + [len(r) for r in self.rows] or [1])
        lines = []
        if self.caption:
            lines.append(f"**{self.caption}**")
        if self.scale_note:
            lines.append(self.scale_note)
        if lines:
            lines.append("")
        head = (self.headers + [""] * width)[:width] if self.headers else [""] * width
        lines.append("| " + " | ".join(_md(c) for c in head) + " |")
        lines.append("|" + "---|" * width)
        for row in self.rows:
            cells = (row + [""] * width)[:width]
            lines.append("| " + " | ".join(_md(c) for c in cells) + " |")
        return "\n".join(lines)


def _md(text: str) -> str:
    return text.replace("|", "\\|")


# -- parsing --------------------------------------------------------------

class _Parser(HTMLParser):
    """Collect every top-level table as rows of (text, colspan) cells,
    remembering the text that came just before each table."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: List[Tuple[str, List[List[Tuple[str, int]]]]] = []
        self._outside: List[str] = []
        self._skip = 0
        self._depth = 0
        self._rows: List[List[Tuple[str, int]]] = []
        self._row: Optional[List[Tuple[str, int]]] = None
        self._cell: Optional[List[str]] = None
        self._colspan = 1
        self._before = ""
        self._since = 0  # length of _outside when the last table ended

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._before = "".join(self._outside[self._since:])[-_LOOKBACK * 3:]
                self._rows, self._row, self._cell = [], None, None
            return
        if self._depth == 0:
            if tag in _BLOCK:
                self._outside.append("\n")
            return
        if self._depth > 1:  # a table inside a table: flatten into the cell
            if self._cell is not None and tag in _BLOCK | {"tr", "td", "th"}:
                self._cell.append(" ")
            return
        if tag == "tr":
            self._end_row()
            self._row = []
        elif tag in ("td", "th"):
            self._end_cell()
            if self._row is None:
                self._row = []
            self._cell = []
            self._colspan = _colspan(attrs)
        elif self._cell is not None and tag in _BLOCK:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self._end_row()
                self.tables.append((self._before, self._rows))
                self._outside.append("\n")
                self._since = len(self._outside)
            return
        if self._depth == 0:
            if tag in _BLOCK:
                self._outside.append("\n")
            return
        if self._depth > 1:
            return
        if tag in ("td", "th"):
            self._end_cell()
        elif tag == "tr":
            self._end_row()

    def handle_data(self, data):
        if self._skip:
            return
        if self._depth == 0:
            self._outside.append(data)
        elif self._cell is not None:
            self._cell.append(data)

    def _end_cell(self):
        if self._cell is not None and self._row is not None:
            self._row.append((_clean(" ".join(self._cell)), self._colspan))
        self._cell = None

    def _end_row(self):
        self._end_cell()
        if self._row is not None:
            self._rows.append(self._row)
        self._row = None


def _colspan(attrs) -> int:
    for name, value in attrs:
        if name == "colspan":
            try:
                return max(1, min(_MAX_COLSPAN, int(str(value).strip())))
            except ValueError:
                return 1
    return 1


def _clean(text: str) -> str:
    text = _SPACES.sub(" ", text).strip()
    # "( 1,234 )" -> "(1,234)"; "$ 12" -> "$12"
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\$\s+(?=[\d(.])", "$", text)
    return text


def _is_number(text: str) -> bool:
    return bool(text) and bool(_NUMBER.match(text.replace(" ", "")))


# -- cleaning -------------------------------------------------------------

def _build(index: int, before: str, raw_rows: List[List[Tuple[str, int]]]) -> Optional[Table]:
    # 1. Expand column spans into a grid. A spanning cell's text is repeated in
    #    every column it covers, but only the first copy is its "origin".
    grid: List[List[Tuple[str, bool]]] = []
    for raw in raw_rows:
        row: List[Tuple[str, bool]] = []
        for text, span in raw:
            row.append((text, True))
            row.extend((text, False) for _ in range(span - 1))
        grid.append(row)
    grid = [r for r in grid if any(t for t, _ in r)]
    if not grid:
        return None
    width = max(len(r) for r in grid)
    grid = [r + [("", True)] * (width - len(r)) for r in grid]

    # 2. Header rows are the rows before the first row with a number in it
    #    (outside the label column).
    def is_data(r):
        numbers = [t for t, o in r[1:] if o and _is_number(t)]
        if not numbers:
            return False
        # A row of bare years with no label ("2024  2023") is a heading.
        return not (not r[0][0] and all(_YEAR.match(t) for t in numbers))

    first_data = next((i for i, r in enumerate(grid) if is_data(r)), None)
    header_rows = grid[:first_data] if first_data else []
    body = grid[first_data:] if first_data is not None else grid

    # 3. In body rows, fold split-off symbols into their numbers and drop
    #    lone dollar signs. Work on origin cells only.
    body_cells = [[t if o else "" for t, o in r] for r in body]
    for cells in body_cells:
        for c, text in enumerate(cells):
            if text in ("$", "US$"):
                cells[c] = ""
            elif text.startswith("$") and _is_number(text[1:]):
                cells[c] = text[1:]  # "$1,332,232" -> "1,332,232", like the cells where the sign is separate
            elif text in (")", "%", ")%", "%)") and c > 0:
                prev = next((p for p in range(c - 1, -1, -1) if cells[p]), None)
                if prev is not None and prev > 0:
                    cells[prev] = cells[prev] + text
                cells[c] = ""

    # 4. Keep the label column and every column that has a value in the body.
    keep = [0] + [c for c in range(1, width) if any(cells[c] for cells in body_cells)]
    if len(keep) < 2:
        return None

    # 5. Headings: move text that landed in a dropped column to the next kept
    #    column on its right, then join the heading rows column by column.
    header_texts: List[List[str]] = []
    for hrow in header_rows:
        texts = [t for t, _ in hrow]
        moved = list(texts)
        for c in range(width):
            if c not in keep and texts[c]:
                right = next((k for k in keep if k > c), None)
                if right is not None and not moved[right]:
                    moved[right] = texts[c]
        header_texts.append(moved)
    headers = []
    for c in keep:
        parts: List[str] = []
        for texts in header_texts:
            t = texts[c]
            if t and (not parts or parts[-1] != t) and not _SCALE.fullmatch(t):
                parts.append(t)
        headers.append(" ".join(parts))

    rows = [[cells[c] for c in keep] for cells in body_cells]
    rows = [r for r in rows if any(r)]
    if not rows:
        return None
    if first_data is None and all(r[0] in _BULLETS for r in rows):
        return None  # a table used only to lay out bullet points
    headers, rows = _merge_split_columns(headers, rows)
    if not any(headers):
        headers = []

    # 6. Caption and scale note from the text just before the table (or a
    #    heading cell inside it).
    lines = [ln for ln in (_clean(x) for x in before.split("\n")) if ln][-6:]
    scale = ""
    for source in [" ".join(t for t, _ in r) for r in header_rows] + lines[::-1]:
        match = _SCALE.search(source)
        if match:
            scale = match.group(0).strip()
            break
    caption = ""
    for line in reversed(lines):
        if _SCALE.fullmatch(line) or len(line) > 200 or _is_number(line):
            continue
        if line.startswith("(") and line.endswith(")"):
            continue  # "(Unaudited)", "(Dollars in millions)" and the like
        caption = line
        break
    return Table(index=index, caption=caption, scale_note=scale, headers=headers, rows=rows)


# -- public ---------------------------------------------------------------

def extract_tables(html: str) -> List[Table]:
    """Return every data table in an HTML document, cleaned.

    Layout-only tables (a single column of text, or empty) are left out, so
    `index` counts only the tables returned.
    """
    parser = _Parser()
    parser.feed(html)
    parser.close()
    tables: List[Table] = []
    for before, raw_rows in parser.tables:
        table = _build(len(tables), before, raw_rows)
        if table is not None:
            tables.append(table)
    return tables


def list_tables(url: str, client: Optional[EdgarClient] = None) -> List[dict]:
    """Short summaries of the tables in a document, to choose one from:
    [{"index", "caption", "scale_note", "headers", "rows", "first_labels"}]."""
    client = client or EdgarClient()
    tables = extract_tables(client.get_text(url, ttl=FOREVER))
    return [
        {
            "index": t.index,
            "caption": t.caption,
            "scale_note": t.scale_note,
            "headers": t.headers,
            "rows": len(t.rows),
            "first_labels": [r[0] for r in t.rows[:5]],
        }
        for t in tables
    ]


def get_table(
    url: str,
    index: Optional[int] = None,
    query: Optional[str] = None,
    client: Optional[EdgarClient] = None,
) -> Table:
    """Return one table from a document, by `index` or by `query`.

    `query` is matched (ignoring case) against each table's caption, headings
    and row labels; every word must appear somewhere in the table. Matches in
    the caption count most. The best match is returned.
    """
    if (index is None) == (query is None or not query.strip()):
        raise ValueError("give exactly one of index or query")
    client = client or EdgarClient()
    tables = extract_tables(client.get_text(url, ttl=FOREVER))
    if index is not None:
        if not 0 <= index < len(tables):
            raise TableNotFoundError(f"table {index} not found; the document has {len(tables)} tables")
        return tables[index]

    words = [w for w in query.lower().split() if len(w) > 1]
    best, best_score, best_hits = None, 0.0, 0
    for table in tables:
        caption = table.caption.lower()
        body = " ".join(table.headers + [r[0] for r in table.rows] + [table.scale_note]).lower()
        hits = [w for w in words if w in caption or w in body]
        if not hits:
            continue
        # Every word need not match: a model often types a whole row of labels as the query
        # ("Adjusted EBITDA Integration costs Transaction costs Restructuring") and one unusual
        # word should not make the table unfindable. Most of the words must, though.
        if len(hits) < max(1, int(0.6 * len(words) + 0.5)):
            continue
        score = sum(3 if w in caption else 1 for w in hits) + 0.5 * len(hits) / len(words)
        if score > best_score:
            best, best_score, best_hits = table, score, len(hits)
    if best is None:
        raise TableNotFoundError(f"no table matches {query!r}; the document has {len(tables)} tables. "
                                 "Try two or three distinctive words from the table's caption or first row label.")
    return best


def _merge_split_columns(headers, rows):
    """Filings often put a value's "$" sign and the number in separate cells,
    and sometimes a merged cell on one row and separate cells on the next, so
    one logical column arrives as two: a row has its number in one or the
    other, never both. Merge adjacent columns that are never both filled on
    the same row and share a heading (or one heading is blank)."""
    width = max([len(headers)] + [len(r) for r in rows])
    headers = (headers + [""] * width)[:width] if headers else [""] * width
    rows = [(r + [""] * width)[:width] for r in rows]
    c = 1
    while c < width - 1:
        left, right = c, c + 1
        complementary = all(not (r[left] and r[right]) for r in rows)
        same_heading = headers[left] == headers[right] or not headers[left] or not headers[right]
        if complementary and same_heading and any(r[left] for r in rows) and any(r[right] for r in rows):
            for r in rows:
                r[left] = r[left] or r[right]
                del r[right]
            headers[left] = headers[left] or headers[right]
            del headers[right]
            width -= 1
        else:
            c += 1
    if not any(headers):
        headers = []
    return headers, rows
