"""The notes file: every fact the agent gathers, with its source.

One JSON object per line, appended as the run goes, so it survives a crash
and context compaction. The sourcing check reads it to decide whether an
answer may be submitted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

SOURCE_TYPES = {"filing", "web", "lead", "calculation", "method", "web_calculation"}  # web_calculation: computed from non-SEC figures


class Notes:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self._entries: List[Dict[str, Any]] = []
        if self.path and self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    self._entries.append(json.loads(line))

    def add(self, label: str, source_type: str, value: Any = None, unit: str = "", period_end: str = "",
            accession_number: str = "", url: str = "", quote: str = "", detail: str = "") -> Dict[str, Any]:
        """Record one fact. A `filing` fact must carry an accession number."""
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {sorted(SOURCE_TYPES)}")
        if source_type == "filing" and not accession_number:
            raise ValueError("a filing fact needs its accession number")
        entry = {"id": len(self._entries) + 1, "label": label, "source_type": source_type, "value": value,
                 "unit": unit, "period_end": period_end, "accession_number": accession_number, "url": url,
                 "quote": quote, "detail": detail}
        self._entries.append(entry)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as handle:
                handle.write(json.dumps(entry) + "\n")
        return entry

    def entries(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        return [e for e in self._entries if source_type is None or e["source_type"] == source_type]

    def render(self, max_chars: int = 6000) -> str:
        """A compact listing for the model to read back."""
        lines = []
        for e in self._entries:
            val = f" = {e['value']}{(' ' + e['unit']) if e['unit'] else ''}" if e["value"] is not None else ""
            src = e["accession_number"] or e["url"] or ""
            lines.append(f"[{e['id']}] {e['label']}{val} | {e['source_type']} {src} {e['period_end']}".rstrip())
        text = "\n".join(lines) or "(no notes yet)"
        return text if len(text) <= max_chars else text[-max_chars:]

    def __len__(self) -> int:
        return len(self._entries)
