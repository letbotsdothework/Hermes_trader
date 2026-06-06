#!/bin/bash
# Sync live files into repo and push if changed
set -e

REPO="/opt/data/hermes-trader-repo"
cd "$REPO"

# Sync from live locations to repo
# Backtest engine
cp /opt/data/.hermes/scripts/backtest_360d.py scripts/ 2>/dev/null || true
cp /opt/data/.hermes/scripts/short_grid_search.py scripts/ 2>/dev/null || true
cp /opt/data/.hermes/scripts/short_strategy_optimize.py scripts/ 2>/dev/null || true
cp /opt/data/.hermes/scripts/eth_optimize.py scripts/ 2>/dev/null || true
cp /opt/data/.hermes/scripts/eth_mixed_test.py scripts/ 2>/dev/null || true
cp /opt/data/.hermes/scripts/btc_short_optimize.py scripts/ 2>/dev/null || true

# Live engine (primary location)
cp ~/.hermes/scripts/poloniex_trader_v3.py scripts/ 2>/dev/null || true

# Strategy maps
cp /opt/data/.hermes_trader/engine/strategy_map.json engine/ 2>/dev/null || true
cp ~/.hermes_trader/engine/strategy_map_v3.json engine/ 2>/dev/null || true

# Check if there are changes
if [ -z "$(git status --porcelain)" ]; then
    # No changes → silent exit (no notification)
    exit 0
fi

# Changes detected → commit and push
git add -A
git commit -m "auto-sync: $(date -u '+%Y-%m-%d %H:%M UTC')" >/dev/null 2>&1
if git push origin main >/dev/null 2>&1; then
    echo "Pushed $(git rev-parse --short HEAD) at $(date -u '+%H:%M UTC')"
else
    echo "Push failed at $(date -u '+%H:%M UTC')"
    exit 1
fi
