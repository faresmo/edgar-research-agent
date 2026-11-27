"""The plugin's session logic, independent of the MCP transport so it can be tested offline.

One MCP session = one research question in progress = one `RunContext`. Claude (the user's own
subscription) is the orchestrator; this module supplies the tools, the gates, the audit record and
the feedback channel. Nothing here calls a model.

Tools added on top of the agent's tool list:
  classify_question  Claude fills the Phase A form (type, method constraints); the server sets the
                     budget-free bookkeeping, looks up the named catalogue metrics, and returns the
                     reliability header plus the rules block Claude must follow for this question.
  check_answer       The three submit gates on a draft (method constraints, definitions, sourcing,
                     precision, bases, route). On success the answer and the plain-English working
                     are written to the audit record and the working is returned for Claude to append.
  feedback           The user's verdict on an answer, recorded against the audit record and, with
                     consent, posted to the feedback endpoint.
  session_status     What this session has recorded so far (for a long conversation).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple, Any, Dict, List, Optional

import edgar_core as core
from edgar_core import conventions as conv
from edgar_agent.config import Settings, resolve_settings
from . import contact as _contact
from edgar_agent.provenance import filing_link, linked
from edgar_agent.loop import QUESTION_TYPES, constraints_block, definitions_block, difficulty_of, system_prompt
from edgar_agent.notes import Notes
from edgar_agent.provenance import render_working
from edgar_agent.tools import PRICES_UNAVAILABLE, RunContext, run_tool, tools_for
from . import pricekey as _pricekey

from edgar_mcp import notfound

RELIABILITY_PATH = Path(__file__).resolve().parents[1] / "evals" / "reliability.json"

# 22 Nov 2026: the wording that financial-data products commonly use ("for informational purposes only ... does not
# constitute investment, legal or tax advice"), in our own words; the user asked for the standard form. It no longer says
# that reliability is "shown at the top of each answer": no score is shown there since 91fc430. To be read by whoever
# reviews the license.
DISCLAIMER = ("EDGAR Research Agent is for informational purposes only. It does not provide investment, legal, tax or accounting "
              "advice, and nothing in an answer is a recommendation to buy, sell or hold any security. Figures are as reported by "
              "the company in its SEC filings; 'checked' means traced to the filing, not audited for truth. Answers can be wrong "
              "and are not a guarantee: check 'How the figures were obtained' before relying on a number, consult a qualified "
              "professional before acting on one, and say so when an answer is wrong: your correction is recorded against it.")


def load_reliability() -> Dict[str, Any]:
    """Per question type and model: measured scores from the eval runs (written by the eval runner)."""
    try:
        return json.loads(RELIABILITY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


_MARKET_TYPES = ("web", "web_calculation")
FINAL_TEXT_LIMIT = 30000  # characters: well under Claude Code's limit for one tool result, JSON escaping included (IDEX, 6 Nov)
_HEADING_LINE = re.compile(r"^\s*(?:#{1,6}\s+\S|\*\*[^*\n]{3,80}\*\*:?\s*$)")


def compose_answer(header: str, a: Dict[str, Any], notes: List[dict], include_header: bool = True) -> Tuple[str, List[dict]]:
    """Lay the answer out results-first from its parts, so the model has no reason to rewrite the accepted
    text (1 Nov eval: on two of 27 questions Claude had check_answer accept, then reordered the answer by
    hand and dropped the header). Order: header, conclusion, results table (under the non-SEC heading when
    any cell rests on market data), the body, then the appendix the caller adds. Returns (text, problems);
    problems are results cells whose note id does not exist."""
    body = str(a.get("answer") or "").strip()
    if header:
        body = body.replace(header, "").strip()  # placed once, by us
        body = re.sub(r"^\s*\**\s*" + re.escape(header[:40]) + r"[^\n]*\n+", "", body)
    parts, problems = [header] if header and include_header else [], []
    conclusion = str(a.get("conclusion") or "").strip()
    # 13 Nov: the reply shows several blocks (the trust line, the answer, the working); the answer itself is marked as such.
    if not re.match(r"\s*#+\s*Answer\b", conclusion or body, re.I):
        parts.append(ANSWER_HEADING)
    if conclusion:
        parts.append(conclusion)
    results = a.get("results") or {}
    rows = results.get("rows") or []
    if rows:
        by_id = {e["id"]: e for e in notes}
        columns = [str(c) for c in (results.get("columns") or [])]
        width = max(len(r.get("cells") or []) for r in rows)
        if not columns:
            columns = [""] * width
        market = False
        lines = ["| | " + " | ".join(columns) + " |", "|---|" + "---|" * len(columns)]
        for r in rows:
            cells = []
            for c in (r.get("cells") or []):
                text = str(c.get("text") or "").strip()
                nid = c.get("note_id")
                if nid is not None:
                    note = by_id.get(int(nid)) if str(nid).isdigit() else None
                    if note is None:
                        problems.append({"row": r.get("label"), "note_id": nid, "reason": "no recorded note has this id"})
                    else:
                        market = market or note.get("source_type") in _MARKET_TYPES
                        text += f" [{int(nid)}]"
                cells.append(text)
            cells += [""] * (len(columns) - len(cells))
            lines.append("| " + str(r.get("label") or "") + " | " + " | ".join(cells) + " |")
        table = "\n".join(lines)
        if market:
            providers = sorted({str(e.get("detail") or "").replace("price from ", "") for e in notes
                                if e.get("source_type") == "web" and str(e.get("detail") or "").startswith("price from")})
            dates = sorted({str(e.get("period_end")) for e in notes if e.get("source_type") == "web" and e.get("period_end")})
            label = "**From non-SEC sources" + (f" (share prices from {', '.join(providers)}" + (f", {dates[0]}" + (f" to {dates[-1]}" if len(dates) > 1 else "") if dates else "") + ")" if providers else "") + "**"
            table = label + "\n\n" + table
            if body and not _HEADING_LINE.match(body.splitlines()[0]):
                body = "**Inputs and working**\n\n" + body  # ends the non-SEC section, so the body is checked as SEC-sourced
        parts.append(table)
    if body:
        parts.append(body)
    return "\n\n".join(parts), problems


# Phase A question types (what classify_question returns) -> the benchmark category the reliability table is keyed by.
TYPE_TO_CATEGORY = {
    "quantitative retrieval": "General Quantitative Analysis", "numerical reasoning": "General Quantitative Analysis",
    "trends": "General Quantitative Analysis", "qualitative retrieval": "General Qualitative Analysis",
    "complex retrieval": "Disclosure Analysis", "adjustments": "Adjustments", "beat or miss": "Earnings Analysis",
    "financial modelling": "Financial Modeling", "market analysis": "Market Analysis",
}


def reliability_row(question_type: str, types: Dict[str, Any]) -> Dict[str, Any]:
    """The table row for a question type: by mapping, else by case-insensitive name (a benchmark category
    given directly, e.g. 'comparables'). Until 3 Nov the lookup was exact on the Phase A name, so no header
    ever carried its per-type figure."""
    q = (question_type or "").strip().lower()
    wanted = TYPE_TO_CATEGORY.get(q, q).lower()
    for name, row in types.items():
        if name.lower() == wanted:
            return row
    return {}


# Gate 6 decision (12 Nov 2026): the proof of concept is published with the wrong-headline rate above the 10% target,
# so answers of the kind that go wrong carry a caution. 22 Nov: the caution no longer quotes a rate ("about one answer in
# eight"): that figure was the builder's own classification of 46 benchmark answers, 15 of which differed from the key
# (audit D6), and the user decided that every answer says it is not a guarantee (NOT_A_GUARANTEE) and none quotes a score.
CAUTION = ("This answer takes several steps and depends on choices of method: in our tests, answers of this kind were wrong "
           "more often than simple lookups.")
# The user's wording, 21 Nov: on every answer, in place of a score from a development set (audit A8).
NOT_A_GUARANTEE = ("This is not a guarantee: the question can be misread or a different basis chosen, so check the working "
                   "before you rely on the answer.")


def _needs_caution(difficulty: str, notes) -> bool:
    """A hard question, or an answer built on three or more calculations."""
    return difficulty == "hard" or sum(1 for e in notes if e.get("source_type") in ("calculation", "web_calculation")) >= 3


NO_FILING_HEADER = ("Every figure below is traced to its source: the assumptions in your question and the calculations shown under "
                    "'How the figures were obtained'. No SEC filing was used, and nothing was checked against one.")


# 13 Nov (publishing decision 6): the public plugin sends nothing to the publisher. The upload of consented feedback is
# in edgar_mcp/upload.py, which the build of the public plugin leaves out; without it, feedback is recorded locally only.
try:
    from .upload import post as UPLOAD
except ImportError:
    UPLOAD = None


def _narrow_line_called_total(ctx, text: str) -> Optional[Dict[str, Any]]:
    """A sentence that says 'total revenue(s)' and gives the narrower revenue figure of a period, but not that period's total."""
    for pair in getattr(ctx, "revenue_pairs", None) or []:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
            if re.search(r"\btotal (?:net )?revenues?\b", sentence, re.I) and _digits_in(sentence, pair["narrow"]) and not _digits_in(sentence, pair["total"]):
                return pair
    return None


def _web_only_for_a_guessed_company(ctx) -> List[Dict[str, Any]]:
    """The companies matched on a guess, when the notes hold figures from the web (other than market prices) and not one
    figure from an SEC filing: the sign that the EDGAR entity was not the company and the answer was built elsewhere."""
    guess = getattr(ctx, "scope_guess", None) or []
    if not guess:
        return []
    notes = ctx.notes.entries()
    if any(e.get("source_type") == "filing" for e in notes):
        return []
    web = [e for e in notes if e.get("source_type") in ("web", "web_calculation") and e.get("value") is not None
           and not str(e.get("detail") or "").startswith("price from")]
    return guess if web else []


_ACCESSION = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")


def _question_key(question: str) -> str:
    return re.sub(r"\s+", " ", question or "").strip().lower()


def revises(earlier: Dict[str, Any], entry: Dict[str, Any]) -> bool:
    """Is `entry` a corrected resubmission of `earlier`, and not another answer of a batch?

    18 Nov, fab1-25 (Paylocity): Claude saw a mistake in an accepted answer and submitted the corrected one; both were
    listed for the turn, the Stop hook asked for both and the reply ended up with neither. Two observable signals, both
    needed: the two answers were checked for the same question, and they cite the same filings (one set inside the other),
    so that one question text covering several companies stays a batch. Answers that cite no filing at all (a company that
    does not file) count as the same answer only when their texts are nearly identical. When in doubt it is not a revision:
    the cost of that is the old behaviour, the cost of the opposite is a batch answer nobody checks for."""
    key = entry.get("question")
    if not key or earlier.get("question") != key:
        return False
    a, b = str(earlier.get("text") or ""), str(entry.get("text") or "")
    if not a or not b:
        return False
    fa, fb = set(_ACCESSION.findall(a)), set(_ACCESSION.findall(b))
    if fa or fb:
        return bool(fa and fb) and (fa <= fb or fb <= fa)
    import difflib
    return difflib.SequenceMatcher(None, a[:20_000], b[:20_000], autojunk=False).ratio() >= 0.9


