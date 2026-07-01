#!/usr/bin/env python3
"""
Hermes Trader Dashboard v2 — Erweitertes Web-Dashboard.

Zeigt:
  • alle Trades (offen + geschlossen) mit Filter nach Zeitraum, Pair, Setup, Resultat
  • umfangreiche Statistiken (PnL, PF, WR, Expectancy, durchschn. Gewinn/Verlust etc.)
  • Per-Pair- und Per-Setup-Auswertungen
  • Diagramm: y-Achse = PnL %, x-Achse = Zeit
    Jeder Trade ist eine separate Linie, eingefärbt nach Pair.
    Die stündlichen Zwischenwerte werden aus den gecachten H1-Candles
    (bzw. live von Poloniex für den aktuellen Zeitraum) berechnet.

Technisch: stdlib-only Python, Chart.js via CDN.

Usage:
    python3 dashboard.py          # Port 8080
    python3 dashboard.py 8888     # Port 8888

SSH-Tunnel:
    ssh -L 8080:localhost:8080 user@server
"""

import os, sys, json, glob, base64, urllib.request, math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

JOURNAL_PATH = os.path.expanduser("~/.hermes_trader/journal/trades_v3.jsonl")
BACKTEST_DIR = os.path.expanduser("~/.hermes_trader/backtests")
CACHE_DIR = os.path.expanduser("~/.hermes_trader/backtests/cache")
STRATEGY_MAP_PATH = os.path.expanduser("~/.hermes_trader/engine/strategy_map_v3.json")
BASE_URL = "https://api.poloniex.com"

AUTH_USER = "hermes"
AUTH_PASS = "trader2026"

_price_cache = {}
_candle_cache = {}

PAIR_COLORS = {
    "BTC_USDT": "#F7931A", "ETH_USDT": "#627EEA", "SOL_USDT": "#14F195",
    "XRP_USDT": "#A0AEC0", "ADA_USDT": "#0033AD", "DOGE_USDT": "#C2A633",
    "LTC_USDT": "#345D9D", "LINK_USDT": "#2A5ADA", "AVAX_USDT": "#E84142",
    "NEAR_USDT": "#00C08B"
}
DEFAULT_COLORS = ["#58a6ff", "#238636", "#da3633", "#d29922", "#a371f7",
                  "#3fb950", "#f85149", "#bd561d", "#79c0ff", "#d2a8ff"]


