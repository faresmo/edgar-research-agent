"""The MCP server over stdio, with nothing but Python's standard library.

The public plugin starts this file directly (`python3 ${CLAUDE_PLUGIN_ROOT}/server/run.py`): the directory installs only
the plugin folder, so the server cannot depend on a package the user would have to install (13 Nov 2026). server.py, which
uses the `mcp` SDK, stays for development.

The stdio transport of the Model Context Protocol is JSON-RPC 2.0, one message per line, UTF-8. This server offers tools
only. Tool calls run on worker threads, so a long EDGAR lookup does not stop the server answering a ping; the session
itself takes one call at a time.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Dict, Optional

from . import contact as _contact
from . import workdir as _workdir

NAME, VERSION = "edgar-research-agent", "0.3.5"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first; the client's choice is honoured when it is one of these


_ENV_FROM_EVAL = ("EDGAR_USER_AGENT", "EDGAR_PRICE_API_KEY")


def settle_env(environ=os.environ) -> None:
    """`claude plugin eval` withholds the shell environment except `EVAL_*` names, and an unexpanded
    `${EDGAR_PRICE_API_KEY}` in .mcp.json arrives as that literal text (the 1 Nov eval: every price call
    403, three questions abandoned). Accept `EVAL_<NAME>` as a fallback and treat `${...}` as unset."""
    for name in _ENV_FROM_EVAL:
        value = (environ.get(name) or "").strip()
        if not value or (value.startswith("${") and value.endswith("}")):
            fallback = (environ.get("EVAL_" + name) or "").strip()
            if fallback:
                environ[name] = fallback
            elif name in environ:
                del environ[name]


class Server:
    def __init__(self, session, instructions: str = "", out=None):
        self.session, self.instructions = session, instructions
        self.out = out or sys.stdout
        self._write_lock = threading.Lock()
        self._threads = []

    # -- wire ------------------------------------------------------------
    def send(self, message: Dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            self.out.write(line + "\n")
            self.out.flush()

    def reply(self, mid, result=None, error: Optional[Dict[str, Any]] = None) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, **({"error": error} if error else {"result": result if result is not None else {}})})

    # -- methods ---------------------------------------------------------
    def tools(self):
        return [{"name": t["name"], "description": t["description"], "inputSchema": t.get("input_schema") or {"type": "object", "properties": {}}}
                for t in self.session.tool_definitions()]

    def call_tool(self, mid, params: Dict[str, Any]) -> None:
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            text, failed = self.session.call(str(name), dict(args)), False
        except Exception as exc:  # noqa: BLE001 - a tool failure is a result the model can read, not a broken connection
            text, failed = f"{type(exc).__name__}: {exc}", True
        self.reply(mid, {"content": [{"type": "text", "text": text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)}],
                         "isError": failed})

    def handle(self, msg: Dict[str, Any]) -> None:
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None or mid is None:
            return  # a response or a notification (notifications/initialized, cancelled): nothing to answer
        if method == "initialize":
            asked = str(params.get("protocolVersion") or "")
            result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0], "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": NAME, "version": VERSION}}
            if self.instructions:
                result["instructions"] = self.instructions
            return self.reply(mid, result)
        if method == "ping":
            return self.reply(mid, {})
        if method == "tools/list":
            return self.reply(mid, {"tools": self.tools()})
        if method == "tools/call":
            t = threading.Thread(target=self.call_tool, args=(mid, params), daemon=True)
            self._threads.append(t)
            return t.start()
        self.reply(mid, error={"code": -32601, "message": f"Method not found: {method}"})

    def serve(self, lines) -> None:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                if isinstance(m, dict):
                    try:
                        self.handle(m)
                    except Exception as exc:  # noqa: BLE001
                        if m.get("id") is not None:
                            self.reply(m["id"], error={"code": -32603, "message": f"{type(exc).__name__}: {exc}"})
        for t in self._threads:  # the client closed its end: let calls in flight finish and answer
            t.join(timeout=30)


def build(environ=os.environ) -> Server:
    """The session behind the server. Without an SEC contact the server still starts and lists its tools; every tool
    then answers by asking for the contact (set_sec_contact), because nothing may be sent to the SEC without one."""
    from edgar_core import EdgarClient
    from .session import make_session, session_instructions
    settle_env(environ)
    agent = _contact.from_settings(environ) or _contact.load()
    if agent:
        environ["EDGAR_USER_AGENT"] = agent
    # 21 Nov: the answer records and the cache go to a fixed place that can be written, not to the folder the server
    # happens to run in (read-only in Cowork; the user's project in Claude Code). EDGAR_RUNS_DIR still names another place.
    root = _workdir.data_root(environ)
    runs = _contact.clean(environ.get("EDGAR_RUNS_DIR"))
    session = make_session(edgar_client=EdgarClient(user_agent=agent or "contact not set", cache_dir=str(root / "cache")),
                           run_root=None if runs else root / "runs")
    session.needs_contact = not agent
    return Server(session, session_instructions())


def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    build().serve(sys.stdin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
