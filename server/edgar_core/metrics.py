"""Standard analyst conventions for named metrics (the form catalogue's sibling).

A model half-knows how analysts compute days inventory outstanding or a fixed
charge coverage ratio, and a benchmark grades the convention strictly. The
catalogue in data/metrics.json writes the conventions down once so the agent
can look them up instead of guessing. A definition given in the question or
in the filing always overrides the catalogue.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

_DATA = Path(__file__).with_name("data") / "metrics.json"


class MetricNotFoundError(KeyError):
    pass


@dataclass(frozen=True)
class Metric:
    name: str
    aliases: List[str]
    formula: str
    convention: str  # "standard" | "varies"
    definition: str = ""  # one plain sentence
    averages: str = ""
    includes: str = ""
    excludes: str = ""
    units: str = ""
    pitfalls: str = ""
    category: str = ""
    sources: str = ""  # where the definition comes from (CFA curriculum reading, or the practice it follows) and how ours differs

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}


@lru_cache(maxsize=1)
def _all() -> List[Metric]:
    data = json.loads(_DATA.read_text(encoding="utf-8"))
    return [Metric(**m) for m in data["metrics"]]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


_GENERIC_ALIASES = {"growth", "margin", "debt", "ev", "eps", "fcf", "capex", "dcf", "lbo", "wacc"}  # too common to count as naming a metric on their own


def metrics_in_text(text: str) -> List[Metric]:
    """The catalogue metrics a question names (by name or alias, as whole words), longest names first.
    Used to put their definitions in front of the model before it starts, instead of hoping it looks them up."""
    hay = " " + _norm(text) + " "
    found: List[Metric] = []
    for met in _all():
        names = [met.name] + [a for a in met.aliases if _norm(a) not in _GENERIC_ALIASES and len(_norm(a)) >= 3]
        if any(" " + _norm(n) + " " in hay for n in names if _norm(n)):
            found.append(met)
    return sorted(found, key=lambda x: -len(x.name))


def describe_metric(name: str) -> Metric:
    """Exact name or alias match first, then a word-overlap match on names,
    aliases and formulas, so 'DIO', 'inventory days' and 'days of inventory
    outstanding' all find the same entry."""
    q = _norm(name)
    if not q:
        raise MetricNotFoundError("empty metric name")
    for m in _all():
        if q == _norm(m.name) or any(q == _norm(a) for a in m.aliases):
            return m
    words = set(q.split())
    best, score = None, 0.0
    for m in _all():
        hay = {w for w in _norm(m.name).split()} | {w for a in m.aliases for w in _norm(a).split()}
        overlap = len(words & hay) / max(1, len(words))
        if overlap > score:
            best, score = m, overlap
    if best is None or score < 0.5:
        raise MetricNotFoundError(f"no catalogued metric matches {name!r}; try list_metrics")
    return best


def list_metrics(category: Optional[str] = None) -> List[dict]:
    return [{"name": m.name, "aliases": m.aliases, "convention": m.convention, "category": m.category}
            for m in _all() if not category or m.category == category]


def categories() -> List[str]:
    return sorted({m.category for m in _all() if m.category})


def render_reference(metrics: Optional[List[Metric]] = None) -> str:
    """The metric catalogue as a Markdown reference document (docs/METRIC_DEFINITIONS.md).
    Generated from data/metrics.json so the document and the agent can never disagree."""
    items = metrics if metrics is not None else _all()
    lines = ["# Metric definitions used by the EDGAR agent", "",
             "Generated from `edgar_core/data/metrics.json`; do not edit by hand (run `python -m edgar_core.metrics > docs/METRIC_DEFINITIONS.md`).", "",
             "Every named metric the agent computes follows the definition below unless the question or the filing defines it otherwise; "
             "the agent states the definition it used in its answer. Where the CFA Program curriculum defines the metric, the source line names "
             "the reading and says whether our formula is identical or how it differs. Metrics the CFA curriculum does not define say so and name "
             "the practice they follow. 'Convention: standard' means analysts agree on the formula; 'varies' means reasonable desks differ and the "
             "answer must say which variant it used.", ""]
    by_cat: dict = {}
    for met in items:
        by_cat.setdefault(met.category or "other", []).append(met)
    for cat in sorted(by_cat):
        lines += [f"## {cat.capitalize()}", ""]
        for met in sorted(by_cat[cat], key=lambda x: x.name.lower()):
            lines += [f"### {met.name}", ""]
            if met.aliases:
                lines.append(f"Also called: {', '.join(met.aliases)}.  ")
            if met.definition:
                lines.append(f"**Definition.** {met.definition}  ")
            lines.append(f"**Formula.** {met.formula}  ")
            lines.append(f"**Convention.** {met.convention}" + (f"; {met.averages}" if met.averages else "") + "  ")
            if met.includes:
                lines.append(f"**Includes.** {met.includes}  ")
            if met.excludes:
                lines.append(f"**Excludes.** {met.excludes}  ")
            if met.units:
                lines.append(f"**Units.** {met.units}  ")
            if met.pitfalls:
                lines.append(f"**Pitfalls.** {met.pitfalls}  ")
            lines.append(f"**Source.** {met.sources or 'not recorded'}")
            lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":  # python -m edgar_core.metrics > docs/METRIC_DEFINITIONS.md
    print(render_reference())