def api_get(endpoint):
    url = BASE_URL + endpoint
    req = urllib.request.Request(url, headers={"User-Agent": "HermesTrader/3.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def parse_ts(value):
    """Wandelt ISO-String oder Millisekunden-Integer in ms-Timestamp um."""
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


def fmt_ts(ms, fmt="%Y-%m-%d %H:%M"):
    if not ms:
        return "—"
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(fmt)
    except Exception:
        return str(ms)


def fmt_dt(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso)


def fmt_pct(v, signed=True):
    try:
        v = float(v)
    except Exception:
        return "—"
    return f"{v:+.2f}%" if signed else f"{v:.2f}%"


def fmt_pf(v):
    try:
        v = float(v)
    except Exception:
        return "—"
    if math.isinf(v):
        return "∞"
    return f"{v:.2f}"


def get_price(pair):
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


def load_all_trades():
    if not os.path.exists(JOURNAL_PATH):
        return []
    trades = []
    try:
        with open(JOURNAL_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return trades


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


def load_cached_candles(pair, interval="HOUR_1", start_ms=None, end_ms=None):
    key = (pair, interval, start_ms, end_ms)
    if key in _candle_cache:
        return _candle_cache[key]
    files = glob.glob(os.path.join(CACHE_DIR, f"{pair}_{interval}_*.json"))
    merged = {}
    for f in files:
        # Nur Dateien laden, deren Zeitraum mit dem gewünschten überlappt
        if start_ms is not None and end_ms is not None:
            try:
                parts = os.path.basename(f).replace(".json", "").split("_")
                f_start = int(parts[-2])
                f_end = int(parts[-1])
                if f_end < start_ms or f_start > end_ms:
                    continue
            except Exception:
                pass
        try:
            with open(f) as fh:
                data = json.load(fh)
            for c in data:
                try:
                    ts = int(c[9])
                    if start_ms is not None and end_ms is not None:
                        if ts < start_ms or ts > end_ms:
                            continue
                    merged[ts] = c
                except Exception:
                    pass
        except Exception:
            pass
    candles = sorted(merged.values(), key=lambda x: x[9])
    _candle_cache[key] = candles
    return candles


def fetch_candles_api(pair, interval, start_ms, end_ms):
    ep = (f"/markets/{pair}/candles?interval={interval}"
          f"&startTime={start_ms}&endTime={end_ms}&limit=500")
    data = api_get(ep)
    if isinstance(data, list):
        return sorted(data, key=lambda x: x[9])
    return []


def get_candles_range(pair, start_ms, end_ms, interval="HOUR_1"):
    """Liefert H1-Candles für einen Zeitraum (Cache + API)."""
    cached = load_cached_candles(pair, interval, start_ms, end_ms)
    merged = {}
    for c in cached:
        try:
            ts = int(c[9])
            if start_ms <= ts <= end_ms:
                merged[ts] = c
        except Exception:
            pass
    # Fehlende Daten am rechten Rand live nachladen
    latest_cached = max(merged) if merged else start_ms - 1
    if latest_cached < end_ms:
        fetch_start = latest_cached + 1
        fetched = fetch_candles_api(pair, interval, fetch_start, end_ms)
        for c in fetched:
            try:
                ts = int(c[9])
                if start_ms <= ts <= end_ms and ts not in merged:
                    merged[ts] = c
            except Exception:
                pass
    return sorted(merged.values(), key=lambda x: x[9])


def color_for_pair(pair):
    return PAIR_COLORS.get(pair) or DEFAULT_COLORS[hash(pair) % len(DEFAULT_COLORS)]


_fetched_candles = {}  # (pair, start_ms, end_ms) -> candles


def trade_duration_min(trade):
    start = parse_ts(trade.get("timestamp") or trade.get("entry_time"))
    end = parse_ts(trade.get("close_time")) if trade.get("close_time") else int(datetime.now(timezone.utc).timestamp() * 1000)
    return max(0, end - start) / 60000


def trade_duration_str(mins):
    if mins < 60:
        return f"{int(mins)}m"
    return f"{int(mins // 60)}h {int(mins % 60)}m"


def filter_trades(trades, qs):
    from_str = qs.get("from", [""])[0]
    to_str = qs.get("to", [""])[0]
    pair = qs.get("pair", [""])[0]
    setup = qs.get("setup", [""])[0]
    result = qs.get("result", [""])[0]

    now = datetime.now(timezone.utc)
    if from_str:
        try:
            from_dt = datetime.strptime(from_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            from_ms = int(from_dt.timestamp() * 1000)
        except Exception:
            from_ms = 0
    else:
        from_ms = 0
    if to_str:
        try:
            to_dt = datetime.strptime(to_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            to_ms = int(to_dt.timestamp() * 1000)
        except Exception:
            to_ms = int(now.timestamp() * 1000)
    else:
        to_ms = int(now.timestamp() * 1000)

    filtered = []
    for t in trades:
        ts = parse_ts(t.get("timestamp") or t.get("entry_time"))
        if not (from_ms <= ts <= to_ms):
            continue
        if pair and t.get("pair") != pair:
            continue
        if setup and t.get("setup_type", t.get("setup", "")) != setup:
            continue
        if result and t.get("result") != result:
            continue
        filtered.append(t)
    return filtered


def compute_stats(trades):
    total = len(trades)
    if total == 0:
        return {
            "total": 0, "wins": 0, "losses": 0, "time_stops": 0,
            "win_rate": 0, "profit_factor": 0, "total_pnl": 0,
            "avg_win": 0, "avg_loss": 0, "max_win": 0, "max_loss": 0,
            "expectancy": 0, "avg_duration_min": 0
        }

    wins = [t for t in trades if t.get("result") == "WIN"]
    losses = [t for t in trades if t.get("result") == "LOSS"]
    tss = [t for t in trades if t.get("result") == "TIME_STOP" or t.get("time_stop")]

    win_pnl = [t.get("pnl_pct", 0) for t in wins]
    loss_pnl = [t.get("pnl_pct", 0) for t in losses]
    all_pnl = [t.get("pnl_pct", 0) for t in trades]

    total_pnl = sum(all_pnl)
    gross_profit = sum(p for p in win_pnl if p > 0)
    gross_loss = abs(sum(p for p in loss_pnl if p < 0))
    if gross_loss:
        pf = gross_profit / gross_loss
    elif gross_profit:
        pf = float('inf')
    else:
        pf = 0.0

    durations = [trade_duration_min(t) for t in trades]

    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "time_stops": len(tss),
        "win_rate": len(wins) / total * 100,
        "profit_factor": pf,
        "total_pnl": total_pnl,
        "avg_win": sum(win_pnl) / len(win_pnl) if win_pnl else 0,
        "avg_loss": sum(loss_pnl) / len(loss_pnl) if loss_pnl else 0,
        "max_win": max(win_pnl) if win_pnl else 0,
        "max_loss": min(loss_pnl) if loss_pnl else 0,
        "expectancy": total_pnl / total,
        "avg_duration_min": sum(durations) / len(durations) if durations else 0
    }


def per_pair_stats(trades):
    pairs = {}
    for t in trades:
        p = t.get("pair", "?")
        pairs.setdefault(p, []).append(t)
    out = []
    for p, ts in sorted(pairs.items()):
        s = compute_stats(ts)
        out.append({"pair": p, **s})
    return out


def per_setup_stats(trades):
    setups = {}
    for t in trades:
        s = t.get("setup_type", t.get("setup", "?"))
        setups.setdefault(s, []).append(t)
    out = []
    for s, ts in sorted(setups.items()):
        st = compute_stats(ts)
        out.append({"setup": s, **st})
    return out


def color_for_pair(pair):
    return PAIR_COLORS.get(pair) or DEFAULT_COLORS[hash(pair) % len(DEFAULT_COLORS)]


_fetched_candles = {}  # (pair, start_ms, end_ms) -> candles


def build_chart_datasets(trades):
    if not trades:
        return []

    # Pro Pair den benötigten Gesamtzeitraum bestimmen und Candles einmal laden
    pair_ranges = {}
    for t in trades:
        pair = t.get("pair")
        if not pair:
            continue
        entry_time = parse_ts(t.get("timestamp") or t.get("entry_time"))
        close_time_raw = t.get("close_time")
        close_time = parse_ts(close_time_raw) if close_time_raw else int(datetime.now(timezone.utc).timestamp() * 1000)
        if pair not in pair_ranges:
            pair_ranges[pair] = [entry_time, close_time]
        else:
            pair_ranges[pair][0] = min(pair_ranges[pair][0], entry_time)
            pair_ranges[pair][1] = max(pair_ranges[pair][1], close_time)

    pair_candles = {}
    def _fetch(args):
        pair, start_ms, end_ms = args
        return pair, get_candles_range(pair, start_ms, end_ms)

    with ThreadPoolExecutor(max_workers=5) as exe:
        futures = {exe.submit(_fetch, (pair, r[0], r[1])): pair for pair, r in pair_ranges.items()}
        for fut in as_completed(futures):
            pair, candles = fut.result()
            pair_candles[pair] = candles
            _fetched_candles[(pair, pair_ranges[pair][0], pair_ranges[pair][1])] = candles

    datasets = []
    for t in trades:
        pair = t.get("pair", "?")
        if pair not in pair_candles:
            continue
        series = compute_trade_series_from_candles(t, pair_candles[pair])
        if not series:
            continue
        setup = t.get("setup_type", t.get("setup", "?"))
        direction = t.get("direction", "?")
        color = color_for_pair(pair)
        datasets.append({
            "label": f"{pair} {direction} {setup} @ {fmt_dt(t.get('timestamp'))}",
            "data": series,
            "borderColor": color,
            "backgroundColor": color,
            "borderWidth": 1.5,
            "pointRadius": 0,
            "pointHoverRadius": 4,
            "tension": 0,
            "pair": pair
        })
    return datasets


def compute_trade_series_from_candles(trade, candles):
    pair = trade.get("pair")
    direction = trade.get("direction", "LONG")
    entry = float(trade.get("entry", trade.get("entry_price", 0)))
    entry_time = parse_ts(trade.get("timestamp") or trade.get("entry_time"))
    close_time_raw = trade.get("close_time")
    close_time = parse_ts(close_time_raw) if close_time_raw else None

    if entry <= 0 or entry_time <= 0 or not pair:
        return []

    end_ms = close_time or int(datetime.now(timezone.utc).timestamp() * 1000)
    series = [{"x": entry_time, "y": 0.0}]
    seen = {entry_time}
    for c in candles:
        try:
            ts = int(c[9])
            if ts < entry_time or ts > end_ms or ts in seen:
                continue
            close = float(c[3])
            if direction == "LONG":
                pnl = (close - entry) / entry * 100
            else:
                pnl = (entry - close) / entry * 100
            series.append({"x": ts, "y": round(pnl, 4)})
            seen.add(ts)
        except Exception:
            pass

    if not close_time_raw:
        price = get_price(pair)
        if price > 0:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            if direction == "LONG":
                pnl = (price - entry) / entry * 100
            else:
                pnl = (entry - price) / entry * 100
            if now_ms not in seen:
                series.append({"x": now_ms, "y": round(pnl, 4)})

    return sorted(series, key=lambda p: p["x"])


def build_filter_options(trades):
    pairs = sorted({t.get("pair", "?") for t in trades})
    setups = sorted({t.get("setup_type", t.get("setup", "?")) for t in trades})
    results = sorted({t.get("result") for t in trades if t.get("result")})
    return pairs, setups, results


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes Trader Dashboard v2</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root {{ --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#c9d1d9; --muted:#8b949e; --green:#238636; --red:#da3633; --blue:#58a6ff; --yellow:#d29922; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; line-height:1.5; padding:20px; }}
h1 {{ font-size:1.6rem; margin-bottom:4px; }}
.sub {{ color:var(--muted); font-size:0.85rem; margin-bottom:18px; }}
a {{ color:var(--blue); }}

.filters {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px; margin-bottom:20px; display:flex; flex-wrap:wrap; gap:12px; align-items:end; }}
.filters label {{ font-size:0.75rem; color:var(--muted); display:block; margin-bottom:3px; }}
.filters input, .filters select {{ background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:6px 8px; font-size:0.85rem; }}
.filters button {{ background:var(--blue); color:#fff; border:none; border-radius:6px; padding:7px 14px; font-size:0.85rem; cursor:pointer; }}
.filters button.secondary {{ background:var(--border); color:var(--text); }}
.quick-links {{ display:flex; gap:8px; flex-wrap:wrap; }}
.quick-links a {{ font-size:0.75rem; text-decoration:none; background:var(--bg); border:1px solid var(--border); border-radius:4px; padding:3px 8px; }}

.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px; margin-bottom:22px; }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px; }}
.card h3 {{ font-size:0.7rem; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); margin-bottom:5px; }}
.card .big {{ font-size:1.6rem; font-weight:700; }}
.card .pos {{ color:var(--green); }}
.card .neg {{ color:var(--red); }}
.card .neu {{ color:var(--yellow); }}
.card .small {{ font-size:0.8rem; color:var(--muted); margin-top:3px; }}

.section {{ margin-bottom:28px; }}
h2 {{ font-size:1.1rem; margin-bottom:10px; border-bottom:1px solid var(--border); padding-bottom:6px; }}

.chart-wrap {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px; position:relative; height:520px; }}
.legend {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:10px; }}
.legend span {{ font-size:0.75rem; display:flex; align-items:center; gap:5px; }}
.legend i {{ width:10px; height:10px; border-radius:50%; display:inline-block; }}

.table-wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--border); border-radius:10px; }}
table {{ width:100%; border-collapse:collapse; font-size:0.82rem; }}
th, td {{ padding:8px 10px; text-align:left; border-bottom:1px solid var(--border); white-space:nowrap; }}
th {{ color:var(--muted); font-weight:600; font-size:0.72rem; text-transform:uppercase; }}
tr:hover {{ background:rgba(255,255,255,0.03); }}
.win {{ color:var(--green); font-weight:600; }}
.loss {{ color:var(--red); font-weight:600; }}
.neutral {{ color:var(--yellow); }}
.ts {{ color:var(--muted); font-size:0.78rem; }}
.badge {{ display:inline-block; padding:2px 8px; border-radius:12px; font-size:0.7rem; font-weight:600; }}
.badge-pos {{ background:rgba(35,134,54,0.2); color:var(--green); }}
.badge-neg {{ background:rgba(218,54,51,0.2); color:var(--red); }}
.badge-neu {{ background:rgba(210,153,34,0.2); color:var(--yellow); }}

