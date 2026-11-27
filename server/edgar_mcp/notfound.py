"""check_not_found: an answer that something could not be found is checked like a figure.

Until 5 Nov a "not found" answer was free text: no tool verified it, it carried no header or appendix, and
the Stop hook let it through (it only acts after check_answer accepted an answer). A wrong "never filed" is
as harmful as a wrong number and harder to spot. Each status is verified as far as the server can:

  not_filed         the company's full EDGAR filing history (older pages included) has none of the forms
  not_public        EDGAR full-text search lists no public filing of the form type at all (forms filed confidentially)
  not_readable      the tool log shows a failed read of that filing, or the filing's documents are images/PDF
  not_in_filing     the filing was opened in this session (what was searched is listed for the reader)
  not_yet_reported  the period has not ended, or no periodic filing covers it yet
  undetermined      what was tried is listed; nothing is claimed beyond that
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List, Tuple

import edgar_core as core

STATUSES = ("not_sec_filer", "not_filed", "not_public", "not_readable", "not_in_filing", "not_yet_reported", "undetermined")

LABELS = {
    "not_sec_filer": "Outside this plugin's scope: the company does not file with the SEC",
    "not_filed": "Not filed",
    "not_public": "Not public on EDGAR",
    "not_readable": "Filed, but not readable by these tools",
    "not_in_filing": "Not disclosed in the filing examined",
    "not_yet_reported": "Not reported yet",
    "undetermined": "Could not be determined",
}

PERIODIC = ["10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F", "6-K", "N-CSR", "N-CSRS", "NPORT-P", "10-KT", "10-QT"]
_UNREADABLE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".pdf", ".tif", ".tiff")


def _digits(accession: str) -> str:
    return re.sub(r"\D", "", accession or "")


def _mentions(call: Dict[str, Any], accession: str, url: str) -> bool:
    text = str(call.get("args") or "")
    d = _digits(accession)
    return bool((d and (d in re.sub(r"\D", "", text) or accession in text)) or (url and url in text))


def _cik_from(a: Dict[str, Any]) -> str:
    cik = str(a.get("cik") or "").strip()
    if not cik:
        m = re.search(r"/edgar/data/(\d+)/", str(a.get("url") or ""))
        cik = m.group(1) if m else ""
    return re.sub(r"\D", "", cik)


def _amendment_forms(forms: List[str]) -> List[str]:
    out = []
    for f in forms:
        f = f.strip().upper()
        out += [f, f + "/A"] if not f.endswith("/A") else [f]
    return out


def verify(a: Dict[str, Any], ctx, client) -> Tuple[str, List[dict]]:
    """Returns (what the server confirmed, problems). Problems mean the claim is refused."""
    status = str(a.get("status") or "")
    searched = [str(s) for s in (a.get("searched") or []) if str(s).strip()]
    if status not in STATUSES:
        return "", [{"field": "status", "reason": f"must be one of {', '.join(STATUSES)}"}]
    if not searched and status != "not_sec_filer":
        return "", [{"field": "searched", "reason": "list what you looked at (filings, forms, sections, search terms), one item each"}]
    if not ctx.tool_calls and status != "not_sec_filer":
        return "", [{"reason": "no EDGAR tool was called in this session: look before reporting that something is not there"}]
    as_of = ctx.as_of
    accession, url = str(a.get("accession_number") or ""), str(a.get("url") or "")

    if status == "not_sec_filer":
        oos = getattr(ctx, "out_of_scope", None)
        if oos:
            return oos["reason"] + ".", []
        company = str(a.get("company") or a.get("statement") or "")
        from edgar_core import scope as _scope
        sc = _scope.check(str(a.get("company") or ""), as_of=as_of, client=client) if a.get("company") else {"in_scope": True}
        if not sc.get("in_scope"):
            return sc["reason"] + ".", []
        if a.get("different_company") and sc.get("match") not in (None, "exact"):
            # 12 Nov: the name was let in on a guess ('Mars' -> Marsh & McLennan, 'BYD' -> Boyd Gaming's symbol). Claude
            # says the entity is another company; the server confirms only that the match was not exact.
            matched = " ".join(str(sc.get("name") or "").split())
            return (f"no EDGAR entity carries exactly the name '{a.get('company')}' and files periodic reports; the nearest match, "
                    f"{matched}, is a different company."), []
        return "", [{"reason": "give the company's name in `company`; the server confirms it has no SEC filings" if not a.get("company")
                     else f"{sc.get('reason')}: it is an SEC filer, so answer from its filings"}]

    if status == "not_filed":
        cik, forms = _cik_from(a), [str(f) for f in (a.get("forms") or []) if str(f).strip()]
        if not cik or not forms:
            return "", [{"reason": "not_filed needs the company's cik and the exact form types (forms)"}]
        try:
            history = core.filing_history(cik, as_of=as_of, client=client)
        except Exception as exc:  # noqa: BLE001
            return "", [{"reason": f"could not read the filing history of CIK {cik}: {exc}. Use status undetermined if EDGAR cannot be reached."}]
        wanted = set(_amendment_forms(forms))
        found = [f for f in history if f.form.upper() in wanted]
        if found:
            listed = [{"form": f.form, "filed": f.filing_date, "accession_number": f.accession_number, "url": f.url} for f in found[:5]]
            return "", [{"reason": f"CIK {cik} has filed {len(found)} filing(s) of {', '.join(sorted({f.form for f in found}))} on or before {as_of}",
                         "filings": listed, "fix": "Answer from these filings with check_answer, or say which one the question means."}]
        newest = history[0].filing_date if history else "none"
        return (f"EDGAR's complete filing history for CIK {int(cik)} ({len(history)} filings on or before {as_of}, newest {newest}, "
                f"older pages included) contains no {', '.join(sorted(wanted))}."), []

    if status == "not_public":
        forms = [str(f).strip() for f in (a.get("forms") or []) if str(f).strip()]
        if not forms:
            return "", [{"reason": "not_public needs the exact form types (forms)"}]
        listed, counts = [], {}
        for f in forms:
            url = "https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode({"q": "", "forms": f})
            try:
                data = client.get_json(url)
            except Exception as exc:  # noqa: BLE001
                return "", [{"reason": f"EDGAR full-text search could not be reached ({exc}); use status undetermined"}]
            hits = (data.get("hits") or {})
            total = int(((hits.get("total") or {}).get("value")) or 0)
            exact = [h for h in hits.get("hits") or [] if str((h.get("_source") or {}).get("form") or "").upper() == f.upper()]
            counts[f] = total if exact or total == 0 else 0  # the search can return near matches; count only the exact form
            for h in exact[:3]:
                src = h.get("_source") or {}
                listed.append({"form": src.get("form"), "filed": src.get("file_date"), "accession_number": src.get("adsh"),
                               "filer": "; ".join(src.get("display_names") or [])[:120]})
        public = {f: n for f, n in counts.items() if n}
        if public:
            return "", [{"reason": "EDGAR full-text search lists public filings of " + ", ".join(f"{f} ({n})" for f, n in public.items()),
                         "filings": listed, "fix": "Answer from the latest of these with check_answer."}]
        return (f"EDGAR full-text search (filings from 2001 on) lists no public filing of form type {', '.join(forms)}: filings of "
                "this type, if made, are not disseminated."), []

    if status == "not_readable":
        if not accession and not url:
            return "", [{"reason": "not_readable needs the accession_number (or url) of the filing that could not be read"}]
        failed = [c for c in ctx.tool_calls if not c.get("ok") and _mentions(c, accession, url)]
        if failed:
            return (f"{len(failed)} tool call(s) on this filing failed in this session: "
                    + "; ".join(f"{c['tool']}: {str(c.get('error') or '')[:120]}" for c in failed[:3]) + "."), []
        cik = _cik_from(a)
        if cik and accession:
            try:
                index = client.get_json(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{_digits(accession)}/index.json")
                names = [i.get("name", "") for i in index.get("directory", {}).get("item", [])]
                images = [n for n in names if n.lower().endswith(_UNREADABLE_EXT)]
                if images:
                    return (f"The filing contains {len(images)} image or PDF document(s) (e.g. {', '.join(images[:3])}) that these tools "
                            "cannot read as text."), []
            except Exception:  # noqa: BLE001
                pass
        return "", [{"reason": "the tool log shows no failed read of this filing, and it has no image or PDF documents",
                     "fix": "Read it with find_in_document, get_table or read_document; if a tool returned something wrong, call "
                            "report_tool_problem. If the item is simply not in the text, use status not_in_filing."}]

    if status == "not_in_filing":
        if not accession and not url:
            return "", [{"reason": "not_in_filing needs the accession_number (or url) of the filing examined"}]
        opened = [c for c in ctx.tool_calls if c.get("ok") and _mentions(c, accession, url)]
        if not opened:
            return "", [{"reason": "this filing was not opened in this session", "fix": "Read or search it before saying it does not disclose the item."}]
        return f"The filing was examined with {len(opened)} tool call(s) in this session ({', '.join(sorted({c['tool'] for c in opened}))}).", []

    if status == "not_yet_reported":
        period = str(a.get("period_end") or "").strip()
        if not re.match(r"\d{4}-\d{2}-\d{2}$", period):
            return "", [{"reason": "not_yet_reported needs period_end (YYYY-MM-DD), the end of the period asked about"}]
        if period > as_of:
            return f"The period ends {period}, after the answer date {as_of}.", []
        cik = _cik_from(a)
        if not cik:
            return "", [{"reason": f"the period ended {period}, before {as_of}: give the company's cik so the filings since can be checked"}]
        try:
            history = core.filing_history(cik, as_of=as_of, client=client)
        except Exception as exc:  # noqa: BLE001
            return "", [{"reason": f"could not read the filing history of CIK {cik}: {exc}"}]
        forms = set(_amendment_forms([str(f) for f in (a.get("forms") or [])] or PERIODIC))
        covering = [f for f in history if f.form.upper() in forms and f.report_date and f.report_date >= period]
        if covering:
            f = covering[-1]
            return "", [{"reason": f"{f.form} filed {f.filing_date} ({f.accession_number}) covers a period ending {f.report_date}, on or after {period}",
                         "fix": "Answer from that filing with check_answer."}]
        since = [f for f in history if f.filing_date > period][:1]
        return (f"The period ended {period}; no {', '.join(sorted(forms))} covering it was filed by {as_of}"
                + (f" (latest filing of any kind: {since[0].form} on {since[0].filing_date})." if since else ".")), []

    return f"{len(ctx.tool_calls)} tool call(s) were made in this session; nothing is claimed beyond what is listed.", []


def render(a: Dict[str, Any], confirmed: str, ctx) -> str:
    """The 'What was searched' section."""
    status = str(a["status"])
    lines = ["## What was searched", "", f"**Status: {LABELS[status]}.** {confirmed}", ""]
    lines += [f"- {s}" for s in a.get("searched") or []]
    errors = [c for c in ctx.tool_calls if not c.get("ok")]
    if errors:
        lines += ["", "Tool errors in this session (they may limit what could be checked):"]
        lines += [f"- {c['tool']}: {str(c.get('error') or '')[:150]}" for c in errors[:5]]
    return "\n".join(lines)
