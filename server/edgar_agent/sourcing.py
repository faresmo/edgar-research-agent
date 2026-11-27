"""The sourcing check (SPEC.md section 4): every figure in the main answer
must trace to a filing fact or a calculation in the notes. Enforced in code
before an answer is accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple, Any, Iterable, List

# Markdown decoration before the words is allowed: "**From non-SEC sources (Tiingo):**" was rejected
# 39 times in one run because of the two asterisks (30 Oct).
# "425 bps", "3.5 billion", "12 percent", "7 times": a unit written as a separate word makes the number a figure.
_SPACED_UNIT = re.compile(r"(\d)\s+(bps|basis points?|percent|million|billion|trillion|thousand|mm|bn)\b", re.IGNORECASE)
_UNIT_SUFFIX = {"bps": "bps", "basis point": "bps", "basis points": "bps", "percent": "%", "million": "M", "billion": "B", "trillion": "T",
                "thousand": "K", "mm": "M", "bn": "B"}  # not "times": "2 times" is a count


def _attach_units(line: str) -> str:
    return _SPACED_UNIT.sub(lambda m: m.group(1) + _UNIT_SUFFIX[m.group(2).lower()], line)


_REF = re.compile(r"(?:\[\s*\d+(?:\s*,\s*\d+)*\s*\]\s*)+")  # [3], [3, 7] or [1][2]: references to notes by id


def _is_label_like(token: str, value: float) -> bool:
    """A bare integer under 10,000 with no currency sign, percent, multiple, decimal, thousands separator
    or scale suffix is a form number, an item, a count or a year, not a financial figure. Real figures
    carry a unit or a reference; the scanner no longer guesses about these (30 Oct)."""
    bare = token.strip("()")
    return bare.isdigit() and abs(value) < 10000 and not any(ch in token for ch in "$%.,") and not re.search(r"[xX]$|[KMBTkmbt]n?$|bn$|mm$|MM$|bps$", token)


NON_SEC_HEADING = re.compile(r"^[\s#>*_]*from non-sec sources\b.*$", re.IGNORECASE | re.MULTILINE)
_ACCESSION = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")
_URL = re.compile(r"https?://\S+")
_MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE = re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/(?:19|20)?\d{2}\b"
                   r"|\b" + _MONTHS + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+(?:19|20)\d{2})?\b"
                   r"|\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTHS + r"\.?(?:,?\s+(?:19|20)\d{2})?\b", re.IGNORECASE)
_YEARISH = re.compile(r"\b(?:FY|Q[1-4]\s*)?(?:19|20)\d{2}\b", re.IGNORECASE)
_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
# Form types, Items and Rules are names, not figures: 10-K, 8-K, 20-F, DEF 14A, 424B5, 13F-HR, N-2, S-4, Item 1A, Rule 144.
_FORMISH = re.compile(r"\bCIK\s*#?\s*\d{1,10}\b|\b(?:Form|Forms|Schedule|Rule)\s+(?:425|424B\d?|S-\d|F-\d|8-K|10-K|10-Q|20-F|13D|13G|14A|14D-9|TO)(?:/A)?\b|\b425\s+(?:filing|communication|prospectus)|\b\d{1,3}-[A-Z]{1,3}(?:/A)?\b|(?<![$\d.,])\b\d{1,3}(?:K|Q|F|D|G|A|KSB|QSB|KT|QT)\d?(?:-[A-Z]{2})?\b|\b[A-Z]{1,2}-\d{1,2}[A-Z]?(?:-\d)?\b|\b(?:Item|Items|Rule|Section|Note|Series|Part)\s+\d+[A-Za-z]?\b")
_NUMBER = re.compile(r"(?<![\w.])[-(]?\$?\d(?:,?\d)*(?:\.\d+)?(?:%|x|bps|(?:[KMBTkmbt]n?|bn|mm|MM)(?![a-z]))?\)?(?!\w|\.\d)")
_SCALES = (1.0, 1e3, 1e6, 1e9, 1e-3, 1e-6, 1e-9)


@dataclass(frozen=True)
class SourcingProblem:
    number: str  # as written in the answer
    line: str  # the line it appears on
    reason: str

    def to_dict(self) -> dict:
        return {"number": self.number, "line": self.line, "reason": self.reason}


def _candidates(value: Any) -> List[float]:
    """Every way a recorded value might be written: scaled to thousands,
    millions or billions, and as a fraction or a percent."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return []
    out = []
    for scale in _SCALES:
        out.extend((v * scale, v * scale * 100, v * scale / 100))
    if v < 0:  # "a decrease of 31.46%" states a negative value in words
        out.extend(-c for c in list(out))
    return out