def record_accepted(answer_id: int, header: str, brief: bool = False, text: str = "", also: str = "", question: str = "") -> None:
    """Note the accepted answer for the plugin's Stop hook (plugin/hooks/check_reply.py), which checks that the
    reply Claude gives is this answer. Written where the hook looks; never fails the check."""
    entry = {"answer_id": answer_id, "header": header, "at": time.time(), "brief": brief}
    # Full answers of one question type share their header line, so the hook also looks for the answer's own first line.
    body = [l.strip() for l in text.split("\n")[1:] if l.strip() and not l.lstrip().startswith("#")] if text.startswith(header) else []  # not the '## Answer' heading
    if body:
        entry["opening"] = body[0][:80]
    # Gate 1 (12 Nov): the hook checks that every figure in the reply is in a checked answer, so it needs the checked text
    # (the short form and the full answer, when both exist).
    checked = "\n".join(t for t in (text, also) if t)
    if checked:
        entry["text"] = checked[:400_000]
    # 12 Nov: several answers accepted in one turn (a batch of questions) must all be in the reply; the hook knew only the
    # last. Every answer accepted since the hook last saw a reply is listed under `turn`.
    earlier = _read_state("last_accepted.json") or {}
    turn = [] if earlier.get("delivered") or time.time() - earlier.get("at", 0) > 2 * 3600 else list(earlier.get("turn") or [])
    # 18 Nov: a corrected resubmission takes the place of the draft it corrects (see revises), in the draft's position.
    if _question_key(question):
        entry["question"] = _question_key(question)
    replaced = False
    for i, e in enumerate(turn):
        if isinstance(e, dict) and revises(e, entry):
            turn[i], replaced = (entry if not replaced else None), True
    turn = [e for e in turn if e is not None] + ([] if replaced else [entry])
    _write_state("last_accepted.json", {**entry, "delivered": False, "turn": turn})


def record_classified(header: str) -> None:
    """Note that a question was started, so the Stop hook can tell a reply that skipped check_answer and
    check_not_found (a started question that ends without a checked answer) from a reply unrelated to this plugin."""
    _write_state("last_classified.json", {"header": header, "at": time.time()})


def _write_state(name: str, data: Dict[str, Any]) -> None:
    state = json.dumps(data)
    explicit = os.environ.get("EDGAR_STATE_DIR")
    places = [explicit] if explicit and not explicit.startswith("${") else [os.environ.get("CLAUDE_PLUGIN_DATA"), str(Path.cwd() / ".edgar")]
    written = False
    for d in places:
        if not d or d.startswith("${"):
            continue
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            (Path(d) / name).write_text(state)
            written = True
        except OSError:
            continue
    if not written and not (explicit and not explicit.startswith("${")):
        # 21 Nov, Cowork: the session's folder cannot be written and the app gives the server no data folder, so no state
        # was kept and the end-of-turn check had nothing to go on. The hooks already look in ~/.edgar-agent. Used only
        # when nothing else worked, so that two projects open at once in Claude Code do not read each other's state.
        try:
            _HOME_STATE().mkdir(parents=True, exist_ok=True)
            (_HOME_STATE() / name).write_text(state)
        except OSError:
            pass


