"""Turn a filing's HTML into plain text, one block of content per line."""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Content inside these tags is never shown to a reader. "ix:header" is the
# hidden block of machine-readable data at the top of modern (inline XBRL) filings.
_SKIP = {"script", "style", "head", "ix:header"}
_BLOCK = {
    "p", "div", "br", "tr", "li", "table", "section", "hr", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
_CELL = {"td", "th"}
_SPACES = re.compile(r"[ \t\r\f\v  -​ 　]+")


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag in _BLOCK:
            self.parts.append("\n")
        elif tag in _CELL:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Return the readable text of an HTML document.

    Each paragraph, heading or table row becomes one line. Blank lines and
    repeated spaces are removed.
    """
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    lines = []
    for raw in "".join(parser.parts).split("\n"):
        line = _SPACES.sub(" ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)