# Unit-conversion constants that appear in working ("105.0 x 100 = 10,500 bps", "/ 1,000") are not figures.
_CONSTANTS = [100.0, 1000.0, 10000.0, 1e6, 1e9, 12.0, 4.0, 52.0, 365.0]


def _allowed_numbers(notes_entries: Iterable[dict], mode: str) -> List[float]:
    """Exact-ish matches: recorded values in any scale. Also the figures inside a VERIFIED
    quote of a filing note (the code found those words in the filing, so every number in
    them is sourced even if the model put the figure in the label rather than `value`)."""
    allowed: List[float] = list(_CONSTANTS)
    for e in notes_entries:
        if e["source_type"] in ("filing", "calculation"):
            allowed.extend(_candidates(e.get("value")))
        if e["source_type"] == "filing" and str(e.get("detail") or "").startswith("quote in"):
            for token in _NUMBER.findall(_strip_non_number_tokens(e.get("quote") or "")):
                try:
                    allowed.extend(_candidates(_parse(token)))
                except ValueError:
                    pass
    return allowed


def _loose_numbers(notes_entries: Iterable[dict]) -> List[float]:
    """Results of calculations, matched with a small relative tolerance: a model that
    rounds a growth factor by hand before multiplying is not inventing a figure."""
    out: List[float] = []
    for e in notes_entries:
        if e["source_type"] == "calculation":
            out.extend(_candidates(e.get("value")))
    return out


def _decimals(token: str) -> int:
    digits = _SUFFIX.sub("", token.strip().replace("$", "").replace(",", "").strip("()"))
    return len(digits.split(".")[1]) if "." in digits else 0


def _matches(value: float, places: int, allowed: Iterable[float]) -> bool:
    """A figure written to `places` decimals matches a recorded value if it is
    within half a unit of that last place (so 391.04 matches 391.035, and
    2.0% matches 0.02022)."""
    tolerance = 0.5 * 10 ** (-places) + 1e-9
    return any(abs(value - c) <= tolerance for c in allowed)


# Words, not figures: "5-year", "3-month", "10-K-style" handled above; list numbering "2." at
# the start of a line; simple fractions and exponents "1/5", "(1/5)"; ordinals "3rd"; "Step 1".
_DURATION = re.compile(r"\b\d{1,3}-(?:year|yr|month|quarter|week|day|hour|minute|point|digit|for)\w*\b", re.IGNORECASE)
_LIST_MARKER = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?\(?\d{1,2}[.):]\s")
_FRACTION = re.compile(r"(?<![\d,.])\(?\d{1,3}\s*/\s*\d{1,3}\)?(?![\d,.])")
_ORDINAL = re.compile(r"\b\d{1,3}(?:st|nd|rd|th)\b", re.IGNORECASE)
_STEPISH = re.compile(r"\b(?:Step|Phase|Stage|Tier|Level|Page|Table|Exhibit|Ex\.?|Figure|Column|Row|Line|Footnote)\s+\d{1,3}\b", re.IGNORECASE)


