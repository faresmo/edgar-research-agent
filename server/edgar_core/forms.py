"""A catalogue of EDGAR form types: what each one is, who files it, what it
contains, where the substance sits inside the filing, and how this library
reads it. The data is in data/forms.json.

Two uses. `describe_form("DEF 14A")` tells the agent what it is about to open.
`forms_for("director compensation")` tells it where to look for a topic. Any
form not in the catalogue can still be read with the generic tools
(list_documents, find_in_document, read_document, get_table).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_DATA = Path(__file__).with_name("data") / "forms.json"
_WORD = re.compile(r"[a-z0-9][a-z0-9.\-]*")
_STOP = {"the", "a", "an", "of", "for", "and", "or", "in", "on", "to", "what", "is", "are", "was", "were",
         "how", "much", "many", "does", "did", "do", "by", "with", "as", "at", "its", "it", "this", "that"}
GENERIC_READ_WITH = ["list_documents", "find_in_document", "read_document", "get_table"]


@dataclass(frozen=True)
class FormInfo:
    form: str
    name: str
    category: str
    filer: str
    cadence: str
    contents: str
    substance_in: str
    format: str
    answers: List[str] = field(default_factory=list)  # topics the form answers
    read_with: List[str] = field(default_factory=list)  # library functions to use
    notes: str = ""
    variants: List[str] = field(default_factory=list)
    tested: bool = False  # this library has been run on a real filing of this type
    catalogued: bool = True  # False: known by name only (the "tail")

    def to_dict(self) -> dict:
        return asdict(self)


def _load() -> dict:
    return json.loads(_DATA.read_text(encoding="utf-8"))


_CACHE: Optional[dict] = None


def _catalogue() -> dict:
    global _CACHE
    if _CACHE is None:
        raw = _load()
        rich = {e["form"].upper(): FormInfo(**e) for e in raw["entries"]}
        tail = {k.upper(): FormInfo(form=k, name=v, category="other", filer="", cadence="", contents=v,
                                    substance_in="main document", format="", read_with=GENERIC_READ_WITH,
                                    catalogued=False)
                for k, v in raw["tail"].items() if k.upper() not in rich}
        # Variants ("10-K/A", "424B5") point at their parent entry.
        aliases: Dict[str, str] = {}
        for key, info in rich.items():
            for v in info.variants:
                code = v.split(" (")[0].strip().upper()
                aliases.setdefault(code, key)
        _CACHE = {"rich": rich, "tail": tail, "aliases": aliases, "source": raw.get("source", "")}
    return _CACHE


def normalize_form(form: str) -> str:
    """'10k' -> '10-K', 'def14a' -> 'DEF 14A', '424b5' -> '424B5', '8-K/A' kept."""
    text = (form or "").strip().upper().replace("FORM ", "")
    if not text:
        raise ValueError("form is empty")
    cat = _catalogue()
    candidates = [text, text.replace(" ", "-"), text.replace("-", " ")]
    compact = re.sub(r"[\s\-]", "", text)
    for key in list(cat["rich"]) + list(cat["aliases"]) + list(cat["tail"]):
        if re.sub(r"[\s\-]", "", key) == compact:
            candidates.append(key)
    for c in candidates:
        if c in cat["rich"] or c in cat["aliases"] or c in cat["tail"]:
            return c
    return text


def describe_form(form: str) -> FormInfo:
    """What a form type is and how to read it. Unknown forms get a generic
    entry rather than an error, so the agent can still open them."""
    key = normalize_form(form)
    cat = _catalogue()
    if key in cat["rich"]:
        return cat["rich"][key]
    if key in cat["aliases"]:
        parent = cat["rich"][cat["aliases"][key]]
        kind = "amendment" if key.endswith("/A") else f"variant {key}"
        return FormInfo(**{**parent.to_dict(), "form": key, "name": f"{parent.name} ({kind} of {parent.form})"})
    if key in cat["tail"]:
        return cat["tail"][key]
    base = key.split("/")[0]
    if base != key and base in cat["rich"]:  # "SC 13D/A" -> amendment of SC 13D
        parent = cat["rich"][base]
        return FormInfo(**{**parent.to_dict(), "form": key, "name": f"{parent.name} (amendment)"})
    return FormInfo(form=key, name="Unknown form type", category="other", filer="", cadence="",
                    contents="Not in the catalogue. Open it with the generic tools and read the cover page.",
                    substance_in="main document", format="", read_with=GENERIC_READ_WITH, catalogued=False)


def forms_for(topic: str, limit: int = 5) -> List[dict]:
    """Which forms answer a topic. Returns [{form, name, score, matched}] best
    first. Scores: a hit in the form's 'answers' list counts most, then the
    name, then the contents text."""
    words = [w for w in _WORD.findall((topic or "").lower()) if w not in _STOP]
    if not words:
        raise ValueError("topic is empty")
    results = []
    for info in _catalogue()["rich"].values():
        if not info.answers:
            continue
        answers = " ".join(info.answers).lower()
        name = info.name.lower()
        contents = (info.contents + " " + info.notes).lower()
        score, matched = 0, []
        for w in words:
            if re.search(r"\b" + re.escape(w) + r"s?\b", answers):
                score += 3; matched.append(w)
            elif re.search(r"\b" + re.escape(w) + r"s?\b", name):
                score += 2; matched.append(w)
            elif re.search(r"\b" + re.escape(w) + r"s?\b", contents):
                score += 1; matched.append(w)
        if score:
            # Reward matching more of the question's words over one strong word.
            score += len(set(matched)) * 2
            results.append({"form": info.form, "name": info.name, "score": score, "matched": sorted(set(matched)),
                            "read_with": info.read_with, "substance_in": info.substance_in})
    results.sort(key=lambda r: (-r["score"], r["form"]))
    return results[:limit]


def list_forms(category: Optional[str] = None, catalogued_only: bool = True) -> List[dict]:
    """[{form, name, category, tested}] for a category, or all."""
    cat = _catalogue()
    items = list(cat["rich"].values()) + ([] if catalogued_only else list(cat["tail"].values()))
    if category:
        items = [i for i in items if i.category == category]
    return [{"form": i.form, "name": i.name, "category": i.category, "tested": i.tested} for i in items]


def categories() -> List[str]:
    return sorted({i.category for i in _catalogue()["rich"].values()})
