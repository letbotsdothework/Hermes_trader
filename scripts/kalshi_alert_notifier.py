#!/usr/bin/env python3
"""
Kalshi Alert Notifier — prüft kalshi_alerts.jsonl auf neue Einträge
und sendet Zusammenfassung via Telegram (Hermes send_message).
"""
import json
import os
import sys

ALERT_FILE = "/opt/data/scripts/kalshi_alerts.jsonl"
STATE_FILE = "/opt/data/scripts/.kalshi_alert_state"

def _load_state() -> int:
    try:
        with open(STATE_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0

def _save_state(n: int) -> None:
    with open(STATE_FILE, "w") as f:
        f.write(str(n))

def main() -> None:
    if not os.path.exists(ALERT_FILE):
        sys.exit(0)

    with open(ALERT_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    total_lines = len(lines)
    last_seen = _load_state()

    new_lines = lines[last_seen:]
    if not new_lines:
        sys.exit(0)

    alerts = []
    for line in new_lines:
        line = line.strip()
        if not line:
            continue
        try:
            alerts.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not alerts:
        _save_state(total_lines)
        sys.exit(0)

    # Zusammenfassung bauen
    msg_parts = [f"🎯 *Kalshi Arbitrage Alert* \u2014 {len(alerts)} neue Gaps\n"]
    for a in alerts[:5]:  # max 5 anzeigen
        msg_parts.append(
            f"\n*{a['ticker']}*\n"
            f"YES bid: `{a['yes_bid']:.4f}`\nNO bid: `{a['no_bid']:.4f}`\n"
            f"Net/Paar: `${a['net_per_contract']:.4f}` x{a['contracts']} = `${a['total_net']:.2f}`"
        )
    if len(alerts) > 5:
        msg_parts.append(f"\n\n... und {len(alerts) - 5} weitere.")

    msg = "".join(msg_parts)

    # Über Hermes Gateway an Telegram senden (wenn verfügbar)
    # Fallback: einfach stdout, damit der cronjob es delivern kann
    print(msg)

    _save_state(total_lines)


if __name__ == "__main__":
    main()