.pair-pill {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:0.75rem; font-weight:600; background:var(--bg); border:1px solid var(--border); }}

.two-col {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; }}
.mini-table {{ width:100%; font-size:0.8rem; }}
.mini-table th, .mini-table td {{ padding:6px 8px; }}
</style>
</head>
<body>
<h1>🧠 Hermes Trader Dashboard v2</h1>
<p class="sub">Paper Mode | Stand: {timestamp} UTC | Trades: {filtered_count} / {total_count}</p>

<form class="filters" method="get">
  <div>
    <label>Von</label>
    <input type="date" name="from" value="{f_from}">
  </div>
  <div>
    <label>Bis</label>
    <input type="date" name="to" value="{f_to}">
  </div>
  <div>
    <label>Pair</label>
    <select name="pair"><option value="">Alle</option>{pair_options}</select>
  </div>
  <div>
    <label>Setup</label>
    <select name="setup"><option value="">Alle</option>{setup_options}</select>
  </div>
  <div>
    <label>Resultat</label>
    <select name="result"><option value="">Alle</option>{result_options}</select>
  </div>
  <button type="submit">Filtern</button>
  <button type="button" class="secondary" onclick="window.location.href='/'">Zurücksetzen</button>
  <div class="quick-links">
    <a href="?from={q_7d}&to={q_today}">7 Tage</a>
    <a href="?from={q_30d}&to={q_today}">30 Tage</a>
    <a href="?from={q_90d}&to={q_today}">90 Tage</a>
    <a href="?from={q_ytd}&to={q_today}">YTD</a>
    <a href="/">Alle</a>
  </div>
