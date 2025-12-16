#!/bin/bash
#
# Stop Kalshi Auto-Trading System
#

cd "$(dirname "$0")"

if [ -f trader.pid ]; then
    PID=$(cat trader.pid)
    if kill -0 $PID 2>/dev/null; then
        echo "Stopping auto-trader (PID: $PID)..."
        kill $PID
        rm trader.pid
        echo "Stopped."
    else
        echo "Process not running. Cleaning up..."
        rm trader.pid
    fi
else
    # Try to find and kill by name
    pkill -f "auto_trader.py" 2>/dev/null
    echo "Stopped auto-trader."
fi
