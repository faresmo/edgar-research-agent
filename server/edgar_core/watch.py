"""Company watchlist: which watched companies have filed something new since the last check.

Idea taken from Kestra's EDGAR trigger (kestra-io/plugin-edgar, Apache-2.0; reimplemented here, no code copied):
decide "new" by accession number, not by time, so filings accepted in the same second are neither skipped nor
repeated; keep a 7-day window so a filing SEC publishes late, with an earlier acceptance time, is still caught; the
first check records what is already there and reports nothing unless a lookback is asked for; a company whose
check fails keeps its old state and is checked again next time, without affecting the others.

An MCP server only runs when Claude calls it, so the "watching" happens when watch_check is called: by the user,
or by a scheduled task that asks Claude to check the watchlist (for example every weekday morning).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .client import EdgarClient
from .companies import format_cik, ticker_to_cik

LATE_FILING_WINDOW = timedelta(days=7)
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/{doc}"


def state_path() -> Path:
    return Path(os.environ.get("EDGAR_WATCH_FILE") or Path.home() / ".edgar_agent" / "watchlist.json")


def _load() -> Dict:
    p = state_path()
    if not p.exists():
        return {"companies": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def _save(data: Dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _accepted(row: Dict) -> datetime:
    raw = row.get("acceptanceDateTime") or ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromisoformat(row["filingDate"]).replace(tzinfo=timezone.utc)


def _recent(cik10: str, forms: List[str], client: EdgarClient) -> List[Dict]:
    data = json.loads(client.get_text(SUBMISSIONS_URL.format(cik=cik10), ttl=60))  # fresh: a watch must not read a cached list
    page = data.get("filings", {}).get("recent", {})
    wanted = {f.upper() for f in forms}
    rows = []
    for i, form in enumerate(page.get("form", [])):
        if wanted and form.upper() not in wanted:
            continue
        get = lambda k: (page.get(k) or [""] * (i + 1))[i] if i < len(page.get(k) or []) else ""
        acc = get("accessionNumber")
        rows.append({"form": form, "accession_number": acc, "filing_date": get("filingDate"), "report_date": get("reportDate"),
                     "acceptanceDateTime": get("acceptanceDateTime"), "items": get("items"), "description": get("primaryDocDescription"),
                     "url": ARCHIVES_URL.format(cik_int=int(cik10), nodash=acc.replace("-", ""), doc=get("primaryDocument"))})
    return rows


def evaluate(state: Optional[Dict], recent: List[Dict], now: datetime, lookback: Optional[timedelta]):
    """(new filings, new state). Pure, so it is unit-tested without the network."""
    window_start = now - LATE_FILING_WINDOW
    if state is None:
        fire_from = now - lookback if lookback else None
        since = min(fire_from, window_start) if fire_from else window_start
        in_window = [r for r in recent if _accepted(r) >= since]
        new = [r for r in in_window if fire_from and _accepted(r) >= fire_from]
        return sorted(new, key=_accepted), {"since": since.isoformat(), "seen": sorted(r["accession_number"] for r in in_window)}
    since = datetime.fromisoformat(state["since"])
    seen = set(state.get("seen") or [])
    in_window = [r for r in recent if _accepted(r) >= since]
    new = [r for r in in_window if r["accession_number"] not in seen]
    since = max(since, window_start)
    keep = sorted(r["accession_number"] for r in in_window if _accepted(r) >= since)
    return sorted(new, key=_accepted), {"since": since.isoformat(), "seen": keep}


def add(companies: List[str], forms: Optional[List[str]] = None, client: Optional[EdgarClient] = None) -> Dict:
    client = client or EdgarClient()
    data = _load()
    added, problems = [], []
    for c in companies:
        c = str(c).strip()
        try:
            cik10 = format_cik(c) if c.isdigit() else format_cik(ticker_to_cik(c, client=client))
        except Exception as exc:  # noqa: BLE001
            problems.append({"company": c, "reason": str(exc)})
            continue
        entry = data["companies"].setdefault(cik10, {"label": c.upper(), "forms": [], "state": None})
        entry["forms"] = sorted({f.upper() for f in (forms or [])})
        added.append({"cik": cik10, "label": entry["label"], "forms": entry["forms"] or "all forms"})
    _save(data)
    return {"watching": added, "problems": problems or None, "file": str(state_path()),
            "note": "The first watch_check records what is already filed and reports nothing new, unless lookback_days is given."}


def remove(companies: List[str]) -> Dict:
    data = _load()
    gone = []
    for c in companies:
        c = str(c).strip().upper()
        for cik10, e in list(data["companies"].items()):
            if c in (cik10, cik10.lstrip("0"), e.get("label", "").upper()):
                gone.append(e.get("label")); del data["companies"][cik10]
    _save(data)
    return {"removed": gone, "remaining": [e["label"] for e in data["companies"].values()]}


def listing() -> Dict:
    data = _load()
    return {"companies": [{"cik": k, "label": e["label"], "forms": e["forms"] or "all forms",
                           "last_checked": (e.get("last_checked") or "never")} for k, e in data["companies"].items()],
            "file": str(state_path())}


def check(lookback_days: Optional[int] = None, client: Optional[EdgarClient] = None, now: Optional[datetime] = None) -> Dict:
    client = client or EdgarClient()
    now = now or _now()
    data = _load()
    found, failed = [], []
    for cik10, e in data["companies"].items():
        try:
            recent = _recent(cik10, e.get("forms") or [], client)
            lookback = timedelta(days=int(lookback_days)) if lookback_days and e.get("state") is None else None
            new, state = evaluate(e.get("state"), recent, now, lookback)
        except Exception as exc:  # noqa: BLE001  (state untouched: checked again next time)
            failed.append({"company": e["label"], "reason": str(exc)[:200]})
            continue
        e["state"], e["last_checked"] = state, now.isoformat()
        for r in new:
            r = {k: v for k, v in r.items() if v}
            r["company"], r["cik"] = e["label"], cik10
            found.append(r)
    _save(data)
    found.sort(key=_accepted)
    return {"new_filings": found, "count": len(found), "checked": len(data["companies"]) - len(failed), "failed": failed or None,
            "note": "Each filing is reported once. Read any of them with the document tools; answer questions about them through "
                    "classify_question and check_answer as usual."}