</form>

<div class="grid">
  <div class="card"><h3>Profit Factor</h3><div class="big {pf_cls}">{pf_str}</div></div>
  <div class="card"><h3>Gesamt-PnL</h3><div class="big {pnl_cls}">{total_pnl:+.2f}%</div></div>
  <div class="card"><h3>Win Rate</h3><div class="big">{wr:.1f}%</div><div class="small">{wins}W / {losses}L / {ts}TS</div></div>
  <div class="card"><h3>Trades</h3><div class="big">{total}</div></div>
  <div class="card"><h3>Ø Gewinn</h3><div class="big pos">{avg_win:+.2f}%</div></div>
  <div class="card"><h3>Ø Verlust</h3><div class="big neg">{avg_loss:+.2f}%</div></div>
  <div class="card"><h3>Max Gewinn</h3><div class="big pos">{max_win:+.2f}%</div></div>
  <div class="card"><h3>Max Verlust</h3><div class="big neg">{max_loss:+.2f}%</div></div>
  <div class="card"><h3>Expectancy</h3><div class="big {exp_cls}">{expectancy:+.2f}%</div></div>
  <div class="card"><h3>Ø Dauer</h3><div class="big">{avg_dur}</div></div>
</div>

<div class="section">
  <h2>📈 PnL-Verlauf pro Trade (%)</h2>
  <div class="chart-wrap">
    <div id="chartLoader" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:0.9rem;z-index:2;background:var(--card);">Diagramm wird berechnet...</div>
    <div class="legend">{pair_legend}</div>
    <canvas id="pnlChart"></canvas>
  </div>
  <p class="sub" style="margin-top:8px;">Jede Linie = ein Trade. X = Zeit, Y = Gewinn/Verlust in %. Farbe = Pair. Stündliche Zwischenwerte aus H1-Candles.</p>