# Identifiers, not figures (5 Nov: 'Company CIK | 0001612720' and 'next-20250630.htm' were refused as unsourced
# figures, and a correct filing-identity answer went out unchecked after 16 turns).
_CODE_SPAN = re.compile(r"`[^`\n]{1,200}`")
_FILENAME = re.compile(r"[\w.-]*\d[\w.-]*\.(?:htm|html|xml|txt|pdf|xsd|json|jpg|jpeg|png|gif|xls|xlsx|zip)\b", re.IGNORECASE)
_PADDED_CIK = re.compile(r"(?<![\d,.$-])0\d{9}(?![\d,.-])")


def _strip_non_number_tokens(text: str) -> str:
    text = _LIST_MARKER.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _DURATION.sub(" ", text)
    text = _FRACTION.sub(" ", text)
    text = _ORDINAL.sub(" ", text)
    text = _STEPISH.sub(" ", text)
    text = _ACCESSION.sub(" ", text)
    text = _DATE.sub(" ", text)
    text = _TIME.sub(" ", text)
    text = _YEARISH.sub(" ", text)
    text = _FORMISH.sub(" ", text)
    text = _CODE_SPAN.sub(" ", text)  # after URLs and accession numbers are gone, so they are not split
    text = _FILENAME.sub(" ", text)
    text = _PADDED_CIK.sub(" ", text)
    return text


_SUFFIX = re.compile(r"(?:%|x|bps|[KMBTkmbt]n?|bn|mm|MM)$")


