#!/usr/bin/env python3
import json
import urllib.request

TRADES_FILE = "/opt/data/.hermes_trader/journal/trades_v3.jsonl"

open_trades = []
try:
    with open(TRADES_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trade = json.loads(line)
            except Exception:
                continue  # einzelne defekte Zeile überspringen, Rest weiterlesen
            if trade.get("status") == "OPEN":
                open_trades.append(trade)
except FileNotFoundError:
    pass

if not open_trades:
    exit(0)

prices = {}
for trade in open_trades:
    pair = trade["pair"]
    if pair in prices:
        continue
    try:
        url = f"https://api.poloniex.com/markets/{pair}/price"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            prices[pair] = float(data["price"])
    except Exception:
        continue

for trade in open_trades:
    pair = trade["pair"]
    entry = trade["entry"]
    direction = trade["direction"]
    price = prices.get(pair)
    if price is None:
        continue
    if direction == "LONG":
        pnl_pct = (price - entry) / entry * 100
    else:
        pnl_pct = (entry - price) / entry * 100
    print(f"{pair}: {pnl_pct:+.2f}%")
