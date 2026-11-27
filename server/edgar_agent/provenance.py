"""How the numbers were obtained, in plain English, for the person reading the answer.

The agent records its work as notes (JSON) and tool calls for its own checks; this
module turns them into a short document a reader can follow without knowing the
internals: every filing figure with its filing, every calculation step with the
formula and the values that went in, market data with its provider, and the
definitions used. Nothing here comes from the model: it is rendered from the record."""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*(#.*)?$")


def _money(value, unit: str = "") -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_money(v, unit) for v in value) + "]"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    u = (unit or "").upper()
    if u in ("USD", "$"):
        return f"${v:,.0f}" if abs(v) >= 1000 else f"${v:,.2f}"
    if abs(v) >= 1000:
        return f"{v:,.0f}" + (f" {unit}" if unit else "")
    if v == int(v):
        return f"{int(v)}" + (f" {unit}" if unit else "")
    return f"{v:,.4f}".rstrip("0").rstrip(".") + (f" {unit}" if unit else "")


def _script_steps(script: str) -> List[tuple]:
    steps = []
    for line in (script or "").splitlines():
        m = _ASSIGN.match(line)
        if m:
            steps.append((m.group(1), m.group(2).strip()))
    return steps


def filing_link(accession: str, filing: Optional[dict] = None, note: Optional[dict] = None) -> str:
    """Where the filing can be opened on sec.gov, or '' if it cannot be worked out. 13 Nov: answers named each filing by
    form, date and accession number, and the reader had to go and find it."""
    filing, note = filing or {}, note or {}
    for url in (str(note.get("url") or ""), str(filing.get("url") or "")):
        if url.startswith("https://www.sec.gov/"):
            return url
    cik = str(filing.get("cik") or note.get("cik") or "") or (re.search(r"CIK\s*0*(\d+)", str(note.get("label") or "")) or [None, ""])[1]
    if cik.strip("0").isdigit() and re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession or ""):
        return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm"
    return ""