</div>

<div class="section">
  <h2>📓 Trades ({filtered_count})</h2>
  <div class="table-wrap">
    <table>
      <tr><th>Zeit</th><th>Pair</th><th>Side</th><th>Setup</th><th>Entry</th><th>Exit</th><th>PnL %</th><th>Result</th><th>Dauer</th><th>Regime</th></tr>
      {trade_rows}
    </table>
  </div>
</div>

<div class="two-col">
  <div class="section">
    <h2>📊 Per Pair</h2>
    <div class="table-wrap">
      <table class="mini-table">
        <tr><th>Pair</th><th>Trades</th><th>WR</th><th>PF</th><th>PnL</th></tr>
        {pair_rows}
      </table>
    </div>
  </div>
  <div class="section">
    <h2>⚙️ Per Setup</h2>
    <div class="table-wrap">
      <table class="mini-table">
        <tr><th>Setup</th><th>Trades</th><th>WR</th><th>PF</th><th>PnL</th></tr>
        {setup_rows}
      </table>
    </div>
  </div>
</div>

<script>
const ctx = document.getElementById('pnlChart').getContext('2d');
const datasets = {chart_data};
const pairColors = {pair_colors_json};

function lighten(hex, pct) {{
  const num = parseInt(hex.replace('#',''), 16);
  const r = Math.min(255, (num >> 16) + pct);
  const g = Math.min(255, ((num >> 8) & 0x00FF) + pct);
  const b = Math.min(255, (num & 0x0000FF) + pct);
  return `rgba(${{r}},${{g}},${{b}},0.1)`;
}}

