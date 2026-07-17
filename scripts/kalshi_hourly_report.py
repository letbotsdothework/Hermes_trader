#!/usr/bin/env python3
"""Stündlicher Kalshi-Gap-Report — kurz & knapp."""
import csv
import os
import datetime

LOG_FILE = "/opt/data/scripts/kalshi_gaps_log.csv"

if not os.path.exists(LOG_FILE):
    print("Kalshi: Noch keine Daten.")
    exit(0)

scans = set()
gaps = []
with open(LOG_FILE, "r", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        scans.add(row["scan_nr"])
        gaps.append(row)

if not gaps:
    print("Kalshi: Keine positiven Gaps in diesem Zeitraum.")
    exit(0)

total_scans = len(scans)
total_gaps = len(gaps)

# Summe potenzieller Profit
total_profit = sum(float(g["total_net"]) for g in gaps)

# Best gap
best = max(gaps, key=lambda r: float(r["total_net"]))

# Kategorien
cats = {}
for g in gaps:
    ticker = g["ticker"]
    cat = ""
    for ch in ticker:
        if ch.isalpha():
            cat += ch
        else:
            break
    cats[cat] = cats.get(cat, 0) + 1

top_cats = sorted(cats.items(), key=lambda x: x[1], reverse=True)[:3]
cat_str = ", ".join(f"{k}:{v}" for k, v in top_cats)

now = datetime.datetime.now().strftime("%H:%M")
print(
    f"📊 Kalshi {now}\n"
    f"Scans: {total_scans}\n"
    f"Gaps: {total_gaps}\n"
    f"Profitpotenzial: ${total_profit:.2f}\n"
    f"Best: {best['ticker']} ${float(best['total_net']):.2f}\n"
    f"Top: {cat_str}"
)
