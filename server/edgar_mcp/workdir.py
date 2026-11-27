"""Where the plugin keeps its working files: the record of each answer and the cache of what it fetched.

Until 21 Nov 2026 both were created in the folder the server happened to run in (`runs` and `.edgar_cache`). In Claude
Code that is the user's project folder, so the plugin left its files, the cache among them, in the user's work. In Cowork
that folder cannot be written, and every lookup failed with "read-only file system" (the first Cowork trials, 21 Nov).
The SEC contact was saved all the same, because it goes to a fixed place; the working files now go to the same kind of place.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _usable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(folder, os.W_OK | os.X_OK)


def data_root(environ=os.environ) -> Path:
    """The first of these that can be written: a folder the user names (EDGAR_DATA_DIR), the folder the app gives the
    plugin (CLAUDE_PLUGIN_DATA), `~/.edgar-agent` (where the contact is saved), and last the system's temporary folder,
    so that a lookup is never refused for want of somewhere to keep its record."""
    places = [Path(d) for d in (environ.get("EDGAR_DATA_DIR"), environ.get("CLAUDE_PLUGIN_DATA")) if d and not d.startswith("${")]
    places.append(Path.home() / ".edgar-agent")
    places.append(Path(tempfile.gettempdir()) / "edgar-agent")
    for folder in places:
        if _usable(folder):
            return folder
    return places[-1]
