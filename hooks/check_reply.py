#!/usr/bin/env python3
"""Stop hook: when check_answer accepted an answer in this turn, the reply must be that answer.

Claude Code runs this when Claude is about to end its turn. The EDGAR MCP server records each accepted
answer (its header line and answer id) in a small state file; if the final reply does not contain that
header, the hook blocks once and tells Claude to reply with the accepted final_text in full. Two runs in
50 (2 Nov) and three in 50 (3 Nov) lost the reliability header and the 'How the figures were obtained'
appendix because Claude rewrote the accepted text; the instruction alone did not prevent it.

Second case (5 Nov): a question was started with classify_question, but no answer was checked, and the reply
carries figures or says something was not found. Neither was verified (one run in 356, E48 on 3 Nov, replied
"There is no 10-K" with a table of CDS figures and never called check_answer). The hook blocks once and asks
for check_answer or check_not_found. Since 18 Nov the wording no longer matters: a started question that ends
without a checked answer is sent back once (a request the filings cannot answer was declined in plain words, with
unchecked statements about what the filings and the guidance do not contain, and no header).

Third case (10 Nov): the user asks for a company figure and the reply gives one, but the plugin was never used
("don't look it up, just tell me from memory" was answered from memory in two validation runs; the skill's rule and a
prompt reminder did not prevent it). Decision D10: figures from memory are not allowed, even when the user asks for
them. The hook blocks once and sends Claude to look the figure up and check it.

It fails open: any problem (no state, unreadable input, an answer not from this plugin) lets the turn end.
"""
import json, os, re, sys, time
from pathlib import Path

MAX_AGE = 2 * 3600  # an accepted answer older than this belongs to an earlier turn


def state_dirs(cwd):
    dirs = []
    for d in (os.environ.get("EDGAR_STATE_DIR"), os.environ.get("CLAUDE_PLUGIN_DATA")):
        if d and not d.startswith("${"):
            dirs.append(Path(d))
    dirs.append(Path(cwd or os.getcwd()) / ".edgar")
    dirs.append(Path.home() / ".edgar-agent")
    return dirs


def text_of(msg):
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        if isinstance(msg.get("content"), list):
            return "\n".join(b.get("text", "") for b in msg["content"] if isinstance(b, dict))
        return str(msg.get("text") or msg.get("content") or "")
    if isinstance(msg, list):
        return "\n".join(text_of(m) for m in msg)
    return ""