def linked(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


def render_working(notes: Iterable[dict], tool_calls: Iterable[dict], filings: Optional[Dict[str, dict]] = None) -> str:
    """Markdown. `filings` may map accession number -> {form, company, period_end, filed} for nicer citations."""
    notes = list(notes)
    filings = filings or {}
    facts = [n for n in notes if n.get("source_type") == "filing"]
    market = [n for n in notes if n.get("source_type") == "web"]
    methods = [n for n in notes if n.get("source_type") == "method"]
    calcs = [c for c in tool_calls if c.get("tool") == "calculate" and c.get("ok")]
    evs = [c for c in tool_calls if c.get("tool") == "build_ev" and c.get("ok")]
    by_id = {n.get("id"): n for n in notes}
    if not facts and not calcs and not market and not evs:
        return ""
    out = ["## How the figures were obtained", ""]

    ref: Dict[float, str] = {}  # value -> "[n]" for citing inputs inside calculations
    if facts:
        out += ["### Figures taken from SEC filings", "", "Numbers in brackets are the references used in the answer.", ""]
        for n in facts:
            i = n.get("id", "?")
            acc = n.get("accession_number") or ""
            f = filings.get(acc, {})
            where = f"{f.get('form', 'filing')}" + (f" of {f['company']}" if f.get("company") else "") + (f" filed {f['filed']}" if f.get("filed") else "")
            cite = f"{linked(where, filing_link(acc, f, n))}, accession {acc}" if acc else "filing not recorded"
            period = f", period ending {n['period_end']}" if n.get("period_end") else ""
            line = f"- [{i}] **{n.get('label', '')}**: {_money(n.get('value'), n.get('unit', ''))}{period}. Source: {cite}."
            if n.get("quote"):
                line += f' The filing says: "{str(n["quote"])[:300]}"'
            elif str(n.get("detail", "")).startswith("tool result"):
                line += " (structured XBRL data from the filing)"
            out.append(line)
            try:
                ref.setdefault(float(n.get("value")), f"[{i}]")
            except (TypeError, ValueError):
                pass
        out.append("")

    if market:
        out += ["### Figures from outside SEC filings (market data and web pages)", ""]
        for n in market:
            src = n.get("url") or n.get("detail") or "web"
            quote = str(n.get("quote") or "").strip()
            out.append(f"- [{n.get('id', '?')}] {n.get('label', '')}: {_money(n.get('value'), n.get('unit', ''))}" + (f" on {n['period_end']}" if n.get("period_end") else "")
                       + f". Source: {src}" + (f". The page says: \"{quote[:200]}\"" if quote and not str(n.get('detail') or '').startswith('price from') else "") + ".")
        out.append("")

    if evs:
        out += ["### Enterprise value, assembled by the code from the balance sheet", ""]
        for c in evs:
            a = c.get("args") or {}
            ids = c.get("note_ids") or {}
            mcap, ev = by_id.get(ids.get("market_cap")), by_id.get(ids.get("enterprise_value"))
            line = f"- {a.get('label') or 'enterprise value'}: price {a.get('price')} x shares"
            if mcap:
                line += f" = market cap {_money(mcap.get('value'))} [{mcap['id']}]"
            if ev:
                line += f"; {ev.get('detail', '')} = enterprise value {_money(ev.get('value'))} [{ev['id']}]"
            out.append(line + ("; operating lease liabilities included" if a.get("include_operating_leases") else ""))
        out.append("")

    if calcs:
        out += ["### Calculations", ""]
        for c in calcs:
            a = c.get("args") or {}
            label = a.get("label") or a.get("expression") or "calculation"
            out.append(f"**{label}**" + (f" (metric: {a['metric']})" if a.get("metric") else ""))
            values = dict(a.get("variables") or {})
            if a.get("script"):
                steps = _script_steps(a["script"])
                ids = c.get("note_ids") or {}
                if ids:  # the notes this very call recorded (two calls with one label are not confused)
                    result_notes = {}
                    for key, nid in ids.items():
                        n = by_id.get(nid if not isinstance(nid, list) else (nid[0] if nid else None))
                        if n:
                            result_notes[key] = n
                else:
                    result_notes = {str(n.get("detail", "")).replace("script step ", ""): n for n in notes
                                    if n.get("source_type") in ("calculation", "web_calculation") and str(n.get("label", "")).startswith(label + ": ")}
                for name, expr in steps:
                    n = result_notes.get(name)
                    val = _money(n.get("value")) if n else "?"
                    cited = ""
                    try:
                        if n is not None and float(n.get("value")) in ref and re.fullmatch(r"[\d_.,e+\-]+", expr.replace(" ", "")):
                            cited = f" (figure {ref[float(n['value'])]} above)"
                    except (TypeError, ValueError):
                        pass
                    out.append(f"- [{n.get('id', '?') if n else '?'}] {name} = {expr} → {val}{cited}")
            else:
                nid = (c.get("note_ids") or {}).get("result")
                n = by_id.get(nid) if nid else next((x for x in notes if x.get("source_type") in ("calculation", "web_calculation") and x.get("detail") == a.get("expression")), None)
                val = _money(n.get("value")) if n else "?"
                out.append(f"- [{n.get('id', '?') if n else '?'}] {a.get('expression')} → {val}" + (f", with {', '.join(f'{k} = {_money(v)}' for k, v in values.items())}" if values else ""))
            out.append("")

    defs = [n for n in methods if str(n.get("label", "")).startswith("definition used")]
    if defs:
        out += ["### Definitions used", ""]
        for n in defs:
            out.append(f"- {n.get('detail', '')}")
        out.append("")
    other = [n for n in methods if n not in defs]
    if other:
        out += ["### Method choices recorded", ""]
        for n in other:
            out.append(f"- {n.get('label', '')}" + (f": {n['detail']}" if n.get("detail") else ""))
        out.append("")
    return "\n".join(out).rstrip() + "\n"
