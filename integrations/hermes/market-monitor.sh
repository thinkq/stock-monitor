#!/usr/bin/env bash
set -euo pipefail
# Absolute deployment directory; this file is copied into ~/.hermes/scripts/ unchanged.
MONITOR_ROOT="/Users/zhaoqing/program/stock-monitor"
exec "$MONITOR_ROOT/.venv/bin/market-monitor-hermes" --config "$MONITOR_ROOT/config.toml"
