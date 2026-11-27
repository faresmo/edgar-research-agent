"""Our own question set (SPEC.md section 9).

Two parts:

1. Generated questions with code-verified answers. Each asks for an XBRL
   figure (revenue, net income, assets...) for a company and period chosen
   at random from a list of large filers. The expected answer is fetched from
   the SEC by the library at generation time, so scoring is an exact check
   with no judge model involved, and a fresh batch can be generated whenever
   the old one has been seen too often.

2. Hand-written questions in evals/own_handwritten.csv, in the same shape as
   the public set (question, answer, type, rubric), on companies and periods
   the public set does not use. Half are held out: see the "held_out" column.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import edgar_core as core

from .sourcing import _NUMBER, _candidates, _decimals, _matches, _parse, _strip_non_number_tokens

# Large, long-listed filers with regular 10-Ks. Not used by the public set's
# questions except where unavoidable (Apple, Microsoft).
COMPANIES = ["MSFT", "AAPL", "JNJ", "PG", "KO", "PEP", "WMT", "COST", "HD", "LOW", "MCD", "SBUX", "NKE", "DIS", "CMCSA",
             "VZ", "T", "IBM", "INTC", "CSCO", "ORCL", "ADBE", "CRM", "NVDA", "AMD", "QCOM", "TXN", "AVGO", "AMZN", "GOOGL",
             "META", "NFLX", "XOM", "CVX", "COP", "CAT", "DE", "HON", "GE", "MMM", "BA", "LMT", "RTX", "UPS", "FDX",
             "UNH", "CVS", "PFE", "MRK", "ABBV", "LLY", "AMGN", "GILD", "TMO", "DHR", "MDT", "ABT", "F", "GM", "TSLA"]

CONCEPTS: Dict[str, str] = {
    "revenue": "total revenue",
    "net_income": "net income",
    "total_assets": "total assets",
    "operating_cash_flow": "net cash provided by operating activities",
    "eps_diluted": "diluted earnings per share",
    "cash": "cash and cash equivalents",
}


@dataclass
class OwnQuestion:
    id: str
    question: str
    kind: str  # single | series | change
    ticker: str
    company: str
    concept: str
    as_of: str
    expected: List[dict]  # [{"period_end", "value", "unit", "accession_number", "form"}]
    expected_change: Optional[float] = None  # fraction, for "change" questions
    rubric: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__


def generate(n: int, seed: Optional[int] = None, as_of: Optional[str] = None, client=None,
             companies: Optional[List[str]] = None, progress=None) -> List[OwnQuestion]:
    """Make `n` questions with answers fetched from the SEC. Needs network."""
    rng = random.Random(seed)
    client = client or core.EdgarClient()
    as_of = as_of or time.strftime("%Y-%m-%d")
    say = progress or (lambda m: None)
    pool = list(companies or COMPANIES)
    out: List[OwnQuestion] = []
    attempts = 0
    while len(out) < n and attempts < n * 6:
        attempts += 1
        ticker = rng.choice(pool)
        concept = rng.choice(list(CONCEPTS))
        kind = rng.choice(["single", "single", "series", "change"])
        try:
            cik = core.ticker_to_cik(ticker, client=client)
            profile = core.get_entity_profile(cik, client=client)
            facts = core.get_financial_fact(cik, concept, period="annual", limit=12, as_of=as_of, client=client)
        except core.EdgarError as exc:
            say(f"skip {ticker} {concept}: {exc}")
            continue
        facts = [f for f in facts if f.period_end >= "2010-01-01"]
        if len(facts) < 2:
            continue
        name = f"{_display_name(profile.name, ticker)} ({ticker})"
        label = CONCEPTS[concept]
        unit_note = " (in dollars, as reported)" if concept != "eps_diluted" else ""
        if kind == "single":
            f = rng.choice(facts[:8])
            q = OwnQuestion(id=f"own-{len(out) + 1:03d}", kind="single", ticker=ticker, company=name, concept=concept, as_of=as_of,
                            question=f"What was {name}'s {label} for the fiscal year ended {f.period_end}?{unit_note}",
                            expected=[_exp(f)])
        elif kind == "series":
            k = rng.randint(2, min(6, len(facts)))
            chosen = sorted(facts[:k], key=lambda f: f.period_end)
            y1, y2 = chosen[0].period_end, chosen[-1].period_end
            q = OwnQuestion(id=f"own-{len(out) + 1:03d}", kind="series", ticker=ticker, company=name, concept=concept, as_of=as_of,
                            question=(f"List {name}'s {label} for each fiscal year from the one ended {y1} to the one ended {y2} "
                                      f"({len(chosen)} years), one line per year with the fiscal year end date.{unit_note}"),
                            expected=[_exp(f) for f in chosen])
        else:
            if concept == "eps_diluted":
                continue
            later, earlier = facts[0], rng.choice(facts[1:6])
            if not earlier.value or earlier.value <= 0:
                continue
            change = (later.value - earlier.value) / abs(earlier.value)
            q = OwnQuestion(id=f"own-{len(out) + 1:03d}", kind="change", ticker=ticker, company=name, concept=concept, as_of=as_of,
                            question=(f"By what percentage did {name}'s {label} change from the fiscal year ended {earlier.period_end} "
                                      f"to the fiscal year ended {later.period_end}? Give both figures and the percentage to two decimals."),
                            expected=[_exp(earlier), _exp(later)], expected_change=round(change, 6))
        q.rubric = [{"operator": "correctness", "criteria": f"{label} {e['period_end']}: {e['value']:,}"} for e in q.expected]
        if q.expected_change is not None:
            q.rubric.append({"operator": "correctness", "criteria": f"change: {q.expected_change * 100:.2f}%"})
        out.append(q)
        say(f"{q.id} {ticker} {concept} {kind}")
    return out


def _exp(f) -> dict:
    return {"period_end": f.period_end, "value": f.value, "unit": f.unit, "accession_number": f.accession_number, "form": f.form}


_SMALL = {"of", "and", "the", "&"}
_ACRONYMS = {"GE", "IBM", "AT&T", "UPS", "AMD", "HP", "3M", "CVS", "CSX", "PNC", "KKR", "RTX", "UNH"}


def _display_name(name: str, ticker: str = "") -> str:
    """'HOME DEPOT, INC.' -> 'Home Depot Inc'; 'COSTCO WHOLESALE CORP /NEW' -> 'Costco Wholesale Corp'."""
    name = re.sub(r"\s*/\s*\w+\s*$", "", name)  # trailing '/NEW', '/DE'
    name = name.replace(",", "").replace(".", "")
    words = []
    for w in name.split():
        lw = w.lower()
        if lw in _SMALL:
            words.append(lw)
        elif w.isupper() and (w == ticker or w in _ACRONYMS):
            words.append(w)
        else:
            words.append(w.capitalize())
    return " ".join(words)


def save(questions: List[OwnQuestion], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        for q in questions:
            handle.write(json.dumps(q.to_dict()) + "\n")


def load(path: Path) -> List[OwnQuestion]:
    out = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            out.append(OwnQuestion(**d))
    return out


def score_exact(question: OwnQuestion, answer: str) -> dict:
    """Code-only scoring: each expected figure must appear in the answer (in
    any scale, within rounding), and the change percentage if asked. Also
    reports whether an expected accession number is cited."""
    text = answer or ""
    numbers = []
    for token in _NUMBER.findall(_strip_non_number_tokens(text)):
        try:
            numbers.append((_parse(token), _decimals(token)))
        except ValueError:
            pass
    results = []
    for e in question.expected:
        hit = any(_matches(n, places, _candidates(e["value"])) for n, places in numbers)
        results.append({"criterion": f"{e['period_end']}: {e['value']:,}", "met": hit})
    if question.expected_change is not None:
        pct = question.expected_change * 100
        # "a decrease of 56.46%" states a negative change in words, so a
        # negative expectation also accepts the unsigned figure when the text
        # says the figure fell.
        says_fell = pct < 0 and re.search(r"\b(decreas|declin|fell|fall|drop|lower|down|reduc|negative)", text, re.I) is not None
        hit = any(abs(n - pct) <= 0.051 or abs(n - question.expected_change) <= 0.00051
                  or (says_fell and (abs(n + pct) <= 0.051 or abs(n + question.expected_change) <= 0.00051)) for n, _ in numbers)
        results.append({"criterion": f"change {pct:.2f}%", "met": hit})
    cited = any(e["accession_number"] and e["accession_number"] in text for e in question.expected)
    return {"criteria": results, "met": sum(r["met"] for r in results), "total": len(results), "cited": cited}
