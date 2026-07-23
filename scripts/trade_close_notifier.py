#!/usr/bin/env python3
"""
Trade Close Notifier — Backup-Benachrichtigung für geschlossene Trades.
Scannt trades_v3.jsonl auf Trades, die in den letzten 15 Minuten geschlossen wurden.
Nur stdout-Delivery via no_agent=True Cronjob.
"""
import json
import os
from datetime import datetime, timezone, timedelta

JOURNAL_PATH = os.path.expanduser("~/.hermes_trader/journal/trades_v3.jsonl")
STATE_PATH = os.path.expanduser("~/.hermes_trader/journal/.close_notifier_state")
LOOKBACK_MINUTES = 15  # Cron-Intervall; Watermark verhindert Duplikate

def _load_watermark():
    """Letzter gemeldeter close_time-Zeitpunkt (ISO). None beim ersten Lauf."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw = f.read().strip()
            if raw:
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
    except Exception:
        pass
    return None

def _save_watermark(dt):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(dt.isoformat())
    os.replace(tmp, STATE_PATH)

def main():
    if not os.path.exists(JOURNAL_PATH):
        return

    watermark = _load_watermark()
    # Beim allerersten Lauf nur das Lookback-Fenster melden (keine History-Flut)
    cutoff = watermark or (datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES))
    closed_trades = []
    max_seen = watermark

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
                if ts > cutoff:
                    closed_trades.append(t)
                    if max_seen is None or ts > max_seen:
                        max_seen = ts
            except Exception:
                continue

    if max_seen is not None and max_seen != watermark:
        _save_watermark(max_seen)

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
