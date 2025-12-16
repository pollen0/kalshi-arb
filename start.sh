#!/bin/bash
#
# Kalshi Auto-Trading System
#

cd "$(dirname "$0")"
export ODDS_API_KEY="8ef4fc77b1c77d5addeecd6c6e0bb87e"

# Check if already running
if pgrep -f "auto_trader.py" > /dev/null; then
    echo "Auto-trader is already running!"
    echo "Dashboard: http://127.0.0.1:5050"
    echo ""
    echo "To stop: ./stop.sh"
    exit 0
fi

echo ""
echo "=============================================="
echo "     KALSHI AUTO-TRADING SYSTEM"
echo "=============================================="
echo ""
echo "WARNING: This trades with REAL MONEY!"
echo ""
echo "Starting in background..."

# Run in background with logging
nohup python3 auto_trader.py > trader.log 2>&1 &
echo $! > trader.pid

sleep 2

if pgrep -f "auto_trader.py" > /dev/null; then
    echo ""
    echo "✓ Auto-trader is running!"
    echo ""
    echo "Dashboard: http://127.0.0.1:5050"
    echo "Logs:      tail -f trader.log"
    echo "Stop:      ./stop.sh"
    echo ""
else
    echo "Failed to start. Check trader.log for errors."
fi
