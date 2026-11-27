"""Analyst conventions the user can review and set (stdlib only).

The catalogue in data/metrics.json defines WHAT a metric is; some metrics leave a choice open (which
filing for an earlier year, which share count, exact or rounded against guidance). Those choices live in
data/conventions.json with our default, the alternatives and the source of each. A user's own settings
are kept in a small JSON file (EDGAR_CONVENTIONS_FILE, default ~/.edgar-agent/conventions.json).

Precedence for one question: a method instruction in the question > the user's setting > the default.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_DATA = Path(__file__).resolve().parent / "data" / "conventions.json"


class ConventionError(ValueError):
    pass


def catalogue() -> List[dict]:
    return json.loads(_DATA.read_text())["conventions"]


def _by_id() -> Dict[str, dict]:
    return {c["id"]: c for c in catalogue()}


def user_file() -> Path:
    return Path(os.environ.get("EDGAR_CONVENTIONS_FILE") or Path.home() / ".edgar-agent" / "conventions.json")


def load_user(path: Optional[Path] = None) -> Dict[str, dict]:
    """{id: {"value", "changed"}} as saved by the user; unknown ids or values are ignored."""
    path = path or user_file()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    known = _by_id()
    out = {}
    for cid, entry in (raw.get("settings") or {}).items():
        value = entry.get("value") if isinstance(entry, dict) else entry
        if cid in known and value in known[cid]["options"]:
            out[cid] = {"value": value, "changed": entry.get("changed", "") if isinstance(entry, dict) else ""}
    return out


def set_user(cid: str, value: str, path: Optional[Path] = None) -> Dict[str, dict]:
    """Save one setting; value 'default' removes the user's setting. Returns the saved settings."""
    known = _by_id()
    if cid not in known:
        raise ConventionError(f"unknown convention {cid!r}; known: {', '.join(known)}")
    path = path or user_file()
    settings = load_user(path)
    if value == "default":
        settings.pop(cid, None)
    elif value in known[cid]["options"]:
        settings[cid] = {"value": value, "changed": time.strftime("%Y-%m-%d")}
    else:
        raise ConventionError(f"{cid}: value must be one of {', '.join(known[cid]['options'])} or 'default'")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"settings": settings}, indent=1))
    return settings


def from_question(question: str) -> Dict[str, str]:
    """Settings the question itself fixes ('as originally reported', 'mid-year convention')."""
    q = (question or "").lower()
    out = {}
    for c in catalogue():
        for value, patterns in (c.get("question_patterns") or {}).items():
            if any(re.search(p, q) for p in patterns):
                out[c["id"]] = value
                break
    return out


def effective(question: str = "", user: Optional[Dict[str, dict]] = None) -> Dict[str, dict]:
    """Every convention with the value in force for this question and where it comes from."""
    user = load_user() if user is None else user
    asked = from_question(question)
    out = {}
    for c in catalogue():
        cid = c["id"]
        if cid in asked:
            value, origin = asked[cid], "question"
        elif cid in user:
            value, origin = user[cid]["value"], "you" + (f" ({user[cid]['changed']})" if user[cid].get("changed") else "")
        else:
            value, origin = c["default"], "default"
        out[cid] = {"id": cid, "label": c["label"], "value": value, "meaning": c["options"][value], "origin": origin,
                    "default": c["default"], "alternatives": {k: v for k, v in c["options"].items() if k != value},
                    "applied": c["applied"], "affects": c["affects"], "source": c["source"]}
    return out


def values(eff: Dict[str, dict]) -> Dict[str, str]:
    return {cid: e["value"] for cid, e in eff.items()}


def changed_by_user(eff: Dict[str, dict]) -> List[str]:
    return [cid for cid, e in eff.items() if e["origin"].startswith("you")]


def render_table(eff: Dict[str, dict]) -> str:
    lines = ["| Convention | In force | Set by | Alternatives | Source of the default |", "|---|---|---|---|---|"]
    for e in eff.values():
        alts = "; ".join(f"`{k}`: {v}" for k, v in e["alternatives"].items())
        lines.append(f"| {e['label']} (`{e['id']}`) | `{e['value']}`: {e['meaning']} | {e['origin']} | {alts} | {e['source']} |")
    return "\n".join(lines)


def prompt_block(eff: Dict[str, dict]) -> str:
    """The rules Claude receives for this question."""
    lines = ["CONVENTIONS IN FORCE (a method instruction in the question has already been applied here; follow these, "
             "and when your answer depends on one, the server lists it under 'Conventions used'). When a convention changes a figure "
             "in your answer, give the figure under its main alternative too, as a labelled row next to it, and explain both "
             "bases and why they differ under 'Why there are two values':"]
    for e in eff.values():
        how = "applied by the tools" if e["applied"] == "code" else "apply it yourself"
        lines.append(f"- {e['label']}: {e['meaning']} [{e['origin']}; {how}]")
    return "\n".join(lines)


_RELEVANT = {  # metric names (lower case) or tool names -> conventions they depend on
    "build_ev": ["share_count", "ev_short_term_investments", "ev_operating_leases"],
    "market capitalization": ["share_count"], "enterprise value": ["share_count", "ev_short_term_investments", "ev_operating_leases"],
    "ev to ebitda": ["share_count", "ev_short_term_investments", "ev_operating_leases"],
    "beat or miss versus guidance": ["guidance_point", "guidance_precision", "guidance_version", "earnings_basis"],
    "guidance midpoint": ["guidance_point", "guidance_version"],
    "net debt": ["net_debt_cash"], "free cash flow": ["fcf_deductions"], "free cash flow margin": ["fcf_deductions"],
    "cash flow conversion": ["cash_conversion_denominator", "fcf_deductions"],
    "discounted cash flow": ["dcf_timing", "ufcf_sbc"], "unlevered free cash flow": ["ufcf_sbc"],
    "acquisition premium": ["transaction_value"], "implied enterprise value": ["transaction_value"],
}


def relevant(eff: Dict[str, dict], metrics: Iterable[str], tools: Iterable[str], several_periods: bool) -> List[str]:
    ids: List[str] = []
    for key in list(tools) + [m.lower() for m in metrics]:
        for cid in _RELEVANT.get(key, []):
            if cid not in ids:
                ids.append(cid)
    if several_periods and "prior_year_figures" not in ids:
        ids.insert(0, "prior_year_figures")
    for cid, e in eff.items():  # a choice the user or the question made is always stated
        if e["origin"] != "default" and cid not in ids:
            ids.append(cid)
    return ids


def answer_section(eff: Dict[str, dict], ids: List[str]) -> str:
    if not ids:
        return ""
    lines = ["**Conventions used**"]
    for cid in ids:
        e = eff[cid]
        alt = "; ".join(e["alternatives"].values())
        lines.append(f"- {e['label']}: {e['meaning']} ({e['origin']}). Alternative: {alt}.")
    return "\n".join(lines)
