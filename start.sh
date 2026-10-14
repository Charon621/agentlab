#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then python -m venv .venv; fi
if [[ -f .venv/Scripts/python.exe ]]; then PY=.venv/Scripts/python.exe; else PY=.venv/bin/python; fi
if ! "$PY" -c 'import fastapi, langgraph, langgraph.checkpoint.sqlite, httpx' >/dev/null 2>&1; then
  "$PY" -m pip install -r requirements.txt
fi
exec "$PY" run.py
