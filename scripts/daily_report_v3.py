#!/usr/bin/env python3
"""Tagesreport für Hermes Trader v3 — READ-ONLY.

Schließt KEINE Trades und schreibt NICHT ins Journal oder Learning-File.
Das Schließen von Trades ist ausschließlich Aufgabe von poloniex_trader_v3.py.
"""
import json
import os
import datetime
from datetime import timezone
import urllib.request

JOURNAL_PATH = os.path.expanduser("~/.hermes_trader/journal/trades_v3.jsonl")
LEARN_PATH = os.path.expanduser("~/.hermes_trader/journal/learning_v3.json")
BASE_URL = "https://api.poloniex.com"


def api_get(endpoint):
    url = BASE_URL + endpoint
    req = urllib.request.Request(url, headers={"User-Agent": "HermesTrader/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def get_ticker(symbol):
    return api_get(f"/markets/{symbol}/price")


def load_learning():
    if os.path.exists(LEARN_PATH):
        try:
            with open(LEARN_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def is_win(t):
    """Gleiche Definition wie der Bot (update_learning): nur result == WIN zählt als Win."""
    return t.get("result", "") == "WIN"


def is_loss(t):
    """Alles andere (LOSS, TIME_STOP egal welchen PnL) zählt als Verlust — wie im Bot."""
    return t.get("result", "") in ("LOSS", "TIME_STOP")


def _parse_close_time(t):
    ct = t.get("close_time")
    if ct:
        try:
            dt = datetime.datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None
    return None


def main():
    now = datetime.datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    last_report = now - datetime.timedelta(hours=24)

    if not os.path.exists(JOURNAL_PATH):
        print(f"📊 TAGESREPORT {today}")
        print("Noch keine Trades im Journal.")
        return

    trades = []
    with open(JOURNAL_PATH, "r") as f:
        for line in f:
            try:
                trades.append(json.loads(line))
            except Exception:
                pass

    # "HEUTE" = seit letztem Report (letzte 24h), nicht Kalendertag
    today_trades = [t for t in trades if t.get("status") == "CLOSED" and (ct := _parse_close_time(t)) and ct >= last_report]
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    all_closed = [t for t in trades if t.get("status") == "CLOSED"]

    today_wins = [t for t in today_trades if is_win(t)]
    today_losses = [t for t in today_trades if is_loss(t)]
    today_pnl = sum(t.get("pnl_pct", 0) for t in today_trades)

    total_wins = [t for t in all_closed if is_win(t)]
    total_losses = [t for t in all_closed if is_loss(t)]
    total_pnl = sum(t.get("pnl_pct", 0) for t in all_closed)
    total = len(total_wins) + len(total_losses)
    wr = round(len(total_wins) / total * 100, 1) if total else 0

    # Pair-Stats
    pair_stats = {}
    for t in all_closed:
        p = t["pair"]
        if p not in pair_stats:
            pair_stats[p] = {"wins": 0, "losses": 0, "pnl": 0}
        if is_win(t):
            pair_stats[p]["wins"] += 1
        elif is_loss(t):
            pair_stats[p]["losses"] += 1
        pair_stats[p]["pnl"] += t.get("pnl_pct", 0)

    print(f"📊 TAGESREPORT {today}")
    print(f"")
    print(f"HEUTE (seit letztem Report):")
    print(f"  ✅ Gewonnen: {len(today_wins)}")
    print(f"  🔴 Verloren: {len(today_losses)}")
    print(f"  💰 Tages-PnL: {today_pnl:+.4f}%")
    print(f"")
    print(f"GESAMT (alle Zeit):")
    print(f"  ✅ Gewonnen: {len(total_wins)}")
    print(f"  🔴 Verloren: {len(total_losses)}")
    print(f"  📈 Winrate: {wr}%")
    print(f"  💰 Total PnL: {total_pnl:+.4f}%")
    print(f"  🔄 Offen: {len(open_trades)}")

    total_unreal = 0.0
    if open_trades:
        print(f"")
        print(f"OFFENE TRADES:")
        for t in open_trades:
            tk = get_ticker(t["pair"])
            if "error" not in tk:
                price = float(tk.get("price", 0))
            else:
                price = 0
            entry = float(t["entry"])
            direction = t["direction"]
            if price > 0:
                if direction == "LONG":
                    unrealized = (price - entry) / entry * 100
                else:
                    unrealized = (entry - price) / entry * 100
                total_unreal += unrealized
                unreal_str = f"\n  Unreal: {unrealized:+.2f}%"
            else:
                unreal_str = "\n  Unreal: N/A"
            print(f"  {t['pair']} {t['direction']} @ {t['entry']}\n  Aktuell: {price:.4f}{unreal_str}\n  SL: {t['stop_loss']}\n  TP: {t['take_profit']}\n  {t['setup_type']}")
        print(f"")
        print(f"  📊 Gesamt-Unreal PnL: {total_unreal:+.2f}%")

    if pair_stats:
        print(f"")
        print(f"PER PAIR:")
        for p, s in sorted(pair_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
            wr_p = round(s["wins"] / (s["wins"] + s["losses"]) * 100, 1) if (s["wins"] + s["losses"]) else 0
            print(f"  {p}: {s['wins']}W/{s['losses']}L\n  WR {wr_p}%\n  PnL {s['pnl']:+.4f}%")

    # Lern-Stats (nur Anzeige, kein Schreiben)
    learn = load_learning()
    if learn:
        print(f"")
        print(f"STRATEGIE-LEARNING:")
        for key, data in sorted(learn.items(), key=lambda x: x[1].get("pf", 0), reverse=True)[:10]:
            total_t = data.get("wins", 0) + data.get("losses", 0)
            if total_t >= 3:
                print(f"  {key}:\n  PF={data.get('pf', 0)}\n  W={data['wins']} L={data['losses']}\n  Weight={data.get('weight', 1.0)}")


if __name__ == "__main__":
    main()