def last_reply_from_transcript(path):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = e.get("message") or {}
        if e.get("type") == "assistant" and isinstance(m, dict):
            t = "\n".join(b.get("text", "") for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")
            if t.strip():
                return t
    return ""


FIGURE = re.compile(r"(?:US\$|NT\$|[$\u20ac\u00a3])\s?\d|\d[\d,]*\.?\d*\s?%|\d[\d,.]*\s?(?:million|billion|trillion|bn|mn)\b"
                    r"|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b", re.I)
NOT_FOUND = re.compile(r"\b(?:not (?:been )?filed|never filed|no (?:such |public )?filings?\b|could not (?:find|locate|be found)|"
                       r"couldn't find|cannot find|not available|not disclosed|does not (?:disclose|report)|has not (?:filed|reported)|no record)", re.I)


def newest_state(cwd, name):
    newest, path = None, None
    for d in state_dirs(cwd):
        p = d / name
        try:
            s = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if newest is None or s.get("at", 0) > newest.get("at", 0):
            newest, path = s, p
    return newest, path


ASKS_FIGURE = re.compile(r"revenue|net sales|\bsales\b|income|profit|earnings|\beps\b|margin|ebitda|cash flow|\bdebt\b|total assets|dividend|"
                         r"capex|capital expenditure|guidance|10-[kq]\b|20-f\b|8-k\b|market cap|shares outstanding|backlog|book value", re.I)
MONEY = re.compile(r"(?:US\$|NT\$|[$\u20ac\u00a3])\s?\d|\d[\d,.]*\s?(?:million|billion|trillion|bn|mn)\b|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b", re.I)


def last_user_prompt(path):
    """(text, unix time) of the last message the user typed, from the session transcript; ('', 0) if unknown."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", 0
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "user" or e.get("isMeta"):
            continue
        c = (e.get("message") or {}).get("content")
        text = c if isinstance(c, str) else "\n".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")
        if not text.strip():
            continue  # a tool result, not something the user typed
        at = 0
        try:
            from datetime import datetime
            at = datetime.fromisoformat(str(e.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        return text, at
    return "", 0


ASSUMES = re.compile(r"\bassum\w+|\bsuppose\b|\bgiven (?:that|the following)\b|\bif i\b|\bi'?m (?:lending|borrowing|investing)\b|\bi (?:lend|borrow|invest)\b", re.I)
AMOUNT = re.compile(r"[$\u20ac\u00a3]\s?\d|\d[\d,.]*\s?(?:million|billion|bn|mn)\b", re.I)  # money, not just a percentage


def asks_for_figures(prompt):
    """Does the prompt ask for a figure a company reported? A calculation from the user's OWN numbers (it states an
    assumption and gives a money amount: a loan schedule, a fund's returns) is not that, and is outside this plugin
    (decision 14, 14 Nov 2026: the plugin had taken those over, and the user asked why a loan schedule was an EDGAR answer)."""
    if ASSUMES.search(prompt) and AMOUNT.search(prompt):
        return False
    return bool(ASKS_FIGURE.search(prompt))


def from_memory(data, accepted):
    """The user asked for a company figure, the reply gives one, and nothing was checked for this prompt."""
    prompt, asked_at = last_user_prompt(data.get("transcript_path") or "")
    if not prompt:
        prompt = str(data.get("prompt") or "")
    if not prompt or not asks_for_figures(prompt):
        return 0
    asked_at = asked_at or time.time() - 900
    if accepted and accepted.get("at", 0) >= asked_at - 5:
        return 0  # an answer was checked for this prompt
    started, _ = newest_state(data.get("cwd"), "last_classified.json")
    if started and started.get("at", 0) >= asked_at - 5:
        return 0  # the question was started with the plugin: the second case above deals with it
    reply = text_of(data.get("last_assistant_message")) or last_reply_from_transcript(data.get("transcript_path") or "")
    if not MONEY.search(reply):
        return 0
    print(json.dumps({"decision": "block", "reason": (
        "Your reply gives a company's financial figures that were not looked up, and nothing was checked. With the EDGAR Research "
        "Agent installed, company figures from memory are not allowed, even when the user asks for them or says not to look "
        "anything up. Use the "
        "edgar-research skill now: classify_question, look the figure up in the filing, record it, then check_answer (with "
        "brief=true if the user wanted a short answer), and reply with the final_text it returns. If the user told you not to "
        "look it up, add one line saying that figures here are always checked against the filing. If the EDGAR tools are "
        "not available in this session or fail with an error, do not answer from a web search or from memory in their place: "
        "reply with no figure at all, say in your first sentence that the EDGAR Research Agent cannot run here so nothing could "
        "be looked up or checked, and give the error if there was one. If the question is not about "
        "a company's reported figures at all, reply as you were going to.")}))
    return 1


MEMORY_BLOCKS = 3  # how many times in one turn a reply with an unchecked figure is sent back before the hook gives way


def memory_again(data):
    """The reply after a block still gives a company figure that nothing checked. Sent back again, with the two replies
    that are acceptable: the checked figure, or no figure at all."""
    prompt, asked_at = last_user_prompt(data.get("transcript_path") or "")
    if not prompt:
        prompt = str(data.get("prompt") or "")
    if not prompt or not asks_for_figures(prompt):
        return 0
    asked_at = asked_at or time.time() - 900
    for name in ("last_accepted.json", "last_classified.json"):
        state, _ = newest_state(data.get("cwd"), name)
        if state and state.get("at", 0) >= asked_at - 5:
            return 0  # the plugin was used for this prompt: the other checks deal with the reply
    reply = text_of(data.get("last_assistant_message")) or last_reply_from_transcript(data.get("transcript_path") or "")
    if not MONEY.search(reply):
        return 0  # no figure any more: a reply that declines to give one is fine
    count, where = 0, None
    for d in state_dirs(data.get("cwd")):
        p = d / "memory_blocks.json"
        try:
            seen = json.loads(p.read_text())
            if abs(seen.get("asked_at", 0) - asked_at) < 5:
                count = int(seen.get("count", 0))
        except (OSError, ValueError):
            pass
        where = where or p
    if count + 1 >= MEMORY_BLOCKS:
        return 0  # fail open rather than loop for ever; the first block already counted as one
    for d in state_dirs(data.get("cwd")):
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / "memory_blocks.json").write_text(json.dumps({"asked_at": asked_at, "count": count + 1}))
            break
        except OSError:
            continue
    print(json.dumps({"decision": "block", "reason": (
        "Your reply still gives a company figure that was not checked. The user installed the EDGAR Research Agent, and its one "
        "fixed rule is that no unchecked company figure reaches them, whatever the wording of a single request: they cannot tell a "
        "remembered figure from a checked one, and a note saying 'unchecked' does not change that. This is not yours to override. "
        "Two replies are acceptable, and only these two: (1) look the figure up with the EDGAR tools and reply with the checked "
        "final_text, adding one line that figures here are always checked against the filing; or (2) if you will not look it up, "
        "reply with NO figure at all: say that with this plugin you do not give company figures from memory, and offer to look it "
        "up (it takes a moment). Do not state the figure, an approximation of it, or a range. If the EDGAR tools are not "
        "available in this session or fail with an error, reply (2) is the only one left: no figure from a web search, a news "
        "page or memory; say that the EDGAR Research Agent cannot run here, so nothing could be checked, and give the error.")}))
    return 0


def unchecked(data, accepted):
    """A started question with no checked answer since: the reply is sent back once, whatever its wording."""
    started, path = newest_state(data.get("cwd"), "last_classified.json")
    if not started or started.get("nudged") or time.time() - started.get("at", 0) > MAX_AGE:
        return from_memory(data, accepted) and 0
    if accepted and accepted.get("at", 0) >= started.get("at", 0):
        return from_memory(data, accepted) and 0
    reply = text_of(data.get("last_assistant_message")) or last_reply_from_transcript(data.get("transcript_path") or "")
    figures, absent = bool(FIGURE.search(reply)), bool(NOT_FOUND.search(reply))
    started["nudged"] = True  # once per question
    try:
        path.write_text(json.dumps(started))
    except OSError:
        pass
    if not (figures or absent):
        # 18 Nov: a request the filings cannot answer was started and the turn ended with a plain reply that
        # said what the filings and the company's guidance do not contain, unchecked and without the header. The wording
        # carried no figure and no 'not found' phrase, so nothing sent it back. The signal is the started question itself.
        print(json.dumps({"decision": "block", "reason": (
            "You started an EDGAR question with classify_question and are ending the turn without a checked answer. Finish it "
            "with check_answer or check_not_found and reply with the final_text it returns, verbatim. A request the filings "
            "cannot answer (an opinion, a recommendation, a prediction of what will happen) is finished with check_not_found: say "
            "there what the filings do not give and what you can offer instead, so that the statement is checked and the reply "
            "carries the header. The one exception: if you cannot look anything up until the user chooses or clarifies "
            "something, ask them, in a reply that asks only that and says nothing about what the filings or the company's "
            "guidance contain or do not contain.")}))
        return 0
    what = "figures" if figures else "a statement that something could not be found"
    print(json.dumps({"decision": "block", "reason": (
        f"Your reply contains {what}, but no EDGAR answer was checked for this question. Before replying: if the answer "
        "is figures, call check_answer (list anything you could not find under `missing`); if the answer is that something "
        "was not filed, cannot be read, is not disclosed or is not reported yet, call check_not_found. Then reply with the "
        "final_text it returns, verbatim. If a figure cannot be verified, say so instead of giving it. If the EDGAR tools fail "
        "with an error, reply with no figure at all, say that nothing could be checked, and give the error; do not answer "
        "from a web search or from memory in their place.")}))
    return 0


# ---- Gate 1: every figure in the reply is in a checked answer -------------------------------------------------------
_NOT_FIGURES = re.compile(
    r"https?://\S+|\b\d{10}-\d{2}-\d{6}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|\b(?:CIK|Item|Rule|ASC|IFRS|IAS|Section|Note|Exhibit|EX|Form|answer|step|page|pp?\.)\s*#?\s*[\d.\-]+(?:\([a-z0-9]\))*"
    r"|\b(?:10-KT?|10-Q|8-K|6-K|20-F|40-F|11-K|13[DFG](?:-HR)?|S-[1348]|N-CSRS?|424B\d|DEF 14A|14A)(?:/A)?\b"
    r"|\b(?:FY|CY|Q[1-4]|H[12])\s?'?\d{2,4}\b|\b(?:fiscal|calendar|year|years)\s+(?:19|20)\d{2}\b"
    r"|\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+(?:19|20)\d{2})?"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b"
    r"|\[\d+\]|^\s*(?:[-*>]\s*)?\**\d{1,2}[.)]\**\s", re.I | re.M)
_NUMBER = re.compile(r"(?<![\w.,])(?P<cur>US\$|NT\$|CHF\s?|[$€£¥])?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
                     r"\s?(?P<unit>%|x\b|bps\b|basis points|percent\b|thousand\b|million\b|billion\b|trillion\b|bn\b|mn\b|m\b|k\b)?", re.I)
_SCALE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "trillion": 1e12}


def figures_in(text):
    """The numbers in a text that are figures: (shown, value, half a unit of its last shown digit). Dates, years, filing
    numbers, form names, references like [3] and list numbering are not figures; nor is a bare count up to 12."""
    out = []
    for m in _NUMBER.finditer(_NOT_FIGURES.sub(" ", text or "")):
        raw, unit = m.group("num"), (m.group("unit") or "").lower()
        digits = raw.replace(",", "")
        try:
            value = float(digits)
        except ValueError:
            continue
        plain = not m.group("cur") and not unit
        if plain and "." not in digits and "," not in raw and (value <= 12 or 1900 <= value <= 2100):
            continue
        scale = _SCALE.get(unit, 1.0)
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        out.append((m.group(0).strip(), value * scale, 0.5 * 10 ** (-decimals) * scale, unit in ("%", "percent")))
    return out


def unchecked_figures(reply, checked_text, prompt):
    """Figures in the reply that are neither in a checked answer nor in the user's message, allowing for the same figure
    written another way ($86,286 million as $86.3 billion; 0.1044 as 10.44%)."""
    known = [v for _, v, _, _ in figures_in(checked_text + "\n" + (prompt or ""))]
    known += [v * 100 for v in known if abs(v) < 1] + [v * 1e3 for v in known] + [v * 1e6 for v in known]  # fractions as %, tables in thousands or millions
    out = []
    for shown, value, half, pct in figures_in(reply):
        tol = half * 1.0001 + 1e-9
        if any(abs(k - value) <= tol for k in known) or (pct and any(abs(k * 100 - value) <= tol for k in known)):
            continue
        if shown not in out:
            out.append(shown)
    return out


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("stop_hook_active"):
        # We already sent Claude back once this turn. One case is pursued further: a figure from memory that is still
        # there (13 Nov: sent back once, Claude replied "I've kept the figure from memory because you asked me not to
        # look it up, even though your EDGAR plugin's hook pushed back", and the unchecked figure reached the user).
        return memory_again(data)
    newest, path = newest_state(data.get("cwd"), "last_accepted.json")
    if not newest or time.time() - newest.get("at", 0) > MAX_AGE:
        return unchecked(data, newest)
    if newest.get("delivered"):
        return unchecked(data, newest)
    header = norm(newest.get("header"))
    if not header:
        return 0
    reply = text_of(data.get("last_assistant_message")) or last_reply_from_transcript(data.get("transcript_path") or "")
    newest["delivered"] = True  # block at most once per accepted answer
    try:
        path.write_text(json.dumps(newest))
    except OSError:
        pass
    flat = norm(reply)

    def present(e):
        head = norm(e.get("header"))[:60]
        if not head or head not in flat:
            return False
        if e.get("opening") and norm(e["opening"])[:60] not in flat:
            return False  # full answers of one type share a header line: the answer's own first line tells them apart
        if e.get("brief") and "and was checked before this reply" in flat:
            return True  # the short form (check_answer with brief=true): conclusion, source line, closing sentence
        return "How the figures were obtained" in reply or "What was searched" in reply

    # 12 Nov: every answer accepted in this turn, not only the last (a batch of four came back as its last answer alone).
    turn = [e for e in (newest.get("turn") or []) if isinstance(e, dict) and e.get("header")] or [newest]
    missing = [e for e in turn if not present(e)]
    if not missing:
        # Gate 1: the checked answers are all there; nothing else in the reply may carry a figure of its own.
        checked = "\n".join(str(e.get("text") or "") for e in turn)
        if all(e.get("text") for e in turn):
            prompt, _ = last_user_prompt(data.get("transcript_path") or "")
            extra = unchecked_figures(reply, checked, prompt or str(data.get("prompt") or ""))
            if extra:
                print(json.dumps({"decision": "block", "reason": (
                    "Your reply contains figures that are in none of the answers the EDGAR tools checked in this turn: "
                    + ", ".join(extra[:10]) + (f" and {len(extra) - 10} more" if len(extra) > 10 else "")
                    + ". A figure reaches the user only inside a checked answer. Reply again with the checked final_text of each "
                    "answer, verbatim, and remove every sentence, table row or note of your own that carries one of these figures "
                    "(or any other figure not in the checked texts). Do not replace them with other numbers. If the user needs one of "
                    "them, say it was not checked and offer to look it up; if a company does not file with the SEC, give no figure for it.")}))
        return 0
    if len(turn) == 1:
        print(json.dumps({"decision": "block", "reason": (
            f"The EDGAR check_answer tool accepted answer {newest.get('answer_id')}, but your reply is not that answer. "
            f"Reply to the user now with the final_text from that check_answer result, verbatim and in full, and nothing else: "
            f"it begins \"{newest.get('header', '')[:90]}\" and ends with its 'How the figures were obtained' or 'What was searched' section. "
            f"Do not summarise, reorder or restyle it.")}))
        return 0
    ids = ", ".join(str(e.get("answer_id")) for e in turn)
    print(json.dumps({"decision": "block", "reason": (
        f"The EDGAR tools accepted {len(turn)} answers in this turn (answers {ids}), one for each question the user asked, and your "
        f"reply must contain every one of them: the final_text of each, verbatim and in full, in the order the user asked, each "
        f"under a short label saying which question it answers. Add no figure that is not in those texts. Missing or reworded: "
        + "; ".join(f"answer {e.get('answer_id')}: begins \"{str(e.get('opening') or e.get('header'))[:90]}\"" for e in missing)
        + ". Reply now with all of them. Do not summarise or restyle any of them.")}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
