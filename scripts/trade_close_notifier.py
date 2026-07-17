#!/usr/bin/env python3
"""
Trade Close Notifier — Backup-Benachrichtigung für geschlossene Trades.
Scannt trades_v3.jsonl auf Trades, die in den letzten 15 Minuten geschlossen wurden.
Nur stdout-Delivery via no_agent=True Cronjob.
"""
import json
import os
from datetime import datetime, timezone, timedelta

JOURNAL_PATH = "/opt/data/.hermes_trader/journal/trades_v3.jsonl"
LOOKBACK_MINUTES = 18  # etwas mehr als 15min Intervall, um Lücken zu vermeiden

def main():
    if not os.path.exists(JOURNAL_PATH):
        return

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    closed_trades = []

    with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except Exception:
                continue
            if t.get("status") != "CLOSED" or not t.get("close_time"):
                continue
            try:
                ts = datetime.fromisoformat(t["close_time"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    closed_trades.append(t)
            except Exception:
                continue

    if not closed_trades:
        return  # silent wenn nichts passiert ist (kein "Kein Setup" nötig, ist ja Backup)

    for t in closed_trades:
        pair = t["pair"]
        direction = t["direction"]
        result = t.get("result", "UNKNOWN")
        pnl = t.get("pnl_pct", 0)
        exit_price = t.get("exit_price", "N/A")
        setup = t.get("setup_type", "N/A")
        regime = t.get("regime", "N/A")
        reason = t.get("close_reason", "")
        time_stop = "⏱ Time-Stop" if t.get("time_stop_triggered") else ""
        emoji = "✅" if result == "WIN" else ("❌" if result == "LOSS" else "⏱")
        extra = f"\nGrund: {reason}" if reason else ""
        extra += f"\n{time_stop}" if time_stop else ""
        print(f"{emoji} TRADE GESCHLOSSEN:\n{pair} {direction} {result} @ {exit_price}\nPnL: {pnl:+.2f}%\nSetup: {setup}\nRegime: {regime}{extra}")

if __name__ == "__main__":
    main()
