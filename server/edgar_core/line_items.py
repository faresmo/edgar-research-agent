"""Every line item a filing reports, from the filing's own XBRL instance.

The SEC's company-facts API (get_financial_fact) carries only standard us-gaap/dei/ifrs tags. A company's own lines
("Transaction expenses", "Transaction-based revenue", "Customer-funded R&D") and its revenue and segment breakdowns are
extension tags or dimensional facts that live only in the filing's instance document. On 6-7 Nov the plugin answered
"net revenue" and "transaction revenue" questions from the headline revenue because the lines it needed were not
offered to it; this lists them.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from .client import EdgarClient
from .companies import format_cik

STANDARD = {"us-gaap": "us-gaap", "dei": "dei", "srt": "srt", "ifrs-full": "ifrs", "country": "country", "currency": "currency"}
_SKIP_SUFFIX = ("TextBlock", "Policy", "PolicyTextBlock", "Abstract", "Member", "Axis", "Domain", "Table", "LineItems")


def _humanize(local: str) -> str:
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", local).split()
    return " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in words)


def _taxonomy(ns_uri: str) -> str:
    u = ns_uri.lower()
    for key, name in STANDARD.items():
        if f"/{key}/" in u or u.endswith(f"/{key}") or f"xbrl.{key}" in u or (key == "ifrs-full" and "ifrs" in u):
            return name
    return "company"


def instance_url(cik, accession: str, client: EdgarClient) -> Optional[str]:
    cik_int = int(format_cik(cik))
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession.replace('-', '')}/"
    index = client.get_json(folder + "index.json")
    names = [i.get("name", "") for i in index.get("directory", {}).get("item", [])]
    for pick in (lambda n: n.endswith("_htm.xml"), lambda n: n.endswith("_ins.xml"),
                 lambda n: n.endswith(".xml") and not re.search(r"(FilingSummary|primary_doc|_cal|_def|_lab|_pre|MetaLinks)", n, re.I)):
        hits = [n for n in names if pick(n)]
        if hits:
            return folder + hits[0]
    return None


def line_items(cik, accession: str, period_end: Optional[str] = None, search: Optional[str] = None,
               breakdowns: bool = False, client: Optional[EdgarClient] = None, limit: int = 150) -> Dict:
    """Numeric facts in one filing: concept, readable label, taxonomy ('company' = the company's own line), value,
    unit, period and any breakdown (axis = member). `period_end` keeps facts ending (or dated) on that day; `search`
    keeps facts whose label, concept or breakdown contains the words; breakdowns are included when asked or searched."""
    client = client or EdgarClient()
    url = instance_url(cik, accession, client)
    if not url:
        return {"error": "this filing has no XBRL instance (older filings and some forms have none): read its tables instead"}
    root = ET.fromstring(client.get_text(url, ttl=None).encode("utf-8"))
    contexts: Dict[str, dict] = {}
    units: Dict[str, str] = {}
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "context":
            cid = el.get("id")
            start = end = instant = None
            dims = []
            for sub in el.iter():
                st = sub.tag.split("}")[-1]
                if st == "startDate":
                    start = (sub.text or "").strip()
                elif st == "endDate":
                    end = (sub.text or "").strip()
                elif st == "instant":
                    instant = (sub.text or "").strip()
                elif st == "explicitMember":
                    dims.append(f"{_humanize(sub.get('dimension', '').split(':')[-1])} = {_humanize((sub.text or '').split(':')[-1])}")
                elif st == "typedMember":
                    dims.append(f"{_humanize(sub.get('dimension', '').split(':')[-1])} = {''.join(sub.itertext()).strip()}")
            contexts[cid] = {"start": start, "end": end or instant, "instant": bool(instant), "dims": dims}
        elif tag == "unit":
            measures = [(m.text or "").split(":")[-1] for m in el.iter() if m.tag.split("}")[-1] == "measure"]
            units[el.get("id")] = "/".join(measures)
    words = [w for w in re.split(r"\s+", (search or "").lower()) if w]
    rows = []
    for el in root:
        cref = el.get("contextRef")
        if not cref or el.text is None:
            continue
        local = el.tag.split("}")[-1]
        if local.endswith(_SKIP_SUFFIX):
            continue
        try:
            value = float(el.text.strip())
        except ValueError:
            continue
        ctx = contexts.get(cref, {})
        if period_end and ctx.get("end") != period_end:
            continue
        if ctx.get("dims") and not (breakdowns or words):
            continue
        ns = el.tag[1:].split("}")[0] if el.tag.startswith("{") else ""
        row = {"concept": local, "label": _humanize(local), "taxonomy": _taxonomy(ns), "value": value,
               "unit": units.get(el.get("unitRef", ""), ""),
               "period": (f"{ctx['start']} to {ctx['end']}" if ctx.get("start") else f"at {ctx.get('end')}"),
               "breakdown": "; ".join(ctx.get("dims") or []) or None}
        hay = " ".join(str(v) for v in (row["label"], row["concept"], row["breakdown"] or "")).lower()
        if words and not all(w in hay for w in words):
            continue
        rows.append(row)
    seen, out = set(), []
    for r in rows:
        key = (r["concept"], r["period"], r["breakdown"], r["value"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    out.sort(key=lambda r: (r["breakdown"] is not None, r["taxonomy"] != "company", r["label"]))
    return {"accession_number": accession, "instance": url, "count": len(out), "items": out[:limit],
            "truncated": len(out) > limit,
            "note": "taxonomy 'company' = a line the company defined itself (often the one a qualified question needs). "
                    "Record a figure you use with notes_add(source_type='filing', accession_number, value, label naming the line)."}
