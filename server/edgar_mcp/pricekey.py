"""The user's own share-price key (Tiingo), and how they are told to get one.

22 Nov 2026. The plugin has no settings (Cowork does not start a server that refers to them), an environment variable is
beyond most users and is not visible to the Claude desktop app, and a key must not be typed into a conversation. So the key
lives in a small text file on the user's computer, next to their saved answers, where they can see it and remove it:

    Documents/EDGAR Research/tiingo-key.txt

The plugin creates that file, empty but for its instructions, the first time prices are needed, and reads it at the start
of every question, so nothing has to be restarted. An environment variable still works and comes first (the publisher's
own runs use one). The key is sent to Tiingo only, in a request header; it is never written anywhere else by this plugin.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

from . import workdir

FILE = "tiingo-key.txt"
SIGN_UP = "https://www.tiingo.com"
TOKEN_PAGE = "https://www.tiingo.com/account/api/token"
TEMPLATE = ("# Share-price key for the EDGAR Research Agent.\n"
            "# 1. Create a free account at " + SIGN_UP + "\n"
            "# 2. Signed in, open " + TOKEN_PAGE + " and copy the API token shown there.\n"
            "# 3. Paste the token on the empty line below, save this file, and ask your question again.\n"
            "# The key stays on this computer and is sent to Tiingo only. Delete this file to stop using it.\n"
            "\n")
_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{24,64}(?![A-Za-z0-9])")


def _clean(value: Optional[str]) -> str:
    v = (value or "").strip()
    return "" if not v or v.startswith("${") else v


def folders(environ=os.environ) -> List[Path]:
    """Where the key file is looked for: the folder of the saved answers first (the user knows it), then the data folder."""
    out = []
    where = _clean(environ.get("EDGAR_SAVE_DIR"))
    if where and where.lower() != "off":
        out.append(Path(where).expanduser())
    else:
        docs = Path.home() / "Documents"
        out.append((docs if docs.is_dir() else Path.home()) / "EDGAR Research")
    out.append(workdir.data_root(environ))
    return out


def _from_text(text: str) -> str:
    """The key in a file's text: the first line that is not a comment and holds one token-like word. A file saved as rich
    text by mistake (TextEdit's default) still yields its token."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words = _TOKEN.findall(line)
        if len(words) == 1 or (words and line == words[0]):
            return words[0]
        hexes = [w for w in words if re.fullmatch(r"[0-9a-f]{40}", w)]
        if hexes:
            return hexes[0]
    return ""


def load(environ=os.environ) -> str:
    """The key, or ''. The environment variable first, then the key file."""
    key = _clean(environ.get("EDGAR_PRICE_API_KEY"))
    if key:
        return key
    for folder in folders(environ):
        for name in (FILE, "tiingo-key.rtf", "tiingo-key"):
            try:
                key = _from_text((folder / name).read_text(errors="replace"))
            except OSError:
                continue
            if key:
                return key
    return ""


def prepare_file(environ=os.environ) -> Optional[Path]:
    """Make sure the key file exists (with its instructions and no key) and return its path; None if nowhere can be
    written. An existing file is never touched."""
    for folder in folders(environ):
        path = folder / FILE
        try:
            if not path.exists():
                folder.mkdir(parents=True, exist_ok=True)
                path.write_text(TEMPLATE, encoding="utf-8")
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
            return path
        except OSError:
            continue
    return None


def how_to(environ=os.environ) -> str:
    """What Claude passes on to the user when prices are needed and there is no usable key."""
    path = prepare_file(environ)
    where = (f"open the file {path} (this plugin has put it there, with these steps inside), paste the token on the empty line "
             "at the end and save it" if path else
             "set it as the environment variable EDGAR_PRICE_API_KEY on this computer")
    return ("Give the user these steps, numbered, in your reply: (1) create a free account at " + SIGN_UP + "; (2) signed in, open "
            + TOKEN_PAGE + " and copy the API token shown there; (3) " + where + "; (4) ask the question again: the key is read at "
            "the start of each question, nothing needs restarting. If a key was refused, step 3 is to replace the token in that "
            "file with a new one from the same page. Tell them the key stays on their computer and is sent to Tiingo only, and "
            "that they must NOT paste it into this conversation. If they paste it here anyway, do not repeat it, do not save it "
            "for them, and ask them to put it in the file instead.")