def _parse(token: str) -> float:
    cleaned = token.strip().replace("$", "").replace(",", "")
    cleaned = _SUFFIX.sub("", cleaned.rstrip(")")) + (")" if cleaned.endswith(")") else "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    value = float(cleaned)
    return -value if negative else value


_SECTION_HEADING = re.compile(r"^(?:#{1,6}\s+\S|\*\*[^*\n]{3,80}\*\*:?\s*$|[A-Z][A-Za-z ,'&/()-]{2,60}:?\s*$)", re.M)


def split_non_sec(answer: str) -> Tuple[str, str]:
    """The 'From non-SEC sources' part is a SECTION, not the rest of the document: it runs from its heading to the
    next heading (markdown #, a bold line, or a plain title line). So a results table built on share prices can
    come first, under its label, and the filing figures below it are still checked (1 Nov: the layout the user
    wants is conclusion and results first, inputs after)."""
    main_parts: List[str] = []
    non_sec_parts: List[str] = []
    pos = 0
    for m in NON_SEC_HEADING.finditer(answer):
        if m.start() < pos:
            continue
        main_parts.append(answer[pos:m.start()])
        rest = answer[m.end():]
        nxt = None
        for h in _SECTION_HEADING.finditer(rest):
            if h.start() > 0 and not NON_SEC_HEADING.match(rest[h.start():h.end() + 40]):
                nxt = h.start()
                break
        end = m.end() + (nxt if nxt is not None else len(rest))
        non_sec_parts.append(answer[m.start():end])
        pos = end
    main_parts.append(answer[pos:])
    return "\n".join(main_parts), "\n".join(non_sec_parts)


def check_answer(answer: str, notes_entries: Iterable[dict], mode: str = "strict", question: str = "") -> List[SourcingProblem]:
    """Return the figures in the main answer that no filing fact or
    calculation in the notes accounts for. Empty list means the answer passes.

    Numbers inside a "From non-SEC sources" section are exempt in extended
    mode and forbidden in strict mode. Dates, years, accession numbers, URLs
    and note references like [3] are not treated as figures.
    """
    entries = list(notes_entries)
    allowed = _allowed_numbers(entries, mode)
    loose = _loose_numbers(entries)
    non_sec_values: List[float] = []  # recorded web / price figures: allowed only under the heading
    for e in entries:
        if e["source_type"] not in ("filing", "calculation"):
            non_sec_values.extend(_candidates(e.get("value")))
    # Numbers the question itself uses ("5-year CAGR", "top 3", "Series 46") may be echoed.
    for token in _NUMBER.findall(_strip_non_number_tokens(question or "")):
        try:
            allowed.extend(_candidates(_parse(token)))
        except ValueError:
            pass
    problems: List[SourcingProblem] = []
    by_id = {e.get("id"): e for e in entries}

    main, non_sec = split_non_sec(answer)
    if mode == "strict" and non_sec.strip():
        problems.append(SourcingProblem("(section)", "From non-SEC sources", "a non-SEC section is not allowed in strict mode"))

    for original_line in main.splitlines():
        raw_line = _attach_units(original_line)
        # Referenced figures first: "$2,241,236,000 [3]" is checked against note 3 itself. A segment runs
        # from the previous reference (or the line start) to this one; its numbers may match any note cited.
        referenced_ok: set = set()
        cite_hint: dict = {}
        pos = 0
        for ref in _REF.finditer(raw_line):
            segment = raw_line[pos:ref.start()]
            pos = ref.end()
            cited = [by_id.get(int(i)) for i in re.findall(r"\d+", ref.group(0))]
            cited_values: List[float] = []
            cited_loose: List[float] = []
            for e in cited:
                if not e:
                    continue
                cited_values.extend(_candidates(e.get("value")))
                if e.get("source_type") in ("calculation", "web_calculation"):
                    cited_loose.extend(_candidates(e.get("value")))
                if e.get("source_type") == "filing" and e.get("quote"):
                    for tok in _NUMBER.findall(_strip_non_number_tokens(str(e["quote"]))):
                        try:
                            cited_values.extend(_candidates(_parse(tok)))
                        except ValueError:
                            pass
            for token in _NUMBER.findall(_strip_non_number_tokens(segment)):
                try:
                    value = _parse(token)
                except ValueError:
                    continue
                if _matches(value, _decimals(token), cited_values) or any(c and abs(value - c) / abs(c) <= 0.001 for c in cited_loose):
                    referenced_ok.add(token)
                elif cited and all(e for e in cited):  # cited but not matching: the general check below decides, with this hint
                    cite_hint[token] = (f" It cites [{', '.join(str(e['id']) for e in cited)}], recorded as "
                                        f"{', '.join(str(e.get('value')) for e in cited)}: fix the figure or the reference.")
        line = _REF.sub(" ", raw_line)  # note references
        for token in _NUMBER.findall(_strip_non_number_tokens(line)):
            if token in referenced_ok:
                continue
            try:
                value = _parse(token)
            except ValueError:
                continue
            bare = token.strip("()")
            if abs(value) <= 20 and bare.isdigit() and "$" not in token and "%" not in token and "." not in token:
                continue  # "2 times", "4 quarters", "16 KPIs": counts, not financial figures
            if _is_label_like(token, value):
                continue  # "Form 425", "Item 7", "Schedule 14D-9", "Rule 10b5-1": a bare integer is a name, not a figure
            if _matches(value, _decimals(token), allowed):
                continue
            if any(c and abs(value - c) / abs(c) <= 0.001 for c in loose):
                continue  # within 0.1% of a calculated result
            if value < 0 and token.startswith("(") and token.endswith(")"):
                # "(1.19%)" is a negative in a table but plain parenthesis in prose: "$12,647 thousand (1.19%)"
                if _matches(-value, _decimals(token), allowed) or any(c and abs(-value - c) / abs(c) <= 0.001 for c in loose):
                    continue
            if _matches(value, _decimals(token), non_sec_values) or any(c and abs(value - c) / abs(c) <= 0.001 for c in non_sec_values):
                reason = ("this is a market-data / web figure (or derived from one): it may appear only below a line "
                          "'From non-SEC sources' at the end of the answer, not in the main answer"
                          if mode == "extended" else "this is a market-data / web figure: strict mode does not report it")
            else:
                reason = "no filing fact or calculation in the notes has this value" + cite_hint.get(token, "")
            problems.append(SourcingProblem(token, original_line.strip(), reason))
    return problems
