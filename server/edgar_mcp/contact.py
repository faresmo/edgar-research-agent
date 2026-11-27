"""The contact the SEC asks for on every request to EDGAR.

The SEC's fair-access rule for EDGAR ("Accessing EDGAR Data", sec.gov) says: "Please declare your user agent in request
headers", in the form "Sample Company Name AdminContact@<sample company domain>.com". Publishing decision 2 (12 Nov
2026): each user gives their own name and email; the publisher's is never built in. The contact is saved on the user's
computer and is sent to the SEC only, as the User-Agent of the requests this plugin makes for that user.

Where it comes from, in order: the plugin's setting (Claude Code asks for it at install), the saved file, the
set_sec_contact tool (Cowork does not show the settings dialog, so Claude asks in the conversation on first use).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Optional

FILE = "sec_contact.json"
ASK = ("Before the first question can be answered, the SEC needs to know who is asking. The SEC's rule for EDGAR, its public "
       "filings database, is that every request declares a contact: a name and an email address (sec.gov, 'Accessing EDGAR "
       "Data'). Tell the user this in one or two sentences, saying that it is the SEC's requirement, that the contact is saved "
       "on their computer and sent to the SEC only, and that the publisher of this plugin never receives it. Ask for the name "
       "(their own or their organisation's) and the email to use. If you already know the user's name and email, you may offer "
       "them, but use them only after the user says yes. Then call set_sec_contact and continue with the question.")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$")
_PLACEHOLDER = re.compile(r"example\.(com|org|net)$|^(test|none|na|n/a|user|name)@|@(test|localhost)", re.I)


def _places() -> List[Path]:
    out = []
    for d in (os.environ.get("EDGAR_STATE_DIR"), os.environ.get("CLAUDE_PLUGIN_DATA")):
        if d and not d.startswith("${"):
            out.append(Path(d))
    out.append(Path.home() / ".edgar-agent")
    return out


def clean(value: Optional[str]) -> str:
    """A setting that was never filled in arrives empty or as the literal '${...}' text."""
    v = (value or "").strip()
    return "" if not v or (v.startswith("${") and v.endswith("}")) else v


def check(name: str, email: str) -> Optional[str]:
    """None if the contact is usable, else what is wrong with it."""
    name, email = (name or "").strip(), (email or "").strip()
    if len(name) < 2 or "@" in name or len(name) > 80 or not re.search(r"[A-Za-z]", name):
        return "name: give the person's or organisation's name (letters, 2 to 80 characters, not an email address)"
    if not _EMAIL.match(email) or len(email) > 120:
        return "email: this is not an email address"
    if _PLACEHOLDER.search(email):
        return "email: this looks like a placeholder; the SEC asks for a real contact"
    return None


def user_agent(name: str, email: str) -> str:
    return f"{name.strip()} {email.strip()}"


def load() -> str:
    """The saved contact as a User-Agent string, or ''."""
    for d in _places():
        try:
            c = json.loads((d / FILE).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(c, dict) and not check(str(c.get("name", "")), str(c.get("email", ""))):
            return user_agent(c["name"], c["email"])
    return ""


def save(name: str, email: str) -> Optional[Path]:
    for d in _places():
        try:
            d.mkdir(parents=True, exist_ok=True)
            p = d / FILE
            p.write_text(json.dumps({"name": name.strip(), "email": email.strip()}))
            try:
                p.chmod(0o600)
            except OSError:
                pass
            return p
        except OSError:
            continue
    return None


def from_settings(environ=os.environ) -> str:
    """The contact from the plugin's settings: one 'Name email' string, or a name and an email given separately."""
    whole = clean(environ.get("EDGAR_USER_AGENT"))
    if whole:
        return whole
    name, email = clean(environ.get("EDGAR_CONTACT_NAME")), clean(environ.get("EDGAR_CONTACT_EMAIL"))
    return user_agent(name, email) if name and email and not check(name, email) else ""
