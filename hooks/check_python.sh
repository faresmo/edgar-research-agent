#!/bin/sh
# Runs when a session starts. The EDGAR Research Agent's checking program needs Python 3.9 or later. Without it the
# program cannot start, and the user would get ordinary answers with nothing checked and no word of why (13 Nov 2026:
# "we can not have silent failure"). This script uses only what the shell itself provides, so it runs where Python does not.
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  exit 0
fi
found="no Python 3 was found on this computer"
if command -v python3 >/dev/null 2>&1; then
  found="the Python 3 on this computer is older than 3.9, or could not be run"
fi
printf '%s\n' \
  "EDGAR Research Agent is installed but CANNOT RUN in this session: $found." \
  "Its checking program needs Python 3.9 or later, so none of its tools (classify_question, check_answer and the rest) are available." \
  "Tell the user this plainly the first time they ask about a company's figures or filings, before anything else:" \
  "- the plugin is not working, so nothing can be looked up or checked;" \
  "- the fix is to install Python 3.9 or later (python.org/downloads; on a Mac, running 'xcode-select --install' in Terminal also provides it) and then start a new session;" \
  "- until then you will not give company figures, because without the plugin they would be unchecked figures from memory." \
  "Do not answer such a question from memory."
exit 0
