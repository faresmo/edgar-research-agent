#!/usr/bin/env python3
"""Starts the EDGAR research server of this plugin. Standard library only; needs Python 3.9 or later."""
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    sys.stderr.write("EDGAR Research Agent needs Python 3.9 or later; this is %s\n" % sys.version.split()[0])
    raise SystemExit(2)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from edgar_mcp.stdio import main  # noqa: E402

raise SystemExit(main())
