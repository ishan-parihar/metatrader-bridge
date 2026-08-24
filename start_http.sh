#!/bin/bash
cd /home/ishanp/Documents/GitHub/MetaTraderBridge
export PYTHONPATH="src:apps:$PYTHONPATH"
exec .venv/bin/python -m uvicorn apps.mcp_server.main:app --host 127.0.0.1 --port 8010 --log-level warning
