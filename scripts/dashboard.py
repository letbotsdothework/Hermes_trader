#!/usr/bin/env python3
"""
Hermes Trader Dashboard — Pure stdlib, kein pip nötig.
Liest Backtest-JSON und Trade-Logs, rendert HTML.

Usage:
    python3 dashboard.py          # startet auf Port 8080
    python3 dashboard.py 8888     # startet auf Port 8888

Zugriff von unterwegs via SSH-Tunnel:
    ssh -L 8080:localhost:8080 user@server
    dann im Browser: http://localhost:8080
"""

import os, sys, json, glob, base64, urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

BACKTEST_DIR = os.path.expanduser("~/.hermes_trader/backtests")
JOURNAL_PATH = os.path.expanduser("~/.hermes_trader/journal/trades_v3.jsonl")
STRATEGY_MAP_PATH = os.path.expanduser("~/.hermes_trader/engine/strategy_map_v3.json")
BASE_URL = "https://api.poloniex.com"

# Basic Auth — ändern!
AUTH_USER = "hermes"
AUTH_PASS = "trader2026"

_price_cache = {}

def api_get(endpoint):
    url = BASE_URL + endpoint
    req = urllib.request.Request(url, headers={"User-Agent": "HermesTrader/3.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}

def get_price(pair):
    """Cache-Preis für 10 Sekunden, um nicht bei jedem Refresh die API zu belasten."""
    now = datetime.now(timezone.utc).timestamp()
    if pair in _price_cache:
        price, ts = _price_cache[pair]
        if now - ts < 10:
            return price
    data = api_get(f"/markets/{pair}/price")
    try:
        price = float(data.get("price", 0))
    except Exception:
        price = 0
    _price_cache[pair] = (price, now)
    return price

def load_latest_backtest():
    files = glob.glob(os.path.join(BACKTEST_DIR, "backtest_360d_*.json"))
    if not files:
        return None
    latest = max(files, key=os.path.getmtime)
    try:
        with open(latest) as f:
            return json.load(f)
    except Exception:
        return None

def load_all_trades():
    if not os.path.exists(JOURNAL_PATH):
        return []
    trades = []
    try:
        with open(JOURNAL_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return trades

def load_recent_trades(n=25):
    all_t = load_all_trades()
    return all_t[-n:][::-1]

def load_open_trades():
    all_t = load_all_trades()
    return [t for t in all_t if t.get("status") == "OPEN"]

def load_strategy_map_pairs():
    try:
        with open(STRATEGY_MAP_PATH) as f:
            data = json.load(f)
        pairs = data.get("pairs", {})
        return {k: {"avoid": v.get("avoid", []), "sl": v.get("sl_atr_mult", "?"),
                    "tp": v.get("tp_atr_mult", "?"), "conf": v.get("min_confidence", "?")}
                for k, v in pairs.items()}
    except Exception:
        return {}

def fmt_ts(ms):
    if not ms:
        return "—"
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ms)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes Trader Dashboard</title>
<style>
:root {{ --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#c9d1d9; --muted:#8b949e; --green:#238636; --red:#da3633; --blue:#58a6ff; --yellow:#d29922; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; line-height:1.5; padding:20px; }}
h1 {{ font-size:1.6rem; margin-bottom:4px; }}
.sub {{ color:var(--muted); font-size:0.85rem; margin-bottom:20px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin-bottom:24px; }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; }}
.card h3 {{ font-size:0.75rem; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); margin-bottom:6px; }}
.card .big {{ font-size:1.8rem; font-weight:700; }}
.card .pos {{ color:var(--green); }}
.card .neg {{ color:var(--red); }}
.card .neu {{ color:var(--yellow); }}
.badge {{ display:inline-block; padding:2px 8px; border-radius:12px; font-size:0.75rem; font-weight:600; }}
.badge-pos {{ background:rgba(35,134,54,0.2); color:var(--green); }}
.badge-neg {{ background:rgba(218,54,51,0.2); color:var(--red); }}
.badge-neu {{ background:rgba(210,153,34,0.2); color:var(--yellow); }}
table {{ width:100%; border-collapse:collapse; font-size:0.85rem; margin-top:8px; }}
th, td {{ padding:8px 10px; text-align:left; border-bottom:1px solid var(--border); }}
th {{ color:var(--muted); font-weight:600; font-size:0.75rem; text-transform:uppercase; }}
tr:hover {{ background:rgba(255,255,255,0.03); }}
.win {{ color:var(--green); font-weight:600; }}
.loss {{ color:var(--red); font-weight:600; }}
.ts {{ color:var(--muted); font-size:0.8rem; }}
.pair-row {{ display:flex; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px solid var(--border); }}
.pair-row:last-child {{ border-bottom:none; }}
.pair-name {{ font-weight:600; }}
.pair-stat {{ color:var(--muted); font-size:0.85rem; }}
.section {{ margin-bottom:28px; }}
h2 {{ font-size:1.1rem; margin-bottom:10px; border-bottom:1px solid var(--border); padding-bottom:6px; }}
.refresh {{ color:var(--muted); font-size:0.8rem; float:right; }}
</style>
<meta http-equiv="refresh" content="60">
</head>
<body>
<h1>🧠 Hermes Trader Dashboard</h1>
<p class="sub">v3.3 Live | Paper Mode | Stand: {timestamp} UTC <span class="refresh">Auto-Refresh 60s</span></p>

<div class="grid">
  <div class="card">
    <h3>Profit Factor</h3>
    <div class="big {pf_cls}">{pf}</div>
  </div>
  <div class="card">
    <h3>Gesamt-PnL (360d)</h3>
    <div class="big {pnl_cls}">{pnl}%</div>
  </div>
  <div class="card">
    <h3>Win Rate</h3>
    <div class="big">{wr}%</div>
  </div>
  <div class="card">
    <h3>Trades</h3>
    <div class="big">{total}</div>
    <div style="font-size:0.8rem;color:var(--muted);margin-top:2px;">W:{wins} L:{losses} TS:{ts}</div>
  </div>
</div>

<div class="section">
  <h2>🔥 Offene Trades ({open_count})</h2>
  <table>
    <tr><th>Pair</th><th>Side</th><th>Setup</th><th>Entry</th><th>Aktuell</th><th>Unreal PnL</th><th>SL</th><th>TP</th><th>Zeit offen</th></tr>
    {open_rows}
  </table>
</div>

<div class="section">
  <h2>📊 Per Pair (360d Backtest)</h2>
  {pair_rows}
</div>

<div class="section">
  <h2>📓 Letzte Trades</h2>
  <table>
    <tr><th>Zeit</th><th>Pair</th><th>Side</th><th>Setup</th><th>Entry</th><th>Exit</th><th>PnL %</th><th>Result</th></tr>
    {trade_rows}
  </table>
</div>

<div class="section">
  <h2>⚙️ Konfiguration</h2>
  <div class="card">
    {config_rows}
  </div>
</div>

</body>
</html>
"""

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return

        auth = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()
        if auth != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Hermes"')
            self.end_headers()
            return

        bt = load_latest_backtest()
        recent = load_recent_trades(25)
        open_trades = load_open_trades()
        cfg_pairs = load_strategy_map_pairs()

        if bt:
            total = bt.get("total", {})
            pf = total.get("profit_factor", 0)
            pnl = total.get("total_pnl_pct", 0)
            wr = total.get("win_rate", 0)
            trades_n = total.get("total_trades", 0)
            wins = total.get("wins", 0)
            losses = total.get("losses", 0)
            tss = total.get("time_stops", 0)
            pair_data = bt.get("pairs", {})
        else:
            pf = pnl = wr = trades_n = wins = losses = tss = 0
            pair_data = {}

        pf_cls = "pos" if pf >= 1.5 else ("neu" if pf >= 1.0 else "neg")
        pnl_cls = "pos" if pnl >= 0 else "neg"

        # Open trades with unrealized PnL
        open_rows_html = ""
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        for t in open_trades:
            pair = t.get("pair", "?")
            direction = t.get("direction", "?")
            setup = t.get("setup_type", t.get("setup", "?"))
            entry = float(t.get("entry", t.get("entry_price", 0)))
            sl = t.get("stop_loss", "?")
            tp = t.get("take_profit", "?")
            entry_time = t.get("entry_time", t.get("open_time", 0))
            price = get_price(pair)
            if price > 0 and entry > 0:
                if direction == "LONG":
                    unreal = (price - entry) / entry * 100
                else:
                    unreal = (entry - price) / entry * 100
                ucls = "badge-pos" if unreal >= 0 else "badge-neg"
                unreal_str = f'<span class="badge {ucls}">{unreal:+.2f}%</span>'
            else:
                unreal_str = '<span class="badge badge-neu">N/A</span>'
            if entry_time:
                mins = int((now_ms - entry_time) / 60000)
                time_str = f"{mins} min" if mins < 60 else f"{mins//60}h {mins%60}m"
            else:
                time_str = "—"
            price_str = f"{price:.4f}" if price else "—"
            open_rows_html += f'<tr><td>{pair}</td><td>{direction}</td><td>{setup}</td><td>{entry:.4f}</td><td>{price_str}</td><td>{unreal_str}</td><td>{sl}</td><td>{tp}</td><td class="ts">{time_str}</td></tr>'
        if not open_trades:
            open_rows_html = '<tr><td colspan="9" style="text-align:center;color:var(--muted);">Keine offenen Trades</td></tr>'

        # Pair rows
        pair_rows_html = ""
        for pair, stats in pair_data.items():
            t = stats.get("total_trades", 0)
            p = stats.get("profit_factor", 0)
            w = stats.get("win_rate", 0)
            n = stats.get("total_pnl_pct", 0)
            pcls = "pos" if p >= 1.5 else ("neu" if p >= 1.0 else "neg")
            ncls = "pos" if n >= 0 else "neg"
            pair_rows_html += f'<div class="pair-row"><span class="pair-name">{pair}</span><span class="pair-stat">Trades:{t} | PF:<span class="{pcls}">{p}</span> | WR:{w}% | PnL:<span class="{ncls}">{n:+}%</span></span></div>'

        # Closed trade rows
        trade_rows_html = ""
        for tr in recent:
            tm = fmt_ts(tr.get("close_time") or tr.get("entry_time"))
            pair = tr.get("pair", "?")
            side = tr.get("side", tr.get("direction", "?"))
            setup = tr.get("setup", tr.get("setup_type", "?"))
            entry = tr.get("entry_price", tr.get("entry", 0))
            exit_p = tr.get("exit_price", 0)
            pnl_t = tr.get("pnl_pct", 0)
            res = tr.get("result", "OPEN")
            rcls = "win" if res == "WIN" else ("loss" if res == "LOSS" else "")
            trade_rows_html += f'<tr><td class="ts">{tm}</td><td>{pair}</td><td>{side}</td><td>{setup}</td><td>{entry}</td><td>{exit_p}</td><td class="{rcls}">{pnl_t:+.2f}%</td><td class="{rcls}">{res}</td></tr>'
        if not recent:
            trade_rows_html = '<tr><td colspan="8" style="text-align:center;color:var(--muted);">Keine Trade-Logs gefunden</td></tr>'

        # Config rows
        config_rows_html = ""
        for pair, c in cfg_pairs.items():
            av = len(c["avoid"])
            config_rows_html += f'<div class="pair-row"><span class="pair-name">{pair}</span><span class="pair-stat">SL:{c["sl"]}x | TP:{c["tp"]}x | Conf:{c["conf"]} | Avoid:{av} Strategien</span></div>'

        html = HTML_TEMPLATE.format(
            timestamp=datetime.now(timezone.utc).strftime("%H:%M:%S"),
            pf=pf, pf_cls=pf_cls,
            pnl=round(pnl, 2), pnl_cls=pnl_cls,
            wr=wr, total=trades_n, wins=wins, losses=losses, ts=tss,
            open_count=len(open_trades),
            open_rows=open_rows_html,
            pair_rows=pair_rows_html,
            trade_rows=trade_rows_html,
            config_rows=config_rows_html,
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"🧠 Hermes Dashboard läuft auf http://0.0.0.0:{port}")
    print(f"   Login: {AUTH_USER} / {AUTH_PASS}")
    print(f"   SSH-Tunnel: ssh -L {port}:localhost:{port} user@server")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Beendet.")