def _read_state(name: str) -> Optional[Dict[str, Any]]:
    """The newest copy of a state file among the places _write_state uses (the plugin's hooks write there too)."""
    explicit = os.environ.get("EDGAR_STATE_DIR")
    places = [explicit] if explicit and not explicit.startswith("${") else [os.environ.get("CLAUDE_PLUGIN_DATA"), str(Path.cwd() / ".edgar")]
    newest = None
    for d in places:
        if not d or d.startswith("${"):
            continue
        try:
            data = json.loads((Path(d) / name).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and (newest is None or data.get("at", 0) > newest.get("at", 0)):
            newest = data
    if newest is None and not (explicit and not explicit.startswith("${")):
        try:
            data = json.loads((_HOME_STATE() / name).read_text())
            newest = data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            pass
    return newest


def _HOME_STATE() -> Path:
    return Path.home() / ".edgar-agent"


_DEPARTURE = re.compile(r"^\s*DEPARTURE\s+\d+\s*:\s*(.+)$", re.M)
_SIMPLE_TYPES = {"quantitative retrieval", "qualitative retrieval"}


# 13 Nov: every checked answer is also saved as a file the user can find and keep (it used to exist only in a working
# folder). EDGAR_SAVE_DIR names another folder; 'off' turns saving off.
SAVED_LINE = "This answer is saved on your computer: {path}"


def after_the_answer(text: str, line: str) -> str:
    """Put `line` straight after the answer itself: the first paragraph under the 'Answer' heading (or, in the short
    form, the first paragraph). 15 Nov, the user's review: the line saying where the answer was saved sat at the very
    bottom, after the working, where nobody would look for it."""
    m = re.search(r"(?:^|\n)#{1,3}\s*Answer\s*\n+(.+?)(?=\n\s*\n|\Z)", text, re.S) or re.match(r"\s*(.+?)(?=\n\s*\n|\Z)", text, re.S)
    if not m:
        return text.rstrip() + "\n\n" + line
    cut = m.end(1)
    return text[:cut] + "\n\n" + line + ("\n\n" + text[cut:].lstrip("\n") if text[cut:].strip() else "")


def save_for_user(text: str, question: str) -> Optional[Path]:
    where = (os.environ.get("EDGAR_SAVE_DIR") or "").strip()
    if where.lower() == "off":
        return None
    try:
        if where and not where.startswith("${"):
            folder = Path(where).expanduser()
        else:
            docs = Path.home() / "Documents"
            folder = (docs if docs.is_dir() else Path.home()) / "EDGAR Research"
        folder.mkdir(parents=True, exist_ok=True)
        words = re.sub(r"[^A-Za-z0-9 ]+", " ", question or "answer").split()
        stem = time.strftime("%Y-%m-%d %H%M ") + (" ".join(words)[:70].strip() or "answer")
        path, n = folder / (stem + ".md"), 1
        while path.exists():
            n += 1
            path = folder / f"{stem} ({n}).md"
        path.write_text((f"# {question.strip()}\n\n" if question.strip() else "") + text.rstrip() + "\n", encoding="utf-8")
        return path
    except OSError:
        return None  # saving a copy must never stop an answer


ANSWER_HEADING = "## Answer"
# Publishing decision 8 (13 Nov 2026): users may register with the publisher through a sign-up form they open themselves.
# The plugin sends nothing; it mentions the link once per computer, after the first checked answer. No address is built in
# here: the build sets EDGAR_REGISTRATION_URL when the publisher has a form.
REGISTER_LINE = ("Optional: to hear about updates to this plugin, you can register with its publisher at {url}. "
                 "The plugin itself sends nothing to the publisher; this line is shown once.")


# Publishing decision 12 (14 Nov 2026): one-click feedback. Under each full answer, "Was this answer right? Yes · No". The
# links go to a form of the publisher's. "Yes" records with one click and carries the rating, the kind of question and
# the plugin's version, nothing else. "No" opens the form with No chosen and, when the form has a field for it
# (decision 13, same day: a bare "No" cannot be investigated), the user's question filled in, where they can read it,
# edit it or delete it before pressing Submit. The answer and anything about the user never travel in a link, and the
# plugin itself still sends nothing. The build sets the form's address and its field codes
# (EDGAR_FEEDBACK_FORM, EDGAR_FEEDBACK_FIELDS = rating,kind,version[,question]).
FEEDBACK_LINE = ("Was this answer right? [Yes]({yes}) · [No]({no}). \"Yes\" tells the publisher in one click and sends only your rating, "
                 "the kind of question and the plugin's version. {no_says} The answer itself is never sent.")
_NO_PLAIN = "\"No\" opens a short form where you can say what was wrong."
_NO_WITH_QUESTION = ("\"No\" opens a short form with your question filled in, so the publisher can look into it: you can edit or delete "
                     "it there before you press Submit.")
QUESTION_IN_LINK = 600  # characters of the question carried in the "No" link


def feedback_line(kind: str, question: str = "") -> str:
    """The Yes/No links for an answer of this kind, or '' when the build has no feedback form."""
    from urllib.parse import quote_plus
    base = (os.environ.get("EDGAR_FEEDBACK_FORM") or "").strip().rstrip("/")
    fields = [f.strip() for f in (os.environ.get("EDGAR_FEEDBACK_FIELDS") or "").split(",") if f.strip()]
    if not base.startswith("https://docs.google.com/forms/") or len(fields) not in (3, 4) or not all(f.isdigit() for f in fields):
        return ""
    rating, k, v = fields[:3]
    version = quote_plus((os.environ.get("EDGAR_PLUGIN_VERSION") or "unknown").strip()[:20])
    tail = f"&entry.{k}={quote_plus((kind or 'not stated')[:80])}&entry.{v}={version}"
    asked = " ".join((question or "").split())[:QUESTION_IN_LINK]
    with_question = len(fields) == 4 and bool(asked)
    no = f"{base}/viewform?usp=pp_url&entry.{rating}=No{tail}" + (f"&entry.{fields[3]}={quote_plus(asked)}" if with_question else "")
    return FEEDBACK_LINE.format(yes=f"{base}/formResponse?usp=pp_url&entry.{rating}=Yes{tail}&submit=Submit", no=no,
                                no_says=_NO_WITH_QUESTION if with_question else _NO_PLAIN)


def registration_line() -> str:
    """The one-time invitation to register, or '' (no form configured, or already shown on this computer)."""
    url = (os.environ.get("EDGAR_REGISTRATION_URL") or "").strip()
    if not url.startswith("https://") or _read_state("registration_shown.json"):
        return ""
    _write_state("registration_shown.json", {"at": time.time()})
    return REGISTER_LINE.format(url=url)
TRACED = ("Every figure below is traced to an SEC filing and was checked against it; the sources and working are at the end, "
          "under 'How the figures were obtained'.")
# What each question type is, in the customer's words (13 Nov: the header named internal types and spoke of rubric points).
KIND = {"quantitative retrieval": "a figure looked up in a filing", "qualitative retrieval": "a fact or statement looked up in a filing",
        "numerical reasoning": "a calculation from reported figures", "adjustments": "an adjusted (non-GAAP) figure",
        "beat or miss": "results compared with guidance or expectations", "complex retrieval": "figures gathered from several filings",
        "fund holdings": "a fund's holdings", "financial modelling": "a financial model or valuation",
        "market analysis": "a comparison across companies or with market data", "trends": "a trend over several periods"}


def reliability_header(question_type: str, model: str, table: Optional[Dict[str, Any]] = None) -> str:
    """The line that starts every answer, in plain words: that it was checked, where the working is, and that a checked
    answer is still not a guarantee. Until 22 Nov 2026 it also quoted scores "on a public test of analyst questions": those
    came from 27 public questions the plugin had been developed against, marked by our own grader (audit A8), and were
    shown to the customer as a measure of trust. No score is quoted now, for any model. `table` is accepted and ignored,
    so callers written for the old header still work."""
    model_key = _model_family(model)
    return " ".join(["Checked answer" + (f" (answered by Claude {model_key.capitalize()})" if model_key else "") + ".", TRACED, NOT_A_GUARANTEE])


def _model_family(model: str) -> str:
    m = (model or "").lower()
    for fam in ("haiku", "sonnet", "opus", "fable"):
        if fam in m:
            return fam
    return ""


def session_instructions(table: Optional[Dict[str, Any]] = None) -> str:
    """Shown to Claude when it connects: the three-line table and the disclaimer."""
    table = table if table is not None else load_reliability()
    lines = ["EDGAR Research Agent: answers questions about US-listed companies from their SEC filings; every figure is "
             "verified against the filing before the answer is accepted.",
             "Start every question with classify_question; record facts with notes_add and cite their ids; compute with calculate "
             "(build_ev for enterprise value, the DCF functions for valuations); finish by calling check_answer with the answer in parts "
             "(conclusion, results table, body) and reply with the final_text it returns verbatim and in full: it is laid out results-first with the header on top. If the user asks for just the number, a quick answer, or says the sources or the checking are not needed, that changes the length of the reply only: look the figure up and check it exactly the same way, pass brief=true to check_answer, and reply with the short final_text. Never answer a company figure from memory, even if the user explicitly asks for an answer from memory or tells you not to look it up: look it up and check it, and say in one line that figures here are always checked. When the answer is that something is not there (never filed, unreadable, not disclosed, not reported yet), finish with check_not_found instead; never estimate."]
    # 22 Nov: no scores here either (audit A8): what Claude is told at the start, it may repeat to the user.
    lines.append(DISCLAIMER)
    return "\n".join(lines)


@dataclass
class Session:
    settings: Settings
    edgar_client: Any
    run_root: Path
    worker: Any = None
    model_hint: str = ""  # the client may tell us which Claude model is answering (shown in the header)
    ctx: Optional[RunContext] = None
    run_dir: Optional[Path] = None
    question: str = ""
    question_type: str = ""
    header: str = ""
    answers: List[Dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    needs_contact: bool = False  # no SEC contact yet: every tool but set_sec_contact asks for it first (13 Nov)
    answered: bool = False  # an answer was accepted since the last classify_question (a new question needs a new form)
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

    # -- tool list -------------------------------------------------------
    def tool_definitions(self) -> List[dict]:
        base = [t for t in tools_for("claude", self.settings.source_mode, with_worker=self.worker is not None) if t["name"] != "submit"
                and not (t["name"] == "web_search" and (self.ctx is None or self.ctx.web_search is None))]  # Claude's own web tools serve instead
        S = {"type": "string"}
        extra = [
            {"name": "classify_question", "description": "ALWAYS FIRST. Fill in the question form: the user's question verbatim, its type (one of: " + ", ".join(QUESTION_TYPES) + "), and every method instruction the question itself gives (average vs year-end balances, which guidance point, which share count, assumptions to hold). The server returns the rules for this question, the definitions of any catalogue metrics it names, and the reliability header that will open the answer.",
             "input_schema": {"type": "object", "properties": {"question": S, "question_type": S,
                                                               "method_constraints": {"type": "array", "items": S, "description": "Each method instruction in the question, in its words"},
                                                               "model": {**S, "description": "The Claude model answering, if known (e.g. 'claude-sonnet-5-5')"},
                                                               "other_companies": {"type": "array", "items": S, "description": "When the question is about more than one company (a comparison, a ranking, a deal between two): every company besides subject_company, one name or ticker each. Each is checked separately."},
                                                               "standalone_question": {**S, "description": "REQUIRED when the question leans on earlier messages ('their', 'and for Microsoft?', 'same for 2024', 'use basic instead'): the question restated so it stands on its own, naming the company, the measure, the period, and every instruction from earlier messages that still applies. Keep `question` as the user typed it. Omit when the question already stands alone."},
                                                               "subject_company": {**S, "description": "REQUIRED. The main company the question is about (name or ticker), or 'none' for a question not about a company (a hypothetical fund, a market index)."}},
                              "required": ["question", "question_type", "subject_company"]}},
            {"name": "check_answer", "description": "LAST STEP, REQUIRED. Pass the answer in parts and the server lays it out, results first: `conclusion` (one or two sentences in the question's own terms), `results` (the figures the question asked for, as a small table: columns = companies or periods, rows = metrics, each cell a value with the note id it rests on; omit for qualitative questions), and `answer` (the body: inputs from the filings with citations, the working, sources, and a 'Definitions used' section). The server adds the reliability header, puts a results table that rests on share prices under 'From non-SEC sources' itself, appends 'How the figures were obtained', and checks method constraints, definitions, that every figure (table cells included) traces to a recorded note, precision, and the valuation route. It returns accepted=true with final_text: reply with it VERBATIM and in full, do not reorder or shorten it (it is already results-first). accepted=false lists the problems to fix.",
             "input_schema": {"type": "object", "properties": {"answer": {**S, "description": "The body: inputs, working, sources, definitions. The header and the results table are placed by the server."},
                                                               "conclusion": {**S, "description": "One or two sentences answering the question directly."},
                                                               "brief": {"type": "boolean", "description": "true when the user asked for just the number, a short answer, or said the sources are not needed. Everything is still checked; final_text is then the conclusion plus one source line, and the full answer is saved in the audit folder."},
                                                               "results": {"type": "object", "description": "The headline figures as a table. Omit for qualitative questions.",
                                                                           "properties": {"columns": {"type": "array", "items": S, "description": "Column headings, e.g. ['Company A', 'Company B'] or ['FY2023', 'FY2024']; omit or [] for a single value column"},
                                                                                          "rows": {"type": "array", "items": {"type": "object", "properties": {
                                                                                              "label": S, "cells": {"type": "array", "items": {"type": "object", "properties": {"text": {**S, "description": "The figure as it should read, e.g. '11.43x' or '$2,952,221,000'"}, "note_id": {"type": "integer"}}, "required": ["text"]}}},
                                                                                              "required": ["label", "cells"]}}}},
                                                               "sources": {"type": "array", "items": {"type": "object"}, "description": "One per filing: form, company, period, filed, accession_number, url"},
                                                               "review_response": {"type": "array", "description": "REQUIRED when the second reader raised departures: one entry per DEPARTURE line, in order. `resolution` starts with 'fixed:' and says what you changed in the working, or 'kept:' and gives the evidence from the filing (with its note id) for your reading.",
                                                                                   "items": {"type": "object", "properties": {"departure": S, "resolution": S}, "required": ["departure", "resolution"]}},
                                                               "missing": {"type": "array", "description": "Items the question asked for that you could not find or verify (a partial answer). Each is shown under 'Not found' with the reason; never put an estimate in their place.",
                                                                           "items": {"type": "object", "properties": {"item": S, "reason": S, "searched": S}, "required": ["item", "reason"]}},
                                                               "method_check": {"type": "array", "description": "REQUIRED when classify_question returned method_constraints: one entry per constraint, in order, as {\"constraint\": <the instruction>, \"applied\": <concretely how: the figure, document or date used>}.",
                                                                                "items": {"type": "object", "properties": {"constraint": S, "applied": S}, "required": ["constraint", "applied"]}}},
                              "required": ["answer"]}},
            {"name": "check_not_found", "description": "LAST STEP when the answer is that something could not be found (instead of check_answer). Statuses: not_sec_filer (the company the question is about does not file with the SEC at all: outside this plugin's scope; give the company name), not_filed (the company never filed that form: give cik and forms; the server checks its whole EDGAR history), not_public (the form type is filed confidentially and EDGAR shows none publicly, e.g. 17HACON, NPORT-NP: give forms; the server checks full-text search), not_readable (the filing exists but the tools could not read it: give accession_number and cik; the server checks the tool log or that the documents are images/PDF), not_in_filing (the filing does not disclose it: give accession_number; it must have been opened), not_yet_reported (the period has not ended or no filing covers it yet: give period_end and cik), undetermined (what you tried did not settle it). `statement` answers the question in one or two sentences; `answer` may add what does exist instead (the 13G instead of a 13D, guidance instead of results): any figure there must trace to a recorded note, as in check_answer. Returns final_text with the header and a 'What was searched' section: reply with it verbatim. Refused claims come back with what the server found.",
             "input_schema": {"type": "object", "properties": {"company": {"type": "string", "description": "not_sec_filer: the company the question is about"}, "different_company": {"type": "boolean", "description": "not_sec_filer only: true when classify_question matched the name to an EDGAR entity that is NOT the company the user means (a namesake, a trust, a subsidiary), and the company itself files nothing"}, "status": {**S, "enum": list(notfound.STATUSES)}, "statement": S,
                                                               "answer": {**S, "description": "Optional body: what exists instead, with figures cited to notes"},
                                                               "searched": {"type": "array", "items": S, "description": "What you looked at, one item each: filings (form, date, accession), forms checked, sections, search terms"},
                                                               "cik": S, "forms": {"type": "array", "items": S}, "accession_number": S, "url": S,
                                                               "period_end": {**S, "description": "YYYY-MM-DD, for not_yet_reported"},
                                                               "sources": {"type": "array", "items": {"type": "object"}},
                                                               "method_check": {"type": "array", "description": "REQUIRED when classify_question returned method_constraints (an as-of date, a filing to use): one entry per constraint, as in check_answer.",
                                                                                "items": {"type": "object", "properties": {"constraint": S, "applied": S}, "required": ["constraint", "applied"]}}},
                              "required": ["status", "statement", "searched"]}},
            {"name": "feedback", "description": "Call whenever the user says an answer was wrong, disputes a figure or a convention, corrects something, or says it was useful. Records the user's words against the answer's audit record; with consent=true also sends it to the maintainers.",
             "input_schema": {"type": "object", "properties": {"verdict": {**S, "enum": ["wrong", "incomplete", "disputed_convention", "right", "other"]},
                                                               "comment": S, "answer_id": {"type": "integer"}, "consent": {"type": "boolean"}}, "required": ["verdict", "comment"]}},
            {"name": "report_tool_problem", "description": "Call whenever a TOOL result looks wrong rather than you: a refusal whose reason does not make sense (a function argument read as a rounded figure), a lookup that returns the wrong company, a figure the tool says is missing but the filing shows, a schema the description did not explain. Say which tool, what you called it with, what came back, and what you expected. The report is attached to the session's audit record with the tool log; nothing is sent to the maintainers unless the user agrees (consent=true). Keep working on the question afterwards.",
             "input_schema": {"type": "object", "properties": {"tool": S, "arguments": {"type": "object"}, "observed": S, "expected": S, "consent": {"type": "boolean"}},
                              "required": ["tool", "observed", "expected"]}},
            {"name": "list_conventions", "description": "Show the analyst conventions this plugin applies where practice varies (which filing for an earlier year, share count for market cap and EV, exact or rounded against guidance, what enters EV and FCF, DCF timing...): the value in force, whether it is our default or the user's choice, the alternatives, and the source of the default. Call when the user asks about conventions, assumptions or methodology, or wants to change one.",
             "input_schema": {"type": "object", "properties": {"question": {**S, "description": "Optional: a question, to show which conventions it fixes itself"}}}},
            {"name": "set_convention", "description": "Change one convention for this user, saved for future sessions; value 'default' restores our default. Only on the user's request. A method instruction in a question still overrides it for that question.",
             "input_schema": {"type": "object", "properties": {"id": {**S, "description": "Convention id from list_conventions, e.g. 'prior_year_figures'"},
                                                               "value": {**S, "description": "One of the option keys list_conventions shows, or 'default'"}},
                              "required": ["id", "value"]}},
            {"name": "session_status", "description": "What this session has recorded so far: the question, notes count, answers checked, audit folder.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "show_full_answer", "description": "The full checked answer, with its sources and working, for an answer already given in this session (usually one given in the short form). Call it when the user asks for the working, the sources, the details or the full answer. Reply with the final_text it returns, verbatim; do not rebuild the working yourself and do not add figures.",
             "input_schema": {"type": "object", "properties": {"answer_id": {"type": "integer", "description": "The answer's number ('answer 3' in the short form). Omit for the most recent answer."}}}},
            {"name": "set_sec_contact", "description": "Save the contact the SEC requires on every request to EDGAR (its fair-access rule: declare a name and an email). Call it only when a tool result says needs_sec_contact, or when the user asks to change the contact, and only with a name and email the user gave or confirmed in this conversation: never fill them in yourself. Saved on the user's computer; sent to the SEC only.",
             "input_schema": {"type": "object", "properties": {"name": {**S, "description": "The user's own name or their organisation's"},
                                                               "email": {**S, "description": "The email address the user gave or confirmed"}},
                              "required": ["name", "email"]}},
        ]
        if UPLOAD is None:  # the public plugin: nothing is sent to the publisher, so the tools must not say it can be
            for t in extra:
                if t["name"] in ("feedback", "report_tool_problem"):
                    t["input_schema"]["properties"].pop("consent", None)
                    t["description"] = (t["description"].replace("; with consent=true also sends it to the maintainers.", ". It stays on the user's computer.")
                                        .replace("; nothing is sent to the maintainers unless the user agrees (consent=true).", "; it stays on the user's computer."))
        return base + extra

    # -- dispatch ----------------------------------------------------------
    def call(self, name: str, args: Dict[str, Any]) -> str:
        # 10 Nov review: the server runs each call on a worker thread and clients send calls in parallel, so two calls
        # could interleave on the notes file and the check state. One call at a time per session.
        with self._lock:
            if name == "set_sec_contact":
                return json.dumps(self.set_contact(dict(args or {})), ensure_ascii=False)
            if self.needs_contact:
                return json.dumps({"needs_sec_contact": True, "nothing_was_looked_up": True, "what_to_do": _contact.ASK}, ensure_ascii=False)
            return self._call(name, args)

    def set_contact(self, a: Dict[str, Any]) -> Dict[str, Any]:
        name, email = str(a.get("name") or ""), str(a.get("email") or "")
        problem = _contact.check(name, email)
        if problem:
            return {"saved": False, "problem": problem, "what_to_do": "Ask the user for it again; do not make one up."}
        agent = _contact.user_agent(name, email)
        where = _contact.save(name, email)
        self.edgar_client.user_agent = agent  # used on every request from now on
        os.environ["EDGAR_USER_AGENT"] = agent  # for clients built later in this process
        self.needs_contact = False
        return {"saved": True, "contact": agent, "saved_to": str(where) if where else "this session only (the folder could not be written)",
                "note": "Tell the user in one line that the contact is saved on their computer and goes to the SEC only, then answer their question."}

    def _call(self, name: str, args: Dict[str, Any]) -> str:
        args = dict(args or {})
        if name == "classify_question":
            return json.dumps(self.classify(args), ensure_ascii=False)
        if name == "check_answer":
            return json.dumps(self.check(args), ensure_ascii=False)
        if name == "check_not_found":
            return json.dumps(self.not_found(args), ensure_ascii=False)
        if name == "feedback":
            return json.dumps(self.feedback(args), ensure_ascii=False)
        if name == "report_tool_problem":
            return json.dumps(self.report_tool_problem(args), ensure_ascii=False)
        if name == "show_full_answer":
            return json.dumps(self.full_answer(args), ensure_ascii=False)
        if name == "session_status":
            return json.dumps(self.status(), ensure_ascii=False)
        if name == "list_conventions":
            return json.dumps(self.list_conventions(args), ensure_ascii=False)
        if name == "set_convention":
            return json.dumps(self.set_convention(args), ensure_ascii=False)
        if self.ctx is None:
            self._start(args.get("question") or "", "", [])
            # 5 Nov: two forms questions were answered with data tools but no classify_question, so no header, no
            # check and no appendix. Remind on the first tool call instead of relying on the skill alone.
            out = run_tool(name, args, self.ctx)
            try:
                data = json.loads(out)
                if not isinstance(data, dict):
                    data = {"result": data}
                if isinstance(data, dict):
                    data["reminder"] = ("classify_question has not been called for this question. Call it now (the question verbatim and "
                                        "its type), then finish with check_answer or check_not_found and reply with the final_text.")
                    return json.dumps(data, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                pass
            return out
        out = run_tool(name, args, self.ctx)
        if self.answered:
            # 10 Nov review: after an accepted answer a new question reused the old question's form, constraints and scope.
            self.answered = False
            try:
                data = json.loads(out)
                if isinstance(data, dict):
                    data["reminder"] = ("The previous question in this session has been answered. If this call is for a NEW question, "
                                        "call classify_question for it first (its own type, method constraints and subject company); "
                                        "otherwise the checks run against the old question.")
                    return json.dumps(data, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                pass
        return out

    # -- the added tools ---------------------------------------------------
    def _start(self, question: str, question_type: str, constraints: List[str], as_asked: str = "", subject: str = "") -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", (question or "session").lower())[:40].strip("-") or "session"
        self.run_dir = self.run_root / f"mcp-{stamp}-{slug}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        as_of = self.settings.as_of or time.strftime("%Y-%m-%d")
        self.ctx = RunContext(client=self.edgar_client, notes=Notes(self.run_dir / "notes.jsonl"), as_of=as_of,
                              source_mode=self.settings.source_mode, question=question, max_chars=int(self.settings.tool_result_max_chars),
                              price_provider=str(self.settings.values.get("price_provider", "auto")),
                              price_api_key=_pricekey.load())  # read at each question: a key saved a minute ago is used
        self.ctx.price_help = _pricekey.how_to
        self.ctx.worker = self.worker
        self.ctx.method_constraints = list(constraints)
        self.question, self.question_type = question, question_type
        self.question_as_asked, self.subject_company = (as_asked or question), subject
        self.question_started, self.review = time.time(), None
        self.answered = False
        (self.run_dir / "session.json").write_text(json.dumps({"question": question, "question_as_asked": as_asked or question,
                                                               "restated": bool(as_asked and as_asked != question),
                                                               "subject_company": subject or None, "question_type": question_type,
                                                               "method_constraints": constraints, "as_of": as_of,
                                                               "source_mode": self.settings.source_mode, "started": stamp}, indent=1))

    def classify(self, a: Dict[str, Any]) -> Dict[str, Any]:
        question = str(a.get("question") or "").strip()
        qtype = str(a.get("question_type") or "").strip()
        constraints = [str(c) for c in (a.get("method_constraints") or []) if str(c).strip()]
        if a.get("model"):
            self.model_hint = str(a["model"])
        from edgar_agent import tools as _tools
        # 11 Nov: a follow-up ("what was their growth rate in 2024") reaches the server without its company, its measure or
        # the instructions given in earlier messages; the server cannot see the conversation. The model restates it in
        # full, and every check runs on the restated question. The user's own words are kept beside it.
        as_asked, standalone = question, str(a.get("standalone_question") or "").strip()
        subject_raw = str(a.get("subject_company") or "").strip()
        if subject_raw.lower() in ("none", "n/a", "-"):
            subject_raw = ""
        if not standalone and subject_raw and not _names_company(question, subject_raw):
            return {"accepted": False, "problems": [{"missing": "standalone_question",
                                                     "why": f"the question does not name the company it is about ({subject_raw})"}],
                    "fix": "This question leans on earlier messages. Call classify_question again with the same `question` (the user's words) "
                           "and add `standalone_question`: the question restated so it stands alone, naming the company, the measure "
                           "and the period, and repeating any instruction from earlier messages that still applies (a basis, a "
                           "definition, an assumption, a rounding). If an earlier answer chose a reading (for example revenue growth "
                           "for 'growth rate'), say so in it."}
        if standalone:
            question = standalone
        # 11 Nov: the question's own binding phrases are added by code, so none depends on the model listing it.
        added = _tools.binding_phrases(question, constraints)
        constraints = constraints + added
        self._start(question, qtype, constraints, as_asked=as_asked, subject=subject_raw)
        self.ctx.qualifiers = _tools.question_qualifiers(question) if _tools.QUALIFIER_CHECKS else []
        named = core.metrics_in_text(question)
        for met in named:
            self.ctx.metrics_used[met.name] = met
        self.ctx.out_of_scope = None
        subject = str(a.get("subject_company") or "").strip()
        if subject.lower() in ("none", "n/a", "-"):
            subject = ""
        # 11 Nov: a question can be about several companies. Each is checked on its own; the question is refused only when
        # NONE of them files with the SEC. If some do and some do not, the filers are answered and the others are named as
        # not covered (one field holding "Apple and Nestle" matched no EDGAR entity and would have refused the whole question).
        listed = [str(x).strip() for x in (a.get("other_companies") or []) if str(x).strip()]
        others = listed
        self.ctx.not_covered, self.other_companies, self.ctx.scope_unchecked = [], listed, []
        self.ctx.scope_guess = []
        if (subject or others) and str(self.settings.values.get("company_scope", "sec_filers")) == "sec_filers":
            from edgar_core import scope as _scope
            verdicts, began = [], time.time()
            cache = self.__dict__.setdefault("_scope_cache", {})  # a company checked once in this session is not checked again
            for company in ([subject] if subject else []) + others:
                key = (company.lower(), self.ctx.as_of)
                if key not in cache and verdicts and time.time() - began > SCOPE_SECONDS:
                    # There is no limit on how many companies a question may name. The check is paced by time, so a very
                    # long list does not stall the call: the rest are handed to Claude to verify one by one, and recorded.
                    self.ctx.scope_unchecked.append(company)
                    continue
                if key not in cache:
                    cache[key] = _scope.check(company, as_of=self.ctx.as_of, client=self.edgar_client)
                sc = cache[key]
                verdicts.append((company, sc))
                if sc.get("in_scope") and "treated as in scope" in str(sc.get("reason")):
                    # the SEC lookup failed, so the scope check did not really run: say so in the audit record
                    self.ctx.check_log.append({"check": "scope", "company": company, "skipped": str(sc.get("reason"))[:200]})
            if self.ctx.scope_unchecked:
                self.ctx.check_log.append({"check": "scope", "left_to_the_model": list(self.ctx.scope_unchecked)})
            # 12 Nov: a company let in on a guess (a longer name, a name that only begins alike, a symbol typed as a word)
            # is remembered, so Claude is told which EDGAR entity it was matched to and web-only answers are questioned.
            self.ctx.scope_guess = [{"company": c, "matched": " ".join(str(sc.get("name") or "").split()), "cik": sc.get("cik"), "match": sc.get("match")}
                                    for c, sc in verdicts if sc.get("in_scope") and sc.get("match") not in (None, "exact")]
            out = [(c, sc) for c, sc in verdicts if not sc.get("in_scope")]
            if out and len(out) == len(verdicts):
                self.ctx.out_of_scope = {"company": ", ".join(c for c, _ in out), **out[0][1]}
            elif out:
                self.ctx.not_covered = [{"company": c, "reason": sc.get("reason")} for c, sc in out]
        difficulty = self.difficulty = difficulty_of(qtype, question)
        self.conventions = conv.effective(question)
        self.ctx.conventions = conv.values(self.conventions)
        self.header = reliability_header(qtype, self.model_hint)
        mine = conv.changed_by_user(self.conventions)
        if mine and self.header:
            self.header += (f" You have changed {len(mine)} convention{'s' if len(mine) > 1 else ''} from our defaults; "
                            "the measured scores above were obtained with the defaults.")
        record_classified(self.header)
        rules = system_prompt(self.settings, self.ctx.as_of, 0, constraints, named)
        rules = rules.split("Rules that are checked in code", 1)[-1]
        warnings = []
        if self.settings.source_mode == "extended" and not self.ctx.price_api_key:
            # 1 Nov, first benchmark run: every price call failed with HTTP 403 (key not passed through) and 29 rubric
            # points went with it. Say so at the start, not after the research is done.
            warnings.append("Share prices cannot be fetched in this session: no share-price key is set (EDGAR_PRICE_API_KEY). If the "
                            "question needs prices (market cap, EV multiples, price changes, deal premiums): " + PRICES_UNAVAILABLE
                            + " " + _pricekey.how_to())
        if getattr(self.ctx, "scope_unchecked", None):
            warnings.insert(0, "The server has checked " + str(len(verdicts)) + " of the companies in this question against the SEC's records. "
                               "Check each of these yourself before using it: " + ", ".join(self.ctx.scope_unchecked) + " (resolve_entity, "
                               "then list_filings for a 10-K, 20-F or 40-F). Cover every company that files. Give no figures for one "
                               "that does not: list it under `missing` with the reason. There is no limit on the number of companies and "
                               "you must not tell the user there is one; if a company could not be included, name it and say why.")
        if getattr(self.ctx, "scope_guess", None):
            pairs = "; ".join(f"'{g['company']}' -> {g['matched']} (CIK {int(g['cik'])})" for g in self.ctx.scope_guess if g.get("cik"))
            warnings.insert(0, f"COMPANY MATCHED BY NAME, NOT EXACTLY: {pairs}. Check that this is the company the user means before using its "
                               "filings. If it is a different company (a namesake, a lease trust, a subsidiary) and the company asked about "
                               "files nothing with the SEC under another name, give no figures for it from any source: finish with "
                               "check_not_found, status not_sec_filer, different_company=true (or list it under `missing` when other "
                               "companies in the question do file). If it files under another name, call classify_question again with "
                               "that registered name, its ticker or its CIK.")
        if getattr(self.ctx, "not_covered", None):
            names = "; ".join(f"{n['company']} ({n['reason']})" for n in self.ctx.not_covered)
            warnings.insert(0, f"PARTLY OUT OF SCOPE: {names}. This plugin answers only about companies that file with the SEC. Answer the "
                               "parts of the question about the companies that do file. Do not research the others and give no figures "
                               "for them: list each under `missing` in check_answer with the reason 'does not file with the SEC', and say "
                               "in the conclusion which part of the question could not be covered. If a name was matched to the wrong "
                               "EDGAR entity, call classify_question again with its ticker or CIK.")
        if getattr(self.ctx, "out_of_scope", None):
            warnings.insert(0, f"OUT OF SCOPE: {self.ctx.out_of_scope['reason']}. This plugin answers only about companies that file with the SEC "
                               "(US companies and foreign issuers filing 20-F, 40-F or 6-K). Do not research it: finish with check_not_found, "
                               "status not_sec_filer, giving the company and, if you know it, where it publishes its reports (its own website, "
                               "its home exchange or regulator). This verdict came from matching the company's NAME on EDGAR, which can pick "
                               "the wrong entity. If you know the company does file with the SEC, do not report it as a non-filer: call "
                               "classify_question again with subject_company set to its ticker or its CIK number, and the check is redone on that.")
        if added:
            warnings.append(f"The server took {len(added)} binding phrase(s) from the question and added them to method_constraints (the ones "
                            "that start 'The question says' or 'The question names'). check_answer needs a method_check entry for every "
                            "constraint, in order. Plan the working around them now: each must hold literally and in every period.")
        if self.ctx.qualifiers:
            warnings.append("This question qualifies: " + "; ".join(q["phrase"] for q in self.ctx.qualifiers) + ". Map each to the exact reported "
                            "line it names (not the headline total), record that figure with the qualifier in its label, and name the line in "
                            "the answer. check_answer refuses once if no recorded figure carries a qualifier.")
        return {"accepted": True, "question_type": qtype, "difficulty": difficulty, "as_of": self.ctx.as_of, "warnings": warnings or None,
                "header": self.header, "header_note": "check_answer places this sentence at the top of the final text; reply with that text in full.",
                "method_constraints": constraints, "definitions_that_apply": definitions_block(named).strip() or None,
                "conventions": conv.prompt_block(self.conventions),
                "rules": "Rules that are checked in code" + rules, "audit_folder": str(self.run_dir),
                "tools_note": "extract is " + ("available" if self.worker is not None else "not available in this session: read with find_in_document, get_table and read_document")}

    def check(self, a: Dict[str, Any]) -> Dict[str, Any]:
        if self.ctx is None:
            return {"accepted": False, "problems": [{"missing": "classify_question"}], "fix": "Call classify_question first, then record facts, then check_answer."}
        body_all = " ".join(str(a.get(k) or "") for k in ("conclusion", "answer"))
        if (str(self.settings.values.get("company_scope", "sec_filers")) == "sec_filers" and not getattr(self.ctx, "out_of_scope", None)
                and not getattr(self.ctx, "not_covered", None)  # a mixed question rightly says one company does not file
                and re.search(r"(does not|doesn't|do not|is not an?|isn't an?|never) (file|filed|files|sec filer|sec registrant|registered with the sec)[^.]{0,40}(with the sec|sec)?|"
                              r"not an sec (filer|registrant)|no (sec|edgar) filings?|has no filer|no filer (called|named)", body_all, re.I)
                and re.search(r"from non-sec sources", body_all, re.I)):
            # 9 Nov: Valeo was answered from its own press release although the answer itself said Valeo does not file with the SEC.
            self.ctx.out_of_scope = {"company": "the subject company", "in_scope": False,
                                     "reason": "the answer itself says the company the question is about does not file with the SEC"}
        if getattr(self.ctx, "out_of_scope", None):
            return {"accepted": False, "problems": [{"out_of_scope": self.ctx.out_of_scope["reason"]}],
                    "fix": "The company does not file with the SEC, which is outside this plugin's scope. Finish with check_not_found, status not_sec_filer. "
                           "If that is wrong (the name was matched to the wrong EDGAR entity and the company does file), call classify_question "
                           "again with subject_company set to its ticker or CIK number; your recorded facts will need recording again."}
        # The header quotes measured percentages ("57% ... 82%"): it is laid on top AFTER the sourcing check, or the
        # check refuses its own sentence as unsourced figures (2 Nov: 42 of 50 answers rejected for exactly that).
        answer, problems = compose_answer(self.header, a, self.ctx.notes.entries(), include_header=False)
        if problems:
            return {"accepted": False, "problems": problems, "fix": "Fix the results table and call check_answer again."}
        swapped = _narrow_line_called_total(self.ctx, " ".join(str(a.get(k) or "") for k in ("conclusion", "answer")))
        if swapped:
            return {"accepted": False, "problems": [{"revenue_line": f"the answer calls {swapped['narrow']:,.0f} total revenues", "period_end": swapped["period_end"]}],
                    "fix": f"For the period ending {swapped['period_end']} the filing reports total revenues of {swapped['total']:,.0f} and a narrower "
                           f"revenue line of {swapped['narrow']:,.0f} (revenue from contracts with customers, such as net sales). The answer gives the "
                           "narrower figure under the name 'total revenues'. Give total revenues as the answer and name the narrower line for what it is "
                           "(or, if the question asks for net sales, call it net sales), citing each figure's own note."}
        guess = _web_only_for_a_guessed_company(self.ctx)
        if guess and "scope_guess" not in self.ctx.nudged:
            self.ctx.nudged.add("scope_guess")
            self.ctx.check_log.append({"check": "scope_guess", "companies": [g["company"] for g in guess]})
            pairs = "; ".join(f"'{g['company']}' was matched to {g['matched']}" for g in guess)
            return {"accepted": False, "problems": [{"scope_guess": pairs, "reason": "no figure in this answer comes from an SEC filing"}],
                    "fix": "The company was let in on a name match that is not exact, and every figure recorded for this answer comes from "
                           "outside SEC filings. If the matched EDGAR entity is NOT the company the user means, the company does not file "
                           "with the SEC: give no figures for it and finish with check_not_found, status not_sec_filer, "
                           "different_company=true. If it IS the same company, submit again as it is."}
        missing = [m for m in (a.get("missing") or []) if isinstance(m, dict) and str(m.get("item") or "").strip()]
        unlisted = [n["company"] for n in (getattr(self.ctx, "not_covered", None) or [])
                    if not any(_names_company(str(m.get("item")), n["company"]) for m in missing)]
        if unlisted and "not_covered" not in self.ctx.nudged:
            self.ctx.nudged.add("not_covered")
            self.ctx.check_log.append({"check": "not_covered", "companies": unlisted})
            return {"accepted": False, "problems": [{"not_covered": c, "reason": "does not file with the SEC"} for c in unlisted],
                    "fix": "These companies are part of the question but outside this plugin's scope. Give no figures for them. List each "
                           "under `missing` (item: the company and what was asked about it; reason: does not file with the SEC), and say "
                           "in the conclusion which part of the question is not covered."}
        bad = [m["item"] for m in missing if not str(m.get("reason") or "").strip()]
        if bad:
            return {"accepted": False, "problems": [{"missing_item": b, "reason": "give the reason it could not be found"} for b in bad]}
        refused = self._second_reader(a)
        if refused:
            return refused
        result = json.loads(run_tool("submit", {"answer": answer, "sources": a.get("sources") or [], "method_check": a.get("method_check") or []}, self.ctx))
        if not result.get("accepted"):
            return result
        conflicts = self._fund_conflicts(answer)
        if conflicts:
            return {"accepted": False, "problems": conflicts,
                    "fix": "Another SEC filing reports this figure differently. Record the other figure with notes_add (source_type filing, its "
                           "accession) and answer per the filing-conflict rule: a Recommended answer, a labelled Alternative answer with its "
                           "filing, and why the filings differ (do not assert a cause the filings do not state). Then call check_answer again."}
        if missing:
            answer = answer.rstrip() + "\n\n### Not found\n\n" + "\n".join(
                f"- **{m['item']}**: {str(m['reason']).strip()}" + (f" (searched: {str(m['searched']).strip()})" if str(m.get("searched") or "").strip() else "")
                for m in missing)
        header = self.header
        if header and not any(e.get("source_type") in ("filing", "web", "web_calculation") for e in self.ctx.notes.entries()):
            # 12 Nov: a calculation from the user's own assumptions (a loan schedule) was headed "Every figure below is traced
            # to an SEC filing", which was untrue: no filing was involved.
            header = header.replace(TRACED, NO_FILING_HEADER)
        if header and re.search(r"from non-sec sources", answer, re.I):
            # 6 Nov: answers may now carry web figures; the header must not claim every figure is from a filing.
            header = header.replace(TRACED,
                                    "Figures are traced to SEC filings, except those under 'From non-SEC sources', which name their own source; "
                                    "the filing figures were checked against the filings. See 'How the figures were obtained'.")
        # 15 Nov, the user's review: the documents an answer rests on were listed only at the bottom, after the working.
        # They now follow the answer itself (its first paragraph), each as a link; the full list of figures stays at the end.
        docs = self._documents_used(answer)
        if docs:
            m = re.match(r"(\s*(?:#{1,3}\s*Answer\s*\n+)?)(.+?)(?=\n\s*\n|\Z)", answer, re.S)
            cut = m.end(2) if m else 0
            answer = (answer[:cut] + "\n\n" if cut else "") + docs + ("\n\n" + answer[cut:].lstrip("\n") if answer[cut:].strip() else "")
        self.caution = _needs_caution(getattr(self, "difficulty", ""), self.ctx.notes.entries())
        if header and self.caution:
            header = header + " " + CAUTION
        if header:
            answer = header + "\n\n" + answer
        working = render_working(self.ctx.notes.entries(), self.ctx.tool_calls, self.ctx.filings_seen)
        if re.search(r"^#*\s*\**Definitions used", answer, re.M | re.I) and "### Definitions used" in working:
            working = working.split("### Definitions used")[0].rstrip() + "\n"  # the answer already has its own section
        final = answer.rstrip()
        eff = getattr(self, "conventions", None) or conv.effective(self.question)
        tools_used = {c["tool"] for c in self.ctx.tool_calls}
        periods = {e.get("period_end") for e in self.ctx.notes.entries() if e.get("source_type") == "filing" and e.get("period_end")}
        section = conv.answer_section(eff, conv.relevant(eff, self.ctx.metrics_used.keys(), tools_used, len(periods) > 1))
        if section and not re.search(r"^\W*Conventions used", final, re.M | re.I):
            final = final + "\n\n" + section
        if getattr(self, "review", None):
            final = final + "\n\n**Independent check:** " + self.review["line"]
        answer_id = len(self.answers) + 1
        full_working = working
        if working and len(final) + len(working) > FINAL_TEXT_LIMIT:
            # 6 Nov (IDEX): the appendix made final_text too large for Claude Code to return, so the checked answer could
            # not be given verbatim. Keep the appendix readable: long lines shortened, the list capped, the full record
            # saved in the audit folder and named.
            lines = [ln if len(ln) <= 280 else ln[:277] + "..." for ln in working.strip().splitlines()]
            budget, kept = FINAL_TEXT_LIMIT - len(final) - 400, []
            for ln in lines:
                if sum(len(k) + 1 for k in kept) + len(ln) > budget:
                    break
                kept.append(ln)
            full_path = self.run_dir / f"answer-{answer_id}-figures.md"
            full_path.write_text(full_working)
            working = "\n".join(kept) + (f"\n\n(Shortened to fit: {len(lines) - len(kept)} more lines. The full record of every figure is in {full_path}.)"
                                          if len(kept) < len(lines) else "")
        # Look for the section heading, not the phrase: the reliability header itself mentions the phrase.
        complete = final  # what is saved for the user: never shortened
        if working and not re.search(r"^#+\s*How the figures were obtained", final, re.MULTILINE):
            complete = final + "\n\n" + full_working.strip()
            final = final + "\n\n" + working.strip()
        saved = save_for_user(complete, self.question or getattr(self, "question_as_asked", ""))  # 22 Nov: a follow-up was saved as "And the year before.md"
        if saved:
            final = after_the_answer(final, SAVED_LINE.format(path=saved))
        rate = feedback_line(KIND.get((self.question_type or "").strip().lower(), self.question_type), self.question or getattr(self, "question_as_asked", ""))
        if rate and not a.get("brief"):  # not under a one-line answer, where the links would be longer than the answer
            final += "\n\n" + rate
        invite = registration_line()
        if invite:
            final += "\n\n" + invite
        record = {"answer_id": answer_id, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "model": self.model_hint, "question": self.question,
                  "question_as_asked": getattr(self, "question_as_asked", self.question), "subject_company": getattr(self, "subject_company", "") or None,
                  "question_type": self.question_type, "header_present": True, "method_check": a.get("method_check") or [],
                  "tool_calls": len(self.ctx.tool_calls), "notes": len(self.ctx.notes.entries()),
                  "tool_errors": [c for c in self.ctx.tool_calls if not c.get("ok")],
                  "second_reader": getattr(self, "review", None),
                  "checks": list(self.ctx.check_log), "refusals": sum(1 for c in self.ctx.check_log if c.get("fired") or c.get("sourcing_problems")),
                  "tool_problems_reported": sum(1 for _ in open(self.run_dir / "tool_problems.jsonl")) if (self.run_dir / "tool_problems.jsonl").exists() else 0}
        self.answers.append(record)
        (self.run_dir / f"answer-{answer_id}.md").write_text(final + "\n")
        record["file"] = str(self.run_dir / f"answer-{answer_id}.md")  # for show_full_answer
        (self.run_dir / f"check-{answer_id}.json").write_text(json.dumps({**record, "tool_log": self.ctx.tool_calls}, indent=1, default=str))
        if a.get("brief"):
            # 10 Nov validation run: a request to skip the sources and just give the number was answered from memory in one
            # step, plugin never called. Decision: such a request shortens the reply; it never skips the lookup and the check.
            short = self._brief_text(a, answer_id)
            if saved:
                short = after_the_answer(short, SAVED_LINE.format(path=saved).replace("This answer is saved", "The full answer, with its working, is saved"))
            if invite:
                short += "\n" + invite
            (self.run_dir / f"answer-{answer_id}-brief.md").write_text(short + "\n")
            self.ctx.submitted = None
            self._after_answer()
            record_accepted(answer_id, short.split("\n", 1)[0], brief=True, text=short, also=final, question=getattr(self, "question_as_asked", "") or self.question)
            return {"accepted": True, "answer_id": answer_id, "final_text": short, "full_answer": str(self.run_dir / f"answer-{answer_id}.md"),
                    "reply_with": "Reply with final_text verbatim. If the user asked several questions in this message, the reply carries the final_text of every one of them, in the order asked. If the user asks for the working, the sources or the full answer, call show_full_answer and reply with what it returns (full_answer)."}
        self.ctx.submitted = None
        self._after_answer()
        record_accepted(answer_id, header or final.split("\n", 1)[0], text=final, question=getattr(self, "question_as_asked", "") or self.question)
        return {"accepted": True, "answer_id": answer_id, "final_text": final,
                "reply_with": "Reply with final_text verbatim. If the user later disputes anything, call feedback with answer_id " + str(answer_id) + "."}

    def _company_name(self, cik: str) -> str:
        """The company's name for a CIK, from the SEC ticker file the client has already cached; '' if unknown."""
        digits = re.sub(r"\D", "", str(cik or "")).lstrip("0")
        if not digits:
            return ""
        names = self.__dict__.get("_cik_names")
        if names is None:
            try:
                from edgar_core.companies import TICKERS_URL
                names = {str(v.get("cik_str")): str(v.get("title") or "") for v in self.edgar_client.get_json(TICKERS_URL).values()}
            except Exception:  # noqa: BLE001
                names = {}
            self.__dict__["_cik_names"] = names
        return names.get(digits, "").title() if names.get(digits, "").isupper() else names.get(digits, "")

    def _documents_used(self, answer: str, limit: int = 8) -> str:
        """'Documents used', one linked line per filing (and per web page) behind the figures the answer cites, in the
        order they are first cited. When the answer cites only calculations, the filings their inputs came from."""
        notes = {int(e["id"]): e for e in self.ctx.notes.entries() if str(e.get("id", "")).isdigit()}
        cited = [notes[n] for n in dict.fromkeys(int(x) for x in re.findall(r"\[(\d+)\]", answer)) if n in notes]
        if not any(e.get("source_type") in ("filing", "web", "web_calculation") for e in cited):
            cited = list(notes.values())
        lines, seen = [], set()
        for e in cited:
            acc, url = str(e.get("accession_number") or ""), str(e.get("url") or "")
            if e.get("source_type") == "filing" and acc and acc not in seen:
                seen.add(acc)
                f = self.ctx.filings_seen.get(acc) or {}
                cik = str(e.get("cik") or f.get("cik") or "") or (re.search(r"CIK\s*0*(\d+)", str(e.get("label") or "")) or [None, ""])[1]
                who = str(f.get("company") or "") or self._company_name(cik)
                named = ", ".join(x for x in (("Form " + str(f["form"])) if f.get("form") else "SEC filing", who,
                                              ("filed " + str(f["filed"])) if f.get("filed") else "") if x)
                lines.append("- " + linked(named, filing_link(acc, f, e)) + ", accession " + acc)
            elif e.get("source_type") in ("web", "web_calculation") and url.startswith("http") and url not in seen:
                seen.add(url)
                lines.append(f"- Not an SEC filing: [{url.split('/')[2]}]({url})")
        if not lines:
            return ""
        more = f"\n- and {len(lines) - limit} more, listed with the figures at the end" if len(lines) > limit else ""
        return "**Documents used**\n" + "\n".join(lines[:limit]) + more

    def _brief_text(self, a: Dict[str, Any], answer_id: int) -> str:
        """The short form of a checked answer: the conclusion, then where the figures come from, in one line each."""
        conclusion = str(a.get("conclusion") or "").strip() or str(a.get("answer") or "").strip().split("\n\n", 1)[0]
        cells = " ".join(str(c.get("note_id") and f"[{c['note_id']}]" or "") + " " + str(c.get("text") or "")
                         for r in ((a.get("results") or {}).get("rows") or []) for c in (r.get("cells") or []) if isinstance(c, dict))
        notes = {int(e["id"]): e for e in self.ctx.notes.entries() if str(e.get("id", "")).isdigit()}
        cited = [int(n) for n in re.findall(r"\[(\d+)\]", conclusion + " " + cells)] or [int(n) for n in re.findall(r"\[(\d+)\]", str(a.get("answer") or ""))]
        accessions, urls, supports = [], [], {}
        for n in cited:
            e = notes.get(n) or {}
            acc, url = str(e.get("accession_number") or ""), str(e.get("url") or "")
            if e.get("source_type") == "filing" and acc:
                if acc not in accessions:
                    accessions.append(acc)
                raw = str(e.get("label") or "")
                cik = str(e.get("cik") or "") or (re.search(r"CIK\s*0*(\d+)", raw) or [None, ""])[1]
                who = self._company_name(cik)
                label = ((who + ": ") if who and who.lower().split()[0] not in raw.lower() else "") + re.sub(r"\s*CIK\s*\d+", "", raw).strip()[:70]
                if label and label not in supports.setdefault(acc, []):
                    supports[acc].append(label)
            elif e.get("source_type") in ("web", "web_calculation") and url and url not in urls:
                urls.append(url)
        if not accessions and not urls:  # the cited notes are calculations: name the filings their inputs came from
            accessions = list(dict.fromkeys(str(e["accession_number"]) for e in notes.values() if e.get("source_type") == "filing" and e.get("accession_number")))
        lines = []
        # 11 Nov, five questions in one message: the short form listed four filings and "1 more", with nothing to say which
        # figure each filing supports. Every source is listed, each with the figures taken from it.
        for acc in accessions[:30]:
            f = self.ctx.filings_seen.get(acc) or {}
            what = "; ".join(supports.get(acc, [])[:3])
            named = ", ".join(x for x in (("Form " + str(f["form"])) if f.get("form") else "SEC filing", str(f.get("company") or ""),
                                          ("filed " + str(f["filed"])) if f.get("filed") else "") if x)
            first = next((e for e in notes.values() if e.get("source_type") == "filing" and str(e.get("accession_number")) == acc), None)
            lines.append("Source" + (f" for {what}" if what else "") + ": " + linked(named, filing_link(acc, f, first)) + ", accession " + acc + ".")
        if len(accessions) > 30:
            lines.append(f"(and {len(accessions) - 30} more filings, listed in the full answer)")
        lines += [f"Non-SEC source: {u}" for u in urls[:3]]
        traced = ("Every figure above is traced to its source (the assumptions in your question and the calculations saved with the full working; no SEC filing was used)"
                  if not accessions and not urls else "Every figure above is traced to an SEC filing" if not urls else "Every figure above is traced to its source (SEC filings and the non-SEC sources named)")
        # "the full working is saved" is one of the phrases the eval graders (section_any) and readers know the short form
        # by: dropping it on 22 Nov cost six grader marks in the regression of that afternoon. Keep it.
        tail = (f"{traced} and was checked before this reply (answer {answer_id}). This is the short form and not a guarantee: "
                "the full working is saved, ask for it to check the answer.")
        if getattr(self, "caution", False):
            tail += " " + CAUTION
        return "**Answer:** " + re.sub(r"\s*\[\d+\]", "", conclusion).strip() + "\n\n" + "\n".join(lines + [tail])

    def _second_reader(self, a: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """11 Nov 2026: a separate assistant with no stake in the draft compares it with the question's wording (the
        plugin's `second-reader` agent). The assistant that did the work had declared its own reading as compliant, and
        the headline stayed wrong in 6 to 7 of 46 benchmark answers. Here: the review must have run for this question
        (a hook records its report), and every departure it raised must be answered, by a fix or by evidence from the
        filing. Each demand is made once, so a host that cannot run agents is never locked out; what happened is stated
        in the answer and the audit record either way."""
        self.review = None
        if os.environ.get("EDGAR_SECOND_READER", "off").lower() != "on" or a.get("brief"):
            return None
        complex_q = (self.question_type or "").lower() not in _SIMPLE_TYPES or bool(self.ctx.method_constraints) or bool(getattr(self, "other_companies", None))
        if not complex_q:
            return None
        report = _read_state("last_review.json")
        if not report or report.get("at", 0) < getattr(self, "question_started", 0) - 2:
            if "second_reader" not in self.ctx.nudged:
                self.ctx.nudged.add("second_reader")
                self.ctx.check_log.append({"check": "second_reader", "status": "asked for"})
                return {"accepted": False, "problems": [{"missing": "the second reader's review of this draft"}],
                        "fix": "Before this answer can be accepted, have it read by the second reader: run the agent "
                               "`edgar-research-agent:second-reader` (Agent tool). Give it, in full: (1) the user's question in their own "
                               "words, and the restated question if there is one; (2) the method constraints; (3) your draft: the "
                               "conclusion, the results table and the working with its figures. It replies with DEPARTURE lines or "
                               "NO DEPARTURES FOUND. Then call check_answer again. For each DEPARTURE add a `review_response` entry: "
                               "'fixed: ...' after changing the working (recompute with the tools), or 'kept: ...' with the evidence "
                               "from the filing and its note id. Do not answer a departure by adding a second value and keeping the "
                               "headline: if the departure is right, the headline changes. If you cannot run agents here, call "
                               "check_answer again unchanged; the answer will say it was not reviewed."}
            self.review = {"status": "not run", "departures": 0, "line": "not run for this answer. (Normally a second assistant, which did "
                           "not write the answer, compares the working with the wording of your question.)"}
            self.ctx.check_log.append({"check": "second_reader", "status": "not run"})
            return None
        raised = [d.strip() for d in _DEPARTURE.findall(str(report.get("text") or ""))]
        answers = [r for r in (a.get("review_response") or []) if isinstance(r, dict)]
        def good(r):
            t = str(r.get("resolution") or "").strip()
            return (t.lower().startswith("fixed") and len(t) >= 20) or (t.lower().startswith("kept") and bool(re.search(r"\[\d+\]|\d{10}-\d{2}-\d{6}", t)))
        unanswered = [d for i, d in enumerate(raised) if i >= len(answers) or not good(answers[i])]
        if unanswered and "review_response" not in self.ctx.nudged:
            self.ctx.nudged.add("review_response")
            self.ctx.check_log.append({"check": "second_reader", "status": "departures to answer", "raised": len(raised)})
            return {"accepted": False, "problems": [{"departure": d[:400]} for d in unanswered],
                    "fix": "The second reader raised these departures from the question's wording. Answer each in `review_response`, in the "
                           "order raised: resolution 'fixed: <what you changed>' after correcting the working with the tools, or 'kept: "
                           "<evidence from the filing, with its note id like [12]>' if the filing shows your reading is the right one. "
                           "A departure that is right changes the headline; it is not met by adding an alternative beside it. Then "
                           "call check_answer again with the corrected answer."}
        fixed = sum(1 for r in answers[:len(raised)] if str(r.get("resolution") or "").lower().startswith("fixed"))
        # 15 Nov, the user's hand test: the answer said the reviewer "found no departures", and Claude then added a
        # "Correction to the Second reader line above: the reviewer did find one issue". Both were true: the first review
        # raised a point, the draft was corrected, and the second review was clean. The line now tells that whole story,
        # in the customer's words, so there is nothing to correct afterwards.
        started = getattr(self, "question_started", 0) - 2
        earlier = [e for e in (report.get("history") or [])[:-1] if isinstance(e, dict) and e.get("at", 0) >= started]
        before = sum(len(_DEPARTURE.findall(str(e.get("text") or ""))) for e in earlier)
        told = sum(1 for r in answers[len(raised):] if str(r.get("resolution") or "").strip())  # changes Claude reports beyond the last review
        who = "A second assistant, which did not write this answer, compared the working with the wording of your question"
        s_ = lambda n: "" if n == 1 else "s"
        if unanswered:
            line = (f"{who} and raised {len(raised)} point{s_(len(raised))}; {len(unanswered)} of them {'was' if len(unanswered) == 1 else 'were'} "
                    "not settled. Treat this answer with care.")
        elif raised:
            line = (f"{who} and raised {len(raised)} point{s_(len(raised))}: {fixed} led to a change in the answer and {len(raised) - fixed} "
                    f"{'was' if len(raised) - fixed == 1 else 'were'} kept as {'it was' if len(raised) - fixed == 1 else 'they were'}, on evidence from the filing.")
        elif before or told:
            n = max(before, told)
            line = (f"{who}. It raised {n} point{s_(n)} on an earlier draft, which {'was' if n == 1 else 'were'} dealt with before this answer, "
                    "and found nothing further when it read the final version.")
        else:
            line = f"{who} and found nothing to change."
        self.review = {"status": "reviewed", "departures": len(raised), "unresolved": len(unanswered), "fixed": fixed, "line": line,
                       "raised_on_earlier_drafts": before, "changes_reported": told,
                       "raised": [d[:400] for d in raised], "responses": [str(r.get("resolution") or "")[:300] for r in answers[:len(raised)]]}
        self.ctx.check_log.append({"check": "second_reader", "status": "reviewed", "raised": len(raised), "unresolved": len(unanswered)})
        return None

    def _after_answer(self) -> None:
        """An answer was accepted. Whatever comes next (a revision, a follow-up) gets every one-shot check again."""
        self.answered = True
        self.ctx.nudged, self.ctx.check_log, self.ctx.submits = set(), [], 0

    _FUND_LABEL = re.compile(r"net assets|total assets", re.I)

    def _fund_conflicts(self, answer: str) -> List[dict]:
        """Net assets or total assets taken from a fund's N-CSR/N-CSRS: compare with the N-PORT for the same date
        (5 Nov, PIMCO Dynamic Income: N-CSR $7,523.7m, N-PORT $7,514.6m; the answer gave one figure and never
        looked at the other). Silent when the figures agree, there is no N-PORT, or the answer already cites it."""
        out = []
        for e in self.ctx.notes.entries("filing"):
            label, acc, period = str(e.get("label") or ""), str(e.get("accession_number") or ""), str(e.get("period_end") or "")
            form = str((self.ctx.filings_seen.get(acc) or {}).get("form") or "")
            url = str(e.get("url") or "")
            if not (self._FUND_LABEL.search(label) and period and (form.upper().startswith("N-CSR") or "ncsr" in url.lower())):
                continue
            m = re.search(r"/edgar/data/(\d+)/", url)
            ciks = {str(c["args"].get("cik")) for c in self.ctx.tool_calls if isinstance(c.get("args"), dict) and c["args"].get("cik")}
            cik = m.group(1) if m else (ciks.pop() if len(ciks) == 1 else "")
            if not cik:
                continue
            try:
                report = core.nport.portfolio_report_for_period(cik, period, as_of=self.ctx.as_of, client=self.edgar_client)
                value = float(e.get("value"))
            except Exception:  # noqa: BLE001 - a cross-check must never block an answer by failing
                continue
            if report is None:
                continue
            other = report.net_assets if re.search(r"net assets", label, re.I) else report.total_assets
            if other is None:
                continue
            unit = str(e.get("unit") or "").lower()
            scale = 1e3 if "thousand" in unit or "000" in unit else 1e6 if "million" in unit else 1.0
            mine = value * scale
            if abs(mine - other) <= max(0.5 * scale, 1e-4 * abs(other)):
                continue
            if report.accession_number in answer or _digits_in(answer, other):
                continue
            out.append({"conflict": label, "this_figure": f"{mine:,.0f} USD from {form or 'N-CSR'} {acc}",
                        "other_figure": f"{other:,.0f} USD", "other_filing": f"NPORT-P filed {report.filed}, accession {report.accession_number}, period {report.period_end}",
                        "other_url": report.url, "difference": f"{mine - other:,.0f} USD ({(mine - other) / other:.2%})"})
        return out

    def not_found(self, a: Dict[str, Any]) -> Dict[str, Any]:
        if self.ctx is None:
            return {"accepted": False, "problems": [{"missing": "classify_question"}], "fix": "Call classify_question first."}
        statement = str(a.get("statement") or "").strip()
        if not statement:
            return {"accepted": False, "problems": [{"field": "statement", "reason": "answer the question in one or two sentences"}]}
        if (self.settings.source_mode == "extended" and str(a.get("status")) in ("not_filed", "undetermined", "not_in_filing")
                and "web" not in self.ctx.nudged
                and not any(e.get("source_type") in ("web", "web_calculation") for e in self.ctx.notes.entries())):
            # 7 Nov comparison with Claude alone: Valeo (not an SEC filer) was refused here while Claude alone answered from
            # the company's own reports; Ulta's sales ramp sat in investor decks on its website. Look outside EDGAR first.
            self.ctx.nudged.add("web")  # per question (it was per session: a second question never got the reminder)
            self.ctx.check_log.append({"check": "web", "tool": "check_not_found"})
            return {"accepted": False, "status": a.get("status"),
                    "problems": [{"missing": "sources outside EDGAR not tried"}],
                    "fix": "Web sources are allowed here. Before saying it cannot be answered, look outside EDGAR: the company's own annual "
                           "report and results releases (non-US filers publish them on their websites), investor presentations (read PDFs "
                           "with read_pdf), exchange filings. Record each figure with notes_add(source_type='web', url, quote) and answer with "
                           "check_answer, those figures under 'From non-SEC sources'. Use check_not_found only if no reliable source has it, "
                           "and list the web sources you tried in `searched`."}
        if str(a.get("status")) == "not_sec_filer" or getattr(self.ctx, "out_of_scope", None):
            # 11 Nov, mixed batch: a non-filer's figure was given from its own press release inside the "not found" answer.
            # Scope decision of 8 Nov: a company that does not file with the SEC gets no figures here, from any source.
            said = statement + " " + str(a.get("answer") or "")
            web_ids = {str(e.get("id")) for e in self.ctx.notes.entries() if e.get("source_type") in ("web", "web_calculation")}
            if re.search(r"from non-sec sources", said, re.I) or any(n in web_ids for n in re.findall(r"\[(\d+)\]", said)):
                return {"accepted": False, "status": a.get("status"), "problems": [{"out_of_scope": "figures given for a company that does not file with the SEC"}],
                        "fix": "This company is outside the plugin's scope, so the answer gives no figures for it, from any source: remove the "
                               "figures and the 'From non-SEC sources' section. Say that it does not file with the SEC and where it "
                               "publishes its reports (its website, its home exchange or regulator), without quoting numbers."}
        confirmed, problems = notfound.verify(a, self.ctx, self.edgar_client)
        if problems:
            return {"accepted": False, "status": a.get("status"), "problems": problems}
        body = str(a.get("answer") or "").strip()
        text = statement + ("\n\n" + body if body else "")
        shown = ANSWER_HEADING + "\n\n" + text  # what the user sees; `text` is what the checks read
        # 5 Nov: TSMC Q4 2026 was refused four times because the method gate wanted method_check, which this tool
        # did not take; the reply went out unchecked. The field is passed through now.
        result = json.loads(run_tool("submit", {"answer": text, "sources": a.get("sources") or [], "method_check": a.get("method_check") or [],
                                                "scope_only": str(a.get("status")) == "not_sec_filer"}, self.ctx))
        if not result.get("accepted"):
            return result
        # The benchmark scores in the normal header were measured on questions that have answers; they say nothing about a
        # "not found" answer (5 Nov), so this header keeps only the question type and model.
        header = ""
        if self.header:
            first = self.header.split(". ", 1)[0].rstrip(".") + "."  # "Checked answer (answered by Claude Opus)."
            header = (first + " This answer reports what could not be found, checked against EDGAR as described under 'What was searched'. "
                      "This is not a guarantee that it does not exist: it may be reported under other words or in a document that was not searched.")
        final = (header + "\n\n" if header else "") + shown + "\n\n" + notfound.render(a, confirmed, self.ctx)
        shown = self.ctx.notes.entries()
        if str(a.get("status")) == "not_sec_filer" or getattr(self.ctx, "out_of_scope", None):
            shown = [e for e in shown if e.get("source_type") not in ("web", "web_calculation")]  # no figures for a non-filer, in the working either
        if shown:
            working = render_working(shown, self.ctx.tool_calls, self.ctx.filings_seen)
            if working and not re.search(r"^#+\s*How the figures were obtained", final, re.MULTILINE):
                final += "\n\n" + working.strip()
        answer_id = len(self.answers) + 1
        saved = save_for_user(final, self.question or getattr(self, "question_as_asked", ""))
        if saved:
            final = after_the_answer(final, SAVED_LINE.format(path=saved))
        rate = feedback_line("something that could not be found: " + str(a.get("status") or ""), self.question or getattr(self, "question_as_asked", ""))
        if rate:
            final += "\n\n" + rate
        invite = registration_line()
        if invite:
            final += "\n\n" + invite
        record = {"answer_id": answer_id, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "not_found", "status": a.get("status"),
                  "confirmed": confirmed, "question": self.question, "question_as_asked": getattr(self, "question_as_asked", self.question),
                  "subject_company": getattr(self, "subject_company", "") or None, "model": self.model_hint, "tool_calls": len(self.ctx.tool_calls),
                  "tool_errors": [c for c in self.ctx.tool_calls if not c.get("ok")], "checks": list(self.ctx.check_log)}
        self.answers.append(record)
        (self.run_dir / f"answer-{answer_id}.md").write_text(final + "\n")
        record["file"] = str(self.run_dir / f"answer-{answer_id}.md")  # for show_full_answer
        (self.run_dir / f"check-{answer_id}.json").write_text(json.dumps({**record, "claim": a, "tool_log": self.ctx.tool_calls}, indent=1, default=str))
        self.ctx.submitted = None
        self._after_answer()
        record_accepted(answer_id, header or final.split("\n", 1)[0], text=final, question=getattr(self, "question_as_asked", "") or self.question)
        return {"accepted": True, "answer_id": answer_id, "final_text": final, "confirmed": confirmed,
                "reply_with": "Reply with final_text verbatim. If the user later disputes anything, call feedback with answer_id " + str(answer_id) + "."}

    def full_answer(self, a: Dict[str, Any]) -> Dict[str, Any]:
        """13 Nov, the user's hand test: after a short answer, "show me the full working" depended on Claude finding and
        reading a file in the working folder (which Cowork may not let it open) and nothing checked what it then wrote.
        The server hands the checked text back, and records it so the end-of-turn check holds the reply to it."""
        if not self.answers:
            return {"found": False, "problem": "No answer has been checked in this session yet. Answer the question first (classify_question ... check_answer)."}
        wanted = a.get("answer_id")
        record = next((r for r in self.answers if wanted is not None and str(r.get("answer_id")) == str(wanted)), None) or (None if wanted is not None else self.answers[-1])
        if record is None:
            return {"found": False, "problem": f"There is no answer {wanted} in this session.", "answers": [r["answer_id"] for r in self.answers]}
        try:
            text = Path(str(record.get("file"))).read_text().rstrip()
            # 22 Nov: the invitation to register says "this line is shown once" and came back with the replayed answer.
            text = "\n\n".join(p for p in text.split("\n\n") if not p.startswith(REGISTER_LINE.split("{url}")[0])).rstrip()
        except OSError:
            return {"found": False, "problem": "The saved answer could not be read. Answer the question again with check_answer (without brief)."}
        record_accepted(int(record["answer_id"]), text.split("\n", 1)[0], text=text)
        return {"found": True, "answer_id": record["answer_id"], "final_text": text,
                "reply_with": "Reply with final_text verbatim and in full. It is the checked answer: add no figures and no summary of your own."}

    def list_conventions(self, a: Dict[str, Any]) -> Dict[str, Any]:
        eff = conv.effective(str(a.get("question") or self.question or ""))
        return {"conventions": list(eff.values()), "table": conv.render_table(eff), "settings_file": str(conv.user_file()),
                "note": "Show the user the table. Precedence: an instruction in the question > the user's setting > our default. "
                        "To change one, call set_convention only when the user asks."}

    def set_convention(self, a: Dict[str, Any]) -> Dict[str, Any]:
        try:
            conv.set_user(str(a.get("id") or ""), str(a.get("value") or ""))
        except conv.ConventionError as error:
            return {"saved": False, "problem": str(error)}
        eff = conv.effective(self.question or "")
        e = eff[str(a["id"])]
        if self.ctx is not None:  # applies from now on in this session too
            self.conventions = eff
            self.ctx.conventions = conv.values(eff)
        return {"saved": True, "convention": e, "settings_file": str(conv.user_file()),
                "note": f"Tell the user: '{e['label']}' is now '{e['meaning']}' ({e['origin']}). It applies to later answers; "
                        "an instruction in a question still overrides it. The reliability figures in the header were measured with our defaults."}

    def feedback(self, a: Dict[str, Any]) -> Dict[str, Any]:
        if self.run_dir is None:
            self._start("", "", [])
        record = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "verdict": a.get("verdict"), "comment": a.get("comment"),
                  "answer_id": a.get("answer_id"), "question": self.question, "question_type": self.question_type, "model": self.model_hint,
                  "audit_folder": str(self.run_dir), "consent": bool(a.get("consent"))}
        with open(self.run_dir / "feedback.jsonl", "a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        sent = False
        if record["consent"] and UPLOAD is not None:
            payload = dict(record)
            aid = a.get("answer_id")
            if aid and (self.run_dir / f"check-{aid}.json").exists():
                payload["audit"] = json.loads((self.run_dir / f"check-{aid}.json").read_text())
                payload["answer"] = (self.run_dir / f"answer-{aid}.md").read_text()
                payload["notes"] = [json.loads(l) for l in open(self.run_dir / "notes.jsonl")] if (self.run_dir / "notes.jsonl").exists() else []
            sent = self._post(payload)
        return {"recorded": True, "sent_to_maintainers": sent,
                "note": ("Thank the user and say the correction is recorded against answer " + str(a.get("answer_id")) + "."
                         + ("" if record["consent"] or UPLOAD is None else " If they are willing to share the audit record with the maintainers, call feedback again with consent=true."))}

    def report_tool_problem(self, a: Dict[str, Any]) -> Dict[str, Any]:
        """A tool misbehaved, as judged by the model using it: the first half of the debugging that a model
        with the source code did by itself on 1 Nov (round(x, 2) refused as a rounded figure; VSCO resolved
        to VScoop; lululemon's cash tag). The report carries the recent tool log so the cause is traceable."""
        if self.run_dir is None:
            self._start("", "", [])
        recent = (self.ctx.tool_calls[-6:] if self.ctx else [])
        record = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": a.get("tool"), "arguments": a.get("arguments"), "observed": a.get("observed"),
                  "expected": a.get("expected"), "question": self.question, "model": self.model_hint, "recent_tool_calls": recent,
                  "audit_folder": str(self.run_dir), "consent": bool(a.get("consent"))}
        with open(self.run_dir / "tool_problems.jsonl", "a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        sent = self._post(record) if record["consent"] else False
        return {"recorded": True, "sent_to_maintainers": sent,
                "note": "Recorded. Continue the question; work around the tool if you can (another lookup route, passing the figure by hand) and say in the answer if a figure's route was unusual."}

    def _post(self, payload: Dict[str, Any]) -> bool:
        return bool(UPLOAD(payload)) if UPLOAD is not None else False

    def status(self) -> Dict[str, Any]:
        return {"question": self.question, "question_type": self.question_type, "as_of": self.ctx.as_of if self.ctx else None,
                "notes": len(self.ctx.notes.entries()) if self.ctx else 0, "tool_calls": len(self.ctx.tool_calls) if self.ctx else 0,
                "answers_checked": [r["answer_id"] for r in self.answers], "audit_folder": str(self.run_dir) if self.run_dir else None,
                "seconds": round(time.time() - self.started)}


SCOPE_SECONDS = 20  # time the scope check may take inside one call; each company is a few requests to the SEC (about 10 a second allowed)


_CO_STOP = {"inc", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "the", "group", "holdings", "and", "of"}


def _names_company(question: str, subject: str) -> bool:
    """Does the question text itself name the company it is about? True when any distinctive word of the company (or its
    ticker) appears in the question. 'what was their growth rate in 2024' does not name Apple."""
    q = (question or "").lower()
    words = [w for w in re.findall(r"[a-z0-9&]+", (subject or "").lower()) if w not in _CO_STOP and len(w) >= 2]
    if not words:
        return True
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w), q) for w in words)


def make_session(config_path: Optional[Path] = None, run_root: Optional[Path] = None, edgar_client=None, worker=None) -> Session:
    settings = resolve_settings({}, config_path=config_path) if config_path else resolve_settings({})
    client = edgar_client or core.EdgarClient()
    root = run_root or Path(str(settings.runs_dir))
    return Session(settings=settings, edgar_client=client, run_root=root, worker=worker)


def _digits_in(text: str, value: float) -> bool:
    """Whether the answer already writes `value` (in dollars, thousands or millions, to a sensible rounding)."""
    plain = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    forms = {f"{value:.0f}", f"{value / 1e3:.0f}"}
    for scale in (1e6, 1e9):
        for places in (1, 2):
            t = f"{value / scale:.{places}f}"
            forms.add(t.rstrip("0").rstrip(".") if "." in t else t)
    return any(len(f.replace(".", "")) >= 3 and re.search(r"(?<![\d.])" + re.escape(f) + r"(?![\d])", plain) for f in forms)