datasets.forEach(ds => {{
  const c = pairColors[ds.pair] || '#58a6ff';
  ds.borderColor = c;
  ds.backgroundColor = lighten(c, 40);
  ds.borderWidth = 1.6;
  ds.pointRadius = 0;
  ds.pointHoverRadius = 4;
  ds.tension = 0.1;
  ds.fill = false;
}});

new Chart(ctx, {{
  type: 'line',
  data: {{ datasets: datasets }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    interaction: {{ mode: 'nearest', axis: 'x', intersect: false }},
    scales: {{
      x: {{
        type: 'linear',
        title: {{ display: true, text: 'Zeit (UTC)', color: '#8b949e' }},
        ticks: {{
          color: '#8b949e',
          maxTicksLimit: 8,
          callback: function(v) {{ return new Date(v).toLocaleString('de-DE', {{month:'short',day:'numeric',hour:'2-digit'}}); }}
        }},
        grid: {{ color: 'rgba(255,255,255,0.06)' }}
      }},
      y: {{
        title: {{ display: true, text: 'PnL %', color: '#8b949e' }},
        ticks: {{ color: '#8b949e', callback: function(v){{ return v.toFixed(1)+'%'; }} }},
        grid: {{ color: 'rgba(255,255,255,0.06)' }}
      }}
    }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        callbacks: {{
          title: function(ctx) {{ return new Date(ctx[0].parsed.x).toLocaleString('de-DE'); }},
          label: function(ctx) {{ return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(2) + '%'; }}
        }}
      }}
    }},
    animation: {{
      onComplete: function() {{
        const loader = document.getElementById('chartLoader');
        if (loader) loader.style.display = 'none';
      }}
    }}
  }}
}});
</script>

