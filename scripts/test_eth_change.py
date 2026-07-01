#!/usr/bin/env python3
"""
Testet eine einzelne Änderung an der ETH-Avoid-List im Portfolio-Kontext.
Sichert strategy_map_v3.json, modifiziert es temporär, führt Backtest aus
und stellt das Original wieder her.
"""

import os, json, shutil, subprocess

MAP_PATH = os.path.expanduser("~/.hermes_trader/engine/strategy_map_v3.json")
BACKUP_PATH = MAP_PATH + ".backup"

def load_map():
    with open(MAP_PATH) as f:
        return json.load(f)

def save_map(data):
    with open(MAP_PATH, "w") as f:
        json.dump(data, f, indent=2)

def run_backtest():
    result = subprocess.run(
        ["python3", "backtest_360d.py"],
        cwd="/opt/data/.hermes/scripts",
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.stdout + result.stderr

def extract_summary(output):
    summary = {}
    for line in output.splitlines():
        if "Profit Factor:" in line:
            summary["pf"] = float(line.split(":")[1].strip())
        elif "Gesamt-PnL:" in line:
            summary["pnl"] = float(line.split(":")[1].replace("%", "").strip())
        elif "Trades gesamt:" in line:
            summary["trades"] = int(line.split(":")[1].strip())
        elif "ETH_USDT:" in line:
            parts = line.split()
            for p in parts:
                if "PF=" in p:
                    summary["eth_pf"] = float(p.split("=")[1])
                elif "PnL=" in p:
                    summary["eth_pnl"] = float(p.split("=")[1].replace("%", ""))
                elif "Trades=" in p:
                    summary["eth_trades"] = int(p.split("=")[1])
    return summary

def test_changes(eth_add_avoid_list):
    if os.path.exists(BACKUP_PATH):
        os.remove(BACKUP_PATH)
    shutil.copy(MAP_PATH, BACKUP_PATH)

    try:
        data = load_map()
        pairs = data.get("pairs", data)
        eth = pairs.get("ETH_USDT", {})
        avoid = set(eth.get("avoid", []))
        for s in eth_add_avoid_list:
            avoid.add(s)
        eth["avoid"] = sorted(avoid)
        save_map(data)

        print(f"Teste: ETH avoid += {eth_add_avoid_list}")
        output = run_backtest()
        summary = extract_summary(output)
        print(f"  Portfolio PF={summary.get('pf')} PnL={summary.get('pnl')}% Trades={summary.get('trades')}")
        print(f"  ETH PF={summary.get('eth_pf')} PnL={summary.get('eth_pnl')}% Trades={summary.get('eth_trades')}")
        return summary
    finally:
        shutil.copy(BACKUP_PATH, MAP_PATH)
        os.remove(BACKUP_PATH)

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 test_eth_change.py <STRATEGY_NAME> [STRATEGY_NAME ...]")
        print("Example: python3 test_eth_change.py LOWER_HIGH_BREAK EMA_BOUNCE_SHORT")
        sys.exit(1)
    summary = test_changes(sys.argv[1:])
    print(f"\nFinal: PF={summary.get('pf')} PnL={summary.get('pnl')}% Trades={summary.get('trades')}")
