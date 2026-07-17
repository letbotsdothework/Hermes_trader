#!/usr/bin/env python3
"""
Hermes Monthly Strategy Report v1

Liest die letzten 90 Tage aus trades_v3.jsonl und erstellt eine
Strategie-Performance-Übersicht pro Pair:
  - Top Performer (PF >= 2.0, >= 10 Trades)
  - Underperformer (PF < 1.0, >= 5 Trades)
  - Empfehlung für Avoid-List

Ändert keine Konfiguration. Output geht an stdout (vom Cronjob weitergeleitet).
"""

import os, json, math
from datetime import datetime, timezone, timedelta

JOURNAL_PATH = os.path.expanduser("~/.hermes_trader/journal/trades_v3.jsonl")
STRATEGY_MAP_PATH = os.path.expanduser("~/.hermes_trader/engine/strategy_map_v3.json")
DAYS = 90

ALL_STRATEGIES = [
    "TREND_FOLLOW_LONG", "MEAN_REVERSION_LONG", "EMA_BOUNCE_LONG",
    "VWAP_RETAIL_LONG", "RSI_DIVERGENCE_LONG", "WILLR_LONG",
    "BB_SQUEEZE_LONG", "BB_BOUNCE_LONG", "EMA50_BOUNCE_LONG",
    "HIGHER_LOW_BREAK", "LOWER_HIGH_BREAK",
    "TREND_FOLLOW_SHORT", "MEAN_REVERSION_SHORT", "EMA_BOUNCE_SHORT",
    "VWAP_RETAIL_SHORT", "RSI_DIVERGENCE_SHORT",
    "EMA_COMPRESSION_BREAK", "STOCH_MR_SHORT", "WILLR_SHORT",
    "RANGE_BREAKOUT_SHORT", "BB_BOUNCE_SHORT"
]


def parse_ts(value):
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def load_trades():
    if not os.path.exists(JOURNAL_PATH):
        return []
    trades = []
    with open(JOURNAL_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except Exception:
                pass
    return trades


def load_strategy_map():
    if not os.path.exists(STRATEGY_MAP_PATH):
        return {}
    with open(STRATEGY_MAP_PATH) as f:
        data = json.load(f)
    if "pairs" in data:
        return data["pairs"]
    return {k: v for k, v in data.items() if not k.startswith("_")}


def calc_stats(pnls):
    total = len(pnls)
    if total == 0:
        return 0, 0, 0, 0
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss else (float('inf') if gross_profit else 0)
    wr = len(wins) / total * 100
    pnl = sum(pnls)
    return total, pf, wr, pnl


def fmt_pf(v):
    if math.isinf(v):
        return "∞"
    return f"{v:.2f}"


def main():
    now = datetime.now(timezone.utc)
    cutoff_ms = int((now - timedelta(days=DAYS)).timestamp() * 1000)

    trades = load_trades()
    strat_map = load_strategy_map()

    recent = [t for t in trades if parse_ts(t.get("timestamp") or t.get("entry_time")) >= cutoff_ms]

    # Gruppieren nach (pair, strategy)
    groups = {}
    for t in recent:
        pair = t.get("pair", "?")
        strat = t.get("setup_type", t.get("setup", "?"))
        key = (pair, strat)
        groups.setdefault(key, []).append(t.get("pnl_pct", 0))

    # Pair-Strategie-Kombinationen aus strategy_map aufbauen
    active_pairs = sorted(strat_map.keys())

    results = []
    for pair in active_pairs:
        cfg = strat_map.get(pair, {})
        avoid = set(cfg.get("avoid", []))
        for strat in ALL_STRATEGIES:
            key = (pair, strat)
            pnls = groups.get(key, [])
            total, pf, wr, pnl = calc_stats(pnls)
            results.append({
                "pair": pair,
                "strategy": strat,
                "trades": total,
                "pf": pf,
                "wr": wr,
                "pnl": pnl,
                "active": strat not in avoid,
            })

    top = [r for r in results if r["trades"] >= 10 and r["pf"] >= 2.0]
    under = [r for r in results if r["trades"] >= 5 and r["pf"] < 1.0]
    top.sort(key=lambda x: (-x["pf"], -x["pnl"]))
    under.sort(key=lambda x: (x["pf"], x["pnl"]))

    # Empfehlungen
    add_to_avoid = [r for r in under if r["active"]]
    remove_from_avoid = [r for r in top if not r["active"]]

    lines = []
    lines.append(f"📊 Hermes Monthly Strategy Report\nLetzte {DAYS} Tage\nStand: {now.strftime('%Y-%m-%d %H:%M')} UTC")
    lines.append("")
    lines.append(f"Gesamt-Trades im Zeitraum: {len(recent)}")
    lines.append(f"Betrachtete Pair/Strategie-Kombinationen: {len(results)}")
    lines.append("")

    if top:
        lines.append("⭐ Top Performer (PF ≥ 2.0, ≥ 10 Trades)")
        for r in top[:15]:
            flag = " [aktiv]" if r["active"] else " [avoided]"
            lines.append(f"  {r['pair']} {r['strategy']}: PF={fmt_pf(r['pf'])} WR={r['wr']:.1f}% PnL={r['pnl']:+.2f}% Trades={r['trades']}{flag}")
        lines.append("")

    if under:
        lines.append("⚠️ Underperformer (PF < 1.0, ≥ 5 Trades)")
        for r in under[:15]:
            flag = " [aktiv]" if r["active"] else " [avoided]"
            lines.append(f"  {r['pair']} {r['strategy']}: PF={fmt_pf(r['pf'])} WR={r['wr']:.1f}% PnL={r['pnl']:+.2f}% Trades={r['trades']}{flag}")
        lines.append("")

    if add_to_avoid:
        lines.append("🔻 Empfohlene Avoid-List-Ergänzungen (aktiv, aber schlecht)")
        for r in add_to_avoid:
            lines.append(f"  {r['pair']}: '{r['strategy']}' (PF={fmt_pf(r['pf'])}, Trades={r['trades']})")
        lines.append("")

    if remove_from_avoid:
        lines.append("🔼 Erwägen: Aus Avoid-List entfernen (avoided, aber stark)")
        for r in remove_from_avoid:
            lines.append(f"  {r['pair']}: '{r['strategy']}' (PF={fmt_pf(r['pf'])}, Trades={r['trades']})")
        lines.append("")

    # Per-Pair Übersicht
    lines.append("📋 Per Pair\naktive Strategien mit Trades")
    for pair in active_pairs:
        pair_res = [r for r in results if r["pair"] == pair and r["trades"] > 0]
        cfg = strat_map.get(pair, {})
        avoid = cfg.get("avoid", [])
        lines.append(f"\n{pair} (Avoid: {len(avoid)}):")
        if not pair_res:
            lines.append("  Keine Trades im Zeitraum")
            continue
        pair_res.sort(key=lambda x: -x["pf"])
        for r in pair_res[:8]:
            status = "✓" if r["active"] else "✗"
            lines.append(f"  {status} {r['strategy']}: PF={fmt_pf(r['pf'])} WR={r['wr']:.1f}% PnL={r['pnl']:+.2f}% ({r['trades']}T)")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