</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path != "/":
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

        all_trades = load_all_trades()
        total_count = len(all_trades)
        pairs, setups, results = build_filter_options(all_trades)
        trades = filter_trades(all_trades, qs)
        stats = compute_stats(trades)
        pp = per_pair_stats(trades)
        ps = per_setup_stats(trades)

        # Backtest-Gesamtstatistik als Fallback/Ergänzung
        bt = load_latest_backtest()
        if bt and not trades:
            bt_total = bt.get("total", {})
            stats = {
                "total": bt_total.get("total_trades", 0),
                "wins": bt_total.get("wins", 0),
                "losses": bt_total.get("losses", 0),
                "time_stops": bt_total.get("time_stops", 0),
                "win_rate": bt_total.get("win_rate", 0),
                "profit_factor": bt_total.get("profit_factor", 0),
                "total_pnl": bt_total.get("total_pnl_pct", 0),
                "avg_win": 0, "avg_loss": 0, "max_win": 0, "max_loss": 0,
                "expectancy": 0, "avg_duration_min": 0
            }
            pp = []
            ps = []

        # Chart-Daten
        chart_datasets = build_chart_datasets(trades)

        # Filter-Optionen
        selected_pair = qs.get("pair", [""])[0]
        selected_setup = qs.get("setup", [""])[0]
        selected_result = qs.get("result", [""])[0]

        pair_options = "".join(f'<option value="{p}"{" selected" if p == selected_pair else ""}>{p}</option>' for p in pairs)
        setup_options = "".join(f'<option value="{s}"{" selected" if s == selected_setup else ""}>{s}</option>' for s in setups)
        result_options = "".join(f'<option value="{r}"{" selected" if r == selected_result else ""}>{r}</option>' for r in results)

        # Quick-Link-Daten
        now = datetime.now(timezone.utc)
        q_today = now.strftime("%Y-%m-%d")
        q_7d = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        q_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        q_90d = (now - timedelta(days=90)).strftime("%Y-%m-%d")
        q_ytd = datetime(now.year, 1, 1, tzinfo=timezone.utc).strftime("%Y-%m-%d")

        f_from = qs.get("from", [""])[0]
        f_to = qs.get("to", [""])[0]

        # Pair-Legende
        pair_legend = ""
        shown_pairs = set()
        for p in pairs:
            if p not in shown_pairs:
                c = color_for_pair(p)
                pair_legend += f'<span><i style="background:{c}"></i>{p}</span>'
                shown_pairs.add(p)

        # Trade-Tabelle
        trade_rows = ""
        for t in trades[::-1]:  # neueste zuerst
            tm = fmt_dt(t.get("timestamp") or t.get("entry_time"))
            pair = t.get("pair", "?")
            side = t.get("direction", "?")
            setup = t.get("setup_type", t.get("setup", "?"))
            entry = t.get("entry", t.get("entry_price", "?"))
            exit_p = t.get("exit_price", "—")
            pnl = t.get("pnl_pct", 0) if t.get("status") == "CLOSED" else None
            result = t.get("result", "OPEN") or "OPEN"
            duration = trade_duration_str(trade_duration_min(t))
            regime = t.get("regime", "?")

            if pnl is not None:
                pnl_cls = "win" if pnl > 0 else ("loss" if pnl < 0 else "neutral")
                pnl_str = f"{pnl:+.2f}%"
            else:
                pnl_cls = "neutral"
                pnl_str = "offen"
            rcls = "win" if result == "WIN" else ("loss" if result == "LOSS" else ("neutral" if result == "TIME_STOP" else ""))

            trade_rows += (
                f'<tr><td class="ts">{tm}</td>'
                f'<td><span class="pair-pill" style="border-color:{color_for_pair(pair)}">{pair}</span></td>'
                f'<td>{side}</td><td>{setup}</td><td>{entry}</td><td>{exit_p}</td>'
                f'<td class="{pnl_cls}">{pnl_str}</td><td class="{rcls}">{result}</td>'
                f'<td class="ts">{duration}</td><td class="ts">{regime}</td></tr>'
            )
        if not trade_rows:
            trade_rows = '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:20px;">Keine Trades im gewählten Zeitraum</td></tr>'

        # Per-Pair / Per-Setup Zeilen
        pair_rows = ""
        for s in pp:
            pcls = "win" if s["profit_factor"] >= 1.5 else ("neutral" if s["profit_factor"] >= 1.0 else "loss")
            ncls = "win" if s["total_pnl"] >= 0 else "loss"
            pair_rows += (
                f'<tr><td><b>{s["pair"]}</b></td>'
                f'<td>{s["total"]}</td><td>{s["win_rate"]:.1f}%</td>'
                f'<td class="{pcls}">{fmt_pf(s["profit_factor"])}</td>'
                f'<td class="{ncls}">{s["total_pnl"]:+.2f}%</td></tr>'
            )

        setup_rows = ""
        for s in ps:
            pcls = "win" if s["profit_factor"] >= 1.5 else ("neutral" if s["profit_factor"] >= 1.0 else "loss")
            ncls = "win" if s["total_pnl"] >= 0 else "loss"
            setup_rows += (
                f'<tr><td><b>{s["setup"]}</b></td>'
                f'<td>{s["total"]}</td><td>{s["win_rate"]:.1f}%</td>'
                f'<td class="{pcls}">{fmt_pf(s["profit_factor"])}</td>'
                f'<td class="{ncls}">{s["total_pnl"]:+.2f}%</td></tr>'
            )

        pf_cls = "pos" if stats["profit_factor"] >= 1.5 else ("neu" if stats["profit_factor"] >= 1.0 else "neg")
        pnl_cls = "pos" if stats["total_pnl"] >= 0 else "neg"
        exp_cls = "pos" if stats["expectancy"] >= 0 else "neg"
        pf_str = fmt_pf(stats["profit_factor"])

        html = HTML_TEMPLATE.format(
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            filtered_count=len(trades),
            total_count=total_count,
            f_from=f_from,
            f_to=f_to,
            pair_options=pair_options,
            setup_options=setup_options,
            result_options=result_options,
            q_today=q_today,
            q_7d=q_7d,
            q_30d=q_30d,
            q_90d=q_90d,
            q_ytd=q_ytd,
            pf=stats["profit_factor"],
            pf_str=pf_str,
            pf_cls=pf_cls,
            total_pnl=stats["total_pnl"],
            pnl_cls=pnl_cls,
            wr=stats["win_rate"],
            wins=stats["wins"],
            losses=stats["losses"],
            ts=stats["time_stops"],
            total=stats["total"],
            avg_win=stats["avg_win"],
            avg_loss=stats["avg_loss"],
            max_win=stats["max_win"],
            max_loss=stats["max_loss"],
            expectancy=stats["expectancy"],
            exp_cls=exp_cls,
            avg_dur=trade_duration_str(stats["avg_duration_min"]),
            pair_legend=pair_legend,
            chart_data=json.dumps(chart_datasets),
            pair_colors_json=json.dumps(PAIR_COLORS),
            trade_rows=trade_rows,
            pair_rows=pair_rows,
            setup_rows=setup_rows,
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"🧠 Hermes Dashboard v2 läuft auf http://0.0.0.0:{port}")
    print(f"   Login: {AUTH_USER} / {AUTH_PASS}")
    print(f"   SSH-Tunnel: ssh -L {port}:localhost:{port} user@server")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Beendet.")
