#!/bin/bash
# MetaTraderBridge Runner Script
# Ensures correct environment and working directory

cd /home/ishanp/Documents/GitHub/MetaTraderBridge
export PYTHONPATH=/home/ishanp/Documents/GitHub/MetaTraderBridge/src
exec python /home/ishanp/Documents/GitHub/MetaTraderBridge/tools/mcp_mt5_wrapper.py "$@"
