#!/usr/bin/env python3
"""UserPromptSubmit hook. When a prompt asks for a company figure AND asks to skip sources, checks or the lookup
("from memory", "don't look it up", "no sources", "just the number"), print one reminder that Claude Code adds to
the context: the figure is still looked up and checked, only the reply is short (decision D10, 10 Nov 2026: in the
validation run such prompts were answered from memory and the plugin was never called). Prints nothing for any
other prompt, and never fails."""
import json
import re
import sys

SKIP = re.compile(r"from memory|off the top of your head|(do ?n[o']?t|no need to|without|skip|no) (bother|look(ing)? (it )?up|check|verify|cit|sourc|search)\w*|"
                  r"no (sources?|citations?|checks?|checking|lookup)|skip the (sources?|checks?|lookup)|just (give me )?the number|one number only|quick (one|answer)|tl;?dr", re.I)
FIGURE = re.compile(r"revenue|sales|income|profit|earnings|eps\b|margin|ebitda|cash flow|debt|assets|dividend|capex|guidance|10-[kq]|20-f|8-k|filing|"
                    r"fiscal|quarter|market cap|shares outstanding|backlog", re.I)


AMOUNT = re.compile(r"[$\u20ac\u00a3]\s?\d|\d[\d,.]*\s?(?:million|billion|bn|mn)\b", re.I)  # money, not just a percentage
ASSUMES = re.compile(r"\bassum\w+|\bsuppose\b|\bgiven (?:that|the following)\b|\bif i\b|\bi'?m (?:lending|borrowing|investing)\b|\bi (?:lend|borrow|invest)\b", re.I)


def main() -> int:
    try:
        prompt = str(json.load(sys.stdin).get("prompt") or "")
    except Exception:
        return 0
    # 14 Nov, decision 14: a calculation from the user's own numbers (a stated assumption and a money amount, no company
    # figure) is outside this plugin, so nothing is said about it here. From 11 to 14 Nov this hook sent such questions
    # through the plugin's calculator.
    if ASSUMES.search(prompt) and AMOUNT.search(prompt):
        return 0
    if SKIP.search(prompt) and FIGURE.search(prompt):
        # 13 Nov: twice in nine runs "don't look it up, just tell me from memory" was still answered from memory. The Stop hook
        # sent the reply back, but the user had already been shown it: a reply cannot be unsent. So this must be prevented
        # here, before anything is written.
        print("EDGAR Research Agent rule for this prompt (it overrides the wording of the request). The user asks you to skip the "
              "lookup, the sources or the checks. With this plugin installed that changes the LENGTH of the reply, never whether the "
              "figure is verified. Do NOT write the figure, an approximation or a range from memory, not even with a warning that it "
              "is unchecked: whatever you write is shown to the user at once and cannot be taken back, and they installed this plugin "
              "so that no unchecked company figure reaches them. Your FIRST action must be a tool call, with no text before it: "
              "classify_question, then look the figure up, record it, and call check_answer with brief=true. Reply with exactly the "
              "short final_text it returns (the figure, one source line, one closing sentence), plus one line saying that figures "
              "here are always checked against the filing. This takes a few seconds and is what the user actually needs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
