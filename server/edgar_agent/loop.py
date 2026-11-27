"""The run loop (SPEC.md section 7.1).

Phase A: one model call fills in a form (companies, metrics, periods, format,
question type). The program sets the turn budget from it.
Phase B: the loop. Each turn the model sees the conversation and replies with
tool calls; the program runs them and appends the results, until `submit` is
accepted or the budget runs out.

The model is behind a small interface, `ModelClient`, so the loop can be
tested with a scripted stand-in and run with Claude or a local model.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from .config import MODEL_PRICES, Settings
from .notes import Notes
import edgar_core as core
from .provenance import render_working
from .tools import RunContext, run_tool, tools_for

EASY = {"quantitative retrieval", "qualitative retrieval", "numerical reasoning"}
MEDIUM = {"complex retrieval", "adjustments", "beat or miss", "fund holdings"}
HARD = {"trends", "financial modelling", "market analysis"}
QUESTION_TYPES = sorted(EASY | MEDIUM | HARD)


class ModelError(RuntimeError):
    """A model call failed: server unreachable, timed out, or an unusable reply."""


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: List[dict] = field(default_factory=list)  # [{"id", "name", "args"}]
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning: str = ""  # hidden "thinking" some servers return separately; logged, not fed back
    raw: Any = None  # the server's message as received, for the run log
    truncated: bool = False  # the server cut the reply off at the output limit (finish_reason 'length')


class ModelClient(Protocol):
    model: str

    def complete(self, system: str, messages: List[dict], tools: List[dict]) -> ModelReply: ...


@dataclass
class RunResult:
    question: str
    plan: dict
    difficulty: str
    budget: int
    turns: int
    answer: Optional[str]
    sources: List[dict]
    accepted: bool
    stopped_because: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    seconds: float
    notes: List[dict]
    tool_calls: List[dict]
    run_dir: Optional[str]
    method_check: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__


PLAN_PROMPT = """Read the question and fill in this JSON form. Reply with the JSON only.
{{"companies": [names or tickers mentioned], "metrics": [what is being asked for], "periods": [periods or dates mentioned],
 "format": "any format the question demands, or empty", "as_of": "a date the question says the answer is 'as of', YYYY-MM-DD, or empty",
 "method_constraints": [every instruction in the question about HOW to compute or what to use: which document, which share count, average vs year-end balances, day counts, which guidance point (midpoint/low/high), what to include or exclude, which price date, assumptions to hold; quote each briefly in the question's words; empty list if none],
 "question_type": one of {types}}}
Question: {question}"""


_PERIOD = re.compile(r"\b(?:Q[1-4]\s*'?\d{2,4}|(?:FY\s*)?(?:19|20)\d{2}|(?:first|second|third|fourth)\s+quarter)\b", re.IGNORECASE)
_MEDIUM_WORDS = re.compile(r"\b(versus|vs\.?|compared?|guidance|guide[ds]?|beat|miss|strategy|how has|each of|every|breakdown|decompos|portion|driven by|implied|over the (?:past|last))\b", re.IGNORECASE)
_HARD_WORDS = re.compile(r"\b(project|forecast|assum\w+|scenario|model|dilut\w+|peer|competitor|industry|market share|align)\b", re.IGNORECASE)


def difficulty_of(question_type: str, question: str = "") -> str:
    """The model's classification sets the floor; signals in the question text can only
    raise it. A weak model calls a four-quarter guidance hunt 'simple retrieval'."""
    q = (question_type or "").lower().strip()
    level = "hard" if q in HARD else "medium" if q in MEDIUM else "easy"
    rank = {"easy": 0, "medium": 1, "hard": 2}
    periods = len(set(m.lower() for m in _PERIOD.findall(question or "")))
    if level == "easy" and (periods >= 3 or _MEDIUM_WORDS.search(question or "")):
        level = "medium"
    if rank[level] < 2 and (_HARD_WORDS.search(question or "") or periods >= 5):
        level = "hard"
    return level


TRIMMED = "(older tool result trimmed to save context; anything you recorded from it is in notes, call notes_read if needed)"


def trim_context(messages: List[dict], max_chars: int, keep_recent: int = 3) -> int:
    """Context management (§7.4). The conversation is mostly tool results, and once
    the model has recorded what it needed from one, the rest is dead weight. When the
    conversation exceeds `max_chars`, replace the oldest tool results (never the most
    recent `keep_recent` turns' worth) with a short stub, oldest first, until it fits.
    Tool calls, the model's own text and the notes are never touched. Returns the
    number of results trimmed."""
    def size() -> int:
        return len(json.dumps(messages, default=str))

    if size() <= max_chars:
        return 0
    # Trim in one big step, down to half the cap, not "just enough": the model server
    # caches the processed prompt and every rewrite of old messages invalidates it, so
    # trimming a little every turn made each turn ten times slower (TSM, 25 Oct).
    target = max_chars // 2
    result_slots = []  # (message index, item index) for every tool_result, in order
    for mi, m in enumerate(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for ii, item in enumerate(m["content"]):
                if item.get("type") == "tool_result" and item.get("content") != TRIMMED:
                    result_slots.append((mi, ii))
    recent = {mi for mi, _ in result_slots[-keep_recent:]}  # the last few results stay intact
    # Largest first: one 12k-character document read is worth more context than twenty small
    # results, and the small ones (a price change, a fact, a profile) are what the model refers
    # back to. Results under 1,500 characters are never trimmed.
    ordered = sorted((mi, ii) for mi, ii in result_slots if mi not in recent)
    ordered.sort(key=lambda s: -len(messages[s[0]]["content"][s[1]]["content"]))
    trimmed = 0
    for mi, ii in ordered:
        if size() <= target:
            break
        if len(messages[mi]["content"][ii]["content"]) < 1500:
            continue
        messages[mi]["content"][ii]["content"] = TRIMMED
        trimmed += 1
    return trimmed


def _constraint_reminder(ctx) -> str:
    cs = getattr(ctx, "method_constraints", None) or []
    if not cs:
        return ""
    return " Method constraints to account for in method_check: " + "; ".join(f"{i}. {c}" for i, c in enumerate(cs, start=1))


def parse_plan(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    plan = {"companies": [], "metrics": [], "periods": [], "format": "", "as_of": "", "method_constraints": [], "question_type": "quantitative retrieval"}
    if match:
        try:
            plan.update({k: v for k, v in json.loads(match.group(0)).items() if k in plan})
        except json.JSONDecodeError:
            pass
    mc = plan.get("method_constraints")
    plan["method_constraints"] = [str(c).strip() for c in (mc if isinstance(mc, list) else [mc] if mc else []) if str(c).strip()][:20]
    return plan


def constraints_block(constraints: List[str]) -> str:
    if not constraints:
        return ""
    lines = "\n".join(f"  {i}. {c}" for i, c in enumerate(constraints, start=1))
    return (f"\nMETHOD CONSTRAINTS FROM THE QUESTION (mandatory; the grader treats each as a dealbreaker):\n{lines}\n"
            "Apply every one exactly as stated, even where the standard convention differs. submit requires a method_check listing, "
            "for each number above, how you applied it (the figure or document used); a submit without it is refused.\n")


def definitions_block(metrics) -> str:
    """Catalogue definitions for the metrics the question names, so the convention arrives before the
    model computes anything (Caesars' EBITDAR, 30 Oct: the model never called describe_metric)."""
    if not metrics:
        return ""
    from .tools import definition_line
    # The pitfalls carry the caveats a correct answer must state (EBITDA is not cash; reported EPS is rounded; a CDS payout
    # is gross). Until 3 Nov only the formula was sent, so they reached Claude only if it happened to call describe_metric.
    # 7 Nov: definition_line keeps only the formula's first sentence (right for the answer's own section), which cut the
    # industry entries down to "Two legitimate meanings" and Remitly's net revenue was answered with reported revenue.
    # The prompt gets the whole formula; the short line is still the model for 'Definitions used'.
    lines = "\n".join(f"  - {m.name}: {m.formula}\n    Cite as: {definition_line(m)}"
                      + (f"\n    Watch: {m.pitfalls}" if getattr(m, "pitfalls", "") else "") for m in metrics)
    return (f"\nDEFINITIONS THAT APPLY (from the metric catalogue; a definition stated in the question, the filing or an agreement overrides it, and then you say so):\n{lines}\n"
            "Compute these exactly as defined, pass metric=<name> to calculate, and end the answer with a 'Definitions used' section naming each one and its source.\n")


def system_prompt(settings: Settings, as_of: str, budget: int, constraints: Optional[List[str]] = None, metrics=None) -> str:
    mode = settings.source_mode
    # 7 Nov: the plugin passes budget 0 (Claude has no turn budget there); it used to read "You have 0 turns".
    budget_line = f"- You have {budget} turns. Submit before they run out; a partial answer with the gaps named beats no answer." if budget else "- There is no turn limit: take the steps the question needs; accuracy matters more than speed."
    return f"""You are a financial research agent working from SEC filings on EDGAR. Today, for this question, is {as_of}: nothing filed after that date exists for you.
{constraints_block(constraints or [])}{definitions_block(metrics or [])}
Rules that are checked in code, not just asked of you:
- The web is for discovery; EDGAR is the source. Every figure or quotation in your main answer must come from a filing and be recorded with notes_add(source_type='filing', accession_number=...). Figures from the web may not appear in the main answer. {"In extended mode a web-only figure may be given under a heading 'From non-SEC sources' with its URL; it must not be used in any calculation in the main answer. Share prices come from price_history / price_change (market data, recorded as non-SEC): price performance, market capitalisation, EV multiples and deal premiums computed from them go under that heading too, with the provider and dates." if mode == "extended" else "This run is in strict mode: if a figure exists only outside filings (share prices, market data, analyst estimates), say it is not in SEC filings as of the date and do not report it."}
- Valuation is done by the code's conventions, not yours: enterprise value (and EV/EBITDA, lease-adjusted leverage) with build_ev, which assembles every component from the balance sheet and never forgets the lease liabilities; a DCF with the calculate functions ufcf(ebit, da, tax_rate, capex, nwc_change_cf, sbc), npv(rate, cashflows), terminal_exit(ebitda, multiple), terminal_growth(fcf, rate, g) and dcf_ev(ufcfs, rate, terminal_value), which apply the catalogue's mechanics (end-of-year discounting, SBC added back, NWC from the cash-flow line, terminal value discounted n full years). Hand-written discounting or EV sums are a common source of graded misses.
- Never do arithmetic yourself: use calculate, which records the result. For a multi-step model (DCF, LBO, a projection, several ratios at once) use calculate with `script`: the whole model in ONE call, every intermediate value recorded, instead of one call per step. Round only in the final line, to two decimals unless the question says otherwise. Always state units as the filing states them (thousands, millions).
- Use the company's own fiscal calendar and its own fiscal-year NAMES. Every fact from get_financial_fact carries `fiscal_year`, the label the company itself gives that period in its filing (Gap Inc.'s year ending 2025-02-01 is fiscal_year 2024; NVIDIA's year ending 2025-01-26 is fiscal_year 2025: January year-ends differ, so never work it out from the calendar). A question's "FY2024" / "fiscal 2024" means the fact whose fiscal_year is 2024; say which period end that is. For figures read from documents rather than facts, find the sentence that names the fiscal year ("fiscal 2024 ended February 2, 2025").
- A value from get_financial_fact is the company's own XBRL data from that filing and counts as a verified filing fact (the tool records it). It is the headline, company-wide figure: whenever the question qualifies it (net, core, payments-only, organic, adjusted, by segment or product, excluding something) or the concept is missing, read the documents for the narrower line: list_line_items, the 10-K or 10-Q tables, and the period's earnings releases (8-K item 2.02, exhibit 99.1) and investor presentations, which often carry splits the 10-K does not. Accuracy matters more than speed: take the turns you need.
- Restatements: by default get_financial_fact returns each period's value from the most recently filed report that contains it, so a 2021 figure may come from the 2023 10-K and differ from the original 2021 10-K (spin-offs move businesses to discontinued operations; accounting changes restate prior years). That most-recent value is the default to use and to compare across years. When the question says "as originally reported" (or the conventions in force say so), the tool returns each value as first reported instead and says so in its `basis` field: use those and cite each year's own filing. If the two differ and you have seen both, say in one line that prior years were restated, and why if the filing says.
- Work from the documents, not memory. find_in_document and get_table find the right place; read around it with read_document whenever the context matters, and read further rather than guess.
- One answer per reading. If a question is ambiguous, headline the standard reading; when a second reading is also legitimate, give it as a labelled second value with the reason both exist. Never offer unexplained alternatives or a range of guesses. If something cannot be found, say so rather than estimate.
- Conclusions first, plainly. When a question asks how management frames something, which company is better positioned, which method yields more, or whether something beat or missed, state that conclusion as the first line in the question's own terms ("Management frames AI as a tailwind that drives platform demand"), then the evidence, then any caveat. A hedge in place of the conclusion ("the filing does not use that word", "it depends") is graded as no answer.
- The filer may not publish what the question asks for (a company that guides net income, not revenue; reserves reported only as a combined line). Before concluding that, look everywhere it could be: the 10-K and 10-Qs (tables, notes, MD&A), every earnings release and investor presentation in the period (filings_in_window), list_line_items, and full-text search. Only then say in one line which documents do not report it, give the nearest figure they do report, and finish with check_not_found.
- A question that asks you to list, summarise or describe what a filing says wants every distinct point the relevant section makes, each in its own line, with the filing's own terms (law names, metric names, counterparties). Completeness beats brevity there; brevity matters only for single-figure questions.
- When the filing itself names a figure with the term the question uses ("total consideration of approximately $3.25 billion plus a $50 million adjustment"), report the figure as the filing labels it, and list adjustments, add-ons or ranges separately. Do not combine them into a new headline number unless the question asks for a combined figure.
- For a figure that lives in a table (revenue by type, operating statistics, share counts, debt terms), call get_table with a keyword from the question first; it returns the whole table at once. Use find_in_document for sentences, and read_document only to read around a hit. For completeness questions (summarise, list all, every adjustment or line item, how management describes or explains X, what drove a change, decompose) call extract on the relevant document or item FIRST, before searching: it reads everything and returns every relevant passage verbatim, so nothing depends on guessing the filer's wording. Also call it whenever find_in_document has not produced the specific line or driver the question asks for after two tries. Search hits are not evidence that you have seen everything; extract is.
- Record every figure you take from one table or passage with notes_add (one call with `facts`, a list, is easiest).
- For comparisons with guidance, estimates or prior periods, use the exact figure from the financial statements table in the release (e.g. "3,611,497" in thousands), not a rounded headline ("$3.6 billion"); the headline is for words, the table is for arithmetic. Report a beat or miss against the guidance midpoint AND against the range (low and high ends), each with its bps or % difference, unless the question names one.
- Named metrics: before computing any named ratio, growth rate, multiple, coverage, valuation or deal figure, call describe_metric and use that formula (averages where it says average, the period count it gives for CAGR, the components it lists). A definition in the question or the filing overrides it. When the catalogue says the convention varies, say in the working which choice you made and why.
- Definitions used. Whenever you compute a named metric (a margin, a coverage ratio, EBITDAR, CAGR, enterprise value, a premium...), look it up with describe_metric first or pass metric=<name> to calculate, and end the answer with a section "Definitions used": one line per metric giving the formula and its source as the catalogue states it (a CFA curriculum reading, a rating-agency methodology, credit-agreement practice, the company's own definition). If the question, the filing or an agreement defines the metric differently, use that definition and name it as the source instead. An answer that computes a catalogue metric without this section is sent back once.
- Cite every figure. Each figure in the main answer is followed by the id of the note that holds it, in brackets: "revenue of $2,241,236,000 [3] grew at a 32.82% CAGR [9]". notes_add and calculate return the ids (note_id / note_ids). The check compares each cited figure with its note, so a correct citation is never rejected; a figure without a citation is checked the slow way and may be. Dates, years, form names, item numbers and counts need no citation.
- State every figure the question implies, each as its own number: if it asks to compare with a peer average, write the average; if it asks how a company stands "relative to the peer set", "versus peers" or "against the group", compute and write the peer average (simple mean of the companies named, including the company itself unless told otherwise) for every metric, next to each company's figure; if it asks for a difference or a change, write the difference and the percentage; if it asks for an increase, write the increase in both dollars and percent. A figure you computed on the way (a subtotal, an intermediate ratio) is also stated. Graders check each implied number as a separate line; a number you had but did not write is a miss.
- Exactness: report share counts to the share (432,876,657, not "433 million"), ratios and margins to the decimals the filing gives, and every class or category the filing lists (a class with zero shares is still listed with its figure). When a question names a line item ("cost of sales", "Adjusted EBITDA reconciliation"), take the line with that label from the primary statement or reconciliation, not a similarly named line from a segment or geographic table.
{budget_line}

Method: forms_for to choose where to look; resolve_entity and get_entity_profile; find the newest relevant filing (search results come newest first; prefer short press-release exhibits and the latest 10-K over old registration statements); gather with get_table and find_in_document, recording each fact with notes_add as you go; calculate; then submit with: the answer in the requested format, the working (inputs with sources, formulas, results), sources (form, company, period, filing date, accession number, URL), and caveats only when real."""


def run_question(question: str, settings: Settings, model: ModelClient, edgar_client, run_dir: Optional[Path] = None,
                 web_search=None, progress=None, worker: Optional[ModelClient] = None) -> RunResult:
    """`progress(message)` is called as the run goes, so a user watching the
    terminal can see what the model is doing. Pass None for silence."""
    started = time.time()
    say = progress or (lambda message: None)
    as_of = settings.as_of or time.strftime("%Y-%m-%d")
    price_in, price_out = MODEL_PRICES.get(model.model, (0.0, 0.0))
    tokens_in = tokens_out = 0

    w_in, w_out = MODEL_PRICES.get(getattr(worker, "model", "") or "", (0.0, 0.0)) if worker is not None else (0.0, 0.0)

    def cost() -> float:
        wt = getattr(ctx, "worker_tokens", None) if "ctx" in locals() else None
        extra = (wt["input"] / 1e6 * w_in + wt["output"] / 1e6 * w_out) if wt else 0.0
        return tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out + extra

    # Phase A: the form.
    reply = model.complete("You classify financial research questions.", [{"role": "user", "content": PLAN_PROMPT.format(types=QUESTION_TYPES, question=question)}], [])
    tokens_in += reply.input_tokens; tokens_out += reply.output_tokens
    plan = parse_plan(reply.text)
    # A date in the question names a period the question is about (a fiscal
    # year end, "as of Dec 31, 2024"); it is never the filing cutoff. The
    # filing for a period is filed weeks or months after the period ends, so
    # using the period date as the cutoff would hide the very filing needed.
    # The cutoff comes only from settings (flag, env, config file) or today.
    difficulty = difficulty_of(plan["question_type"], question)
    budget = settings.turn_budget(difficulty)
    say(f"classified as {plan['question_type']} ({difficulty}, up to {budget} turns); as of {as_of}")

    notes = Notes(run_dir / "notes.jsonl" if run_dir else None)
    ctx = RunContext(client=edgar_client, notes=notes, as_of=as_of, source_mode=settings.source_mode, question=question,
                     web_search=web_search if settings.web_search == "on" else None, max_chars=int(settings.tool_result_max_chars),
                     price_provider=str(settings.values.get("price_provider", "auto")), price_api_key=os.environ.get("EDGAR_PRICE_API_KEY", ""))
    ctx.worker = worker
    profile = "local" if settings.model == "local" else "claude"
    tools = tools_for(profile, settings.source_mode, with_worker=worker is not None)
    named_metrics = core.metrics_in_text(question)
    system = system_prompt(settings, as_of, budget, plan.get("method_constraints"), named_metrics)
    for met in named_metrics:
        ctx.metrics_used[met.name] = met  # submit will want them in 'Definitions used'
    if named_metrics:
        say(f"catalogue metrics named in the question: {', '.join(m.name for m in named_metrics)}")
    ctx.method_constraints = list(plan.get("method_constraints") or [])
    if ctx.method_constraints:
        say(f"method constraints from the question: {len(ctx.method_constraints)}")
    messages: List[dict] = [{"role": "user", "content": f"Question: {question}\n\nPlan so far: {json.dumps(plan)}"}]
    stopped = "budget exhausted"
    turns = 0
    silent = 0  # consecutive replies with neither text nor tool calls
    replies_log: List[dict] = [{"phase": "A", "text": reply.text, "reasoning": reply.reasoning, "raw": reply.raw}]

    # Phase B: the loop.
    # Logs are written even if something unexpected breaks the loop, so a
    # failed run can still be read afterwards.
    try:
        cap = settings.cost_cap()
        final_for_cost = False
        while turns < budget:
            turns += 1
            if cost() > cap:
                if final_for_cost:
                    stopped = "cost cap reached"
                    break
                final_for_cost = True  # one last turn with submit only, as at the turn limit: anything gathered beats nothing
            turn_tools = tools
            if turns == budget or final_for_cost:
                # Last turn: the only move left is to submit. Anything gathered beats nothing.
                turn_tools = [t for t in tools if t["name"] in ("submit", "notes_add")]
                why = f"Final turn ({turns} of {budget})" if turns == budget else f"Cost cap reached (${cost():.2f} of ${cap:.2f})"
                messages.append({"role": "user", "content": f"{why}. Call submit now with the best answer the notes support, naming any gaps. No other tool is available." + _constraint_reminder(ctx)})
            elif turns >= int(budget * 0.8) or cost() >= 0.8 * cap:
                where = f"Turn {turns} of {budget}" if turns >= int(budget * 0.8) else f"${cost():.2f} of the ${cap:.2f} cost cap used"
                messages.append({"role": "user", "content": f"{where}: wrap up. Record what you have found with notes_add and submit; name any gaps." + _constraint_reminder(ctx)})
            trimmed = trim_context(messages, int(settings.context_max_chars))
            if trimmed:
                say(f"  (trimmed {trimmed} older tool results to keep the conversation within the context limit)")
            say(f"turn {turns}/{budget}: waiting for the model ({int(time.time() - started)}s elapsed)")
            try:
                reply = model.complete(system, messages, turn_tools)
            except ModelError as exc:
                # Out-of-memory crashes look like dropped connections; shrink hard before retrying.
                trim_context(messages, int(settings.context_max_chars) // 2)
                # A lost reply (timeout, server hiccup) costs a turn, not the question.
                say(f"  model call failed: {exc}")
                replies_log.append({"turn": turns, "error": str(exc)})
                messages.append({"role": "assistant", "content": [{"type": "text", "text": "(no reply arrived)"}]})
                messages.append({"role": "user", "content": f"Your last reply did not arrive ({exc}). Keep your reasoning short. "
                                 "Reply with one tool call: submit now with the figures already in your notes, or the next step if a figure is missing."})
                silent += 1
                if silent >= 3:
                    stopped = f"model failed three times in a row: {exc}"
                    break
                continue
            tokens_in += reply.input_tokens; tokens_out += reply.output_tokens
            for call in reply.tool_calls:
                say(f"  -> {call['name']}({', '.join(f'{k}={str(v)[:40]!r}' for k, v in (call['args'] or {}).items())})")
            if reply.text and not reply.tool_calls:
                say(f"  model said: {reply.text[:160]!r}")
            replies_log.append({"turn": turns, "text": reply.text, "tool_calls": reply.tool_calls,
                                "reasoning": reply.reasoning[:4000], "raw": reply.raw})
            if not reply.text and not reply.tool_calls:
                silent += 1
                if silent >= 3:
                    stopped = "model gave three empty replies in a row" + (" (each cut off while still thinking)" if reply.truncated else "")
                    break
            else:
                silent = 0
            assistant_content: List[dict] = []
            if reply.text:
                assistant_content.append({"type": "text", "text": reply.text})
            for call in reply.tool_calls:
                assistant_content.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["args"]})
            messages.append({"role": "assistant", "content": assistant_content or [{"type": "text", "text": "(no reply)"}]})
            if not reply.tool_calls:
                if reply.truncated and not reply.text:
                    # A thinking model that plans the whole task in its head hits the output limit before
                    # acting. Ask for one step, and (Qwen) switch thinking off for this one turn.
                    nudge = ("Your reply was cut off at the output limit while you were still thinking, so nothing happened. "
                             "Do not plan the whole task. In one or two sentences decide the single next step, then call ONE tool now. /no_think")
                elif not reply.text:
                    nudge = "Your reply was empty. Reply with a tool call: the next step, or submit with the answer so far."
                elif len(reply.text) > 200:
                    nudge = ("You wrote an answer as text, but nothing is recorded until you call the submit tool. "
                             "Call submit(answer=<that text>, sources=[...]) now.")
                else:
                    nudge = "Continue. Use a tool, or submit."
                messages.append({"role": "user", "content": nudge})
                continue
            results = []
            for call in reply.tool_calls:
                output = run_tool(call["name"], call["args"], ctx)
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": output})
            if ctx.worker is not None and not any(c["tool"] == "extract" for c in ctx.tool_calls):
                searches = [c for c in ctx.tool_calls if c["tool"] in ("find_in_document", "read_document")]
                if len(searches) >= 4:
                    results.append({"type": "text", "text": "Note from the harness: four or more searches/reads and no extract yet. If the question asks what drove something, "
                                    "for every item, or how management explains it, call extract(url, question) on the document now; it reads the whole thing and returns every relevant passage."})
            messages.append({"role": "user", "content": results})
            for res in results:
                if '"accepted": false' in res.get("content", ""):  # the harness note has "text", not "content" (crashed 3 questions, 30 Oct)
                    say("  submit rejected by the sourcing check; the model will fix and resubmit")
            if ctx.submitted:
                stopped = "submitted"
                break

    except Exception:
        _write_logs(run_dir, settings, messages, replies_log)
        raise

    result = RunResult(
        question=question, plan=plan, difficulty=difficulty, budget=budget, turns=turns,
        answer=ctx.submitted["answer"] if ctx.submitted else None,
        sources=ctx.submitted["sources"] if ctx.submitted else [],
        method_check=ctx.submitted.get("method_check", []) if ctx.submitted else [],
        accepted=bool(ctx.submitted), stopped_because=stopped, cost_usd=round(cost(), 4),
        input_tokens=tokens_in, output_tokens=tokens_out, seconds=round(time.time() - started, 1),
        notes=notes.entries(), tool_calls=ctx.tool_calls, run_dir=str(run_dir) if run_dir else None,
    )
    if run_dir:
        _write_logs(run_dir, settings, messages, replies_log)
        (run_dir / "result.json").write_text(json.dumps(result.to_dict(), indent=1, default=str))
        if result.answer:
            (run_dir / "answer.md").write_text(result.answer)
            working = render_working(result.notes, result.tool_calls, ctx.filings_seen)
            if working:
                (run_dir / "working.md").write_text(working)
    return result


def _write_logs(run_dir, settings, messages, replies_log) -> None:
    if not run_dir:
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "settings.json").write_text(json.dumps(settings.to_dict(), indent=1))
    (run_dir / "conversation.json").write_text(json.dumps(messages, indent=1, default=str))
    (run_dir / "model_replies.json").write_text(json.dumps(replies_log, indent=1, default=str))
