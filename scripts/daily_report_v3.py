#!/usr/bin/env python3
import json
import os
import datetime
from datetime import timezone
import urllib.request

JOURNAL_PATH = "/opt/data/.hermes_trader/journal/trades_v3.jsonl"
LEARN_PATH = "/opt/data/.hermes_trader/journal/learning_v3.json"
LOG_PATH = "/opt/data/.hermes_trader/logs/agent_v3.log"
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
        with open(LEARN_PATH, "r") as f:
            return json.load(f)
    return {}

def save_learning(data):
    os.makedirs(os.path.dirname(LEARN_PATH), exist_ok=True)
    with open(LEARN_PATH, "w") as f:
        json.dump(data, f, indent=2)

def is_win(t):
    """TIME_STOP mit positivem PnL zählt als Gewinn."""
    result = t.get("result", "")
    pnl = t.get("pnl_pct", 0)
    return result == "WIN" or (result == "TIME_STOP" and pnl > 0)

def is_loss(t):
    """TIME_STOP mit negativem PnL zählt als Verlust."""
    result = t.get("result", "")
    pnl = t.get("pnl_pct", 0)
    return result == "LOSS" or (result == "TIME_STOP" and pnl <= 0)

def update_learning(pair, strategy, result, pnl_pct):
    learn = load_learning()
    key = f"{pair}:{strategy}"
    if key not in learn:
        learn[key] = {"wins": 0, "losses": 0, "total_pnl": 0, "gross_profit": 0, "gross_loss": 0, "pf": 0, "weight": 1.0}
    entry = learn[key]
    if "gross_profit" not in entry:
        entry["gross_profit"] = 0
    if "gross_loss" not in entry:
        entry["gross_loss"] = 0
    if result == "WIN":
        entry["wins"] += 1
        entry["gross_profit"] += pnl_pct
    else:
        entry["losses"] += 1
        entry["gross_loss"] += abs(pnl_pct)
    entry["total_pnl"] += pnl_pct
    if entry["gross_loss"] > 0:
        entry["pf"] = round(entry["gross_profit"] / entry["gross_loss"], 2)
    else:
        entry["pf"] = float(entry["gross_profit"]) if entry["gross_profit"] > 0 else 0.0
    total = entry["wins"] + entry["losses"]
    if total >= 5:
        if entry["pf"] < 1.0:
            entry["weight"] = max(0.1, entry["weight"] * 0.8)
        elif entry["pf"] > 1.5:
            entry["weight"] = min(2.0, entry["weight"] * 1.1)
    save_learning(learn)

def log(msg):
    ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts} UTC] {msg}"
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def simulate_and_close():
    """Prüft alle offenen Trades gegen aktuelle Preise und schließt sie bei SL/TP"""
    if not os.path.exists(JOURNAL_PATH):
        return 0, 0, 0.0

    with open(JOURNAL_PATH, "r") as f:
        lines = f.readlines()

    updated = []
    closed_count = 0
    wins = 0
    losses = 0
    total_pnl = 0.0

    for line in lines:
        try:
            t = json.loads(line)
        except:
            updated.append(line)
            continue

        if t.get("status") != "OPEN":
            updated.append(line)
            continue

        tk = get_ticker(t["pair"])
        if "error" in tk:
            updated.append(line)
            continue

        price = float(tk.get("price", 0))
        if price == 0:
            updated.append(line)
            continue

        direction, sl, tp, entry = t["direction"], float(t["stop_loss"]), float(t["take_profit"]), float(t["entry"])
        result = None
        pnl = 0.0

        if direction == "LONG":
            if price <= sl:
                result, pnl = "LOSS", (sl - entry) / entry * 100
            elif price >= tp:
                result, pnl = "WIN", (tp - entry) / entry * 100
        else:
            if price >= sl:
                result, pnl = "LOSS", (entry - sl) / entry * 100
            elif price <= tp:
                result, pnl = "WIN", (entry - tp) / entry * 100

        if result:
            t["status"] = "CLOSED"
            t["result"] = result
            t["exit_price"] = price
            t["pnl_pct"] = round(pnl, 4)
            t["close_time"] = datetime.datetime.now(timezone.utc).isoformat()
            log(f"[REPORT CLOSE] {t['pair']} {direction} {result} @ {price:.4f} PnL={pnl:.2f}%")
            update_learning(t["pair"], t["setup_type"], result, pnl)
            updated.append(json.dumps(t) + "\n")
            closed_count += 1
            total_pnl += pnl
            if result == "WIN":
                wins += 1
            else:
                losses += 1
        else:
            updated.append(line)

    if closed_count > 0:
        with open(JOURNAL_PATH, "w") as f:
            f.writelines(updated)

    return wins, losses, total_pnl

def _parse_close_time(t):
    ct = t.get("close_time")
    if ct:
        try:
            return datetime.datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except Exception:
            return None
    return None

def main():
    now = datetime.datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    last_report = now - datetime.timedelta(hours=24)

    # 1. ERST offene Trades prüfen und schließen
    closed_wins, closed_losses, closed_pnl = simulate_and_close()
    if closed_wins or closed_losses:
        print(f"🔍 Pre-Check: {closed_wins} WIN / {closed_losses} LOSS geschlossen (PnL: {closed_pnl:+.4f}%)")
        print("")

    if not os.path.exists(JOURNAL_PATH):
        print(f"📊 TAGESREPORT {today}")
        print("Noch keine Trades im Journal.")
        return

    trades = []
    with open(JOURNAL_PATH, "r") as f:
        for line in f:
            try:
                trades.append(json.loads(line))
            except:
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
    wr = round(len(total_wins)/total*100, 1) if total else 0

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
        for p, s in sorted(pair_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
            wr_p = round(s["wins"]/(s["wins"]+s["losses"])*100, 1) if (s["wins"]+s["losses"]) else 0
            print(f"  {p}: {s['wins']}W/{s['losses']}L\n  WR {wr_p}%\n  PnL {s['pnl']:+.4f}%")

    # Lern-Stats
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
