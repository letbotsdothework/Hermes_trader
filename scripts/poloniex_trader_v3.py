#!/usr/bin/env python3
"""
Hermes Autonomer Crypto Trading Agent v3.0 — Regime-Adaptive
- Per-pair Regime Detection (ADX + BBW + EMA Slope)
- Dynamic Strategy Selection per Regime
- Soft Confidence Modulation (no hard avoid-lists)
- Multi-Factor Confluence Boost
- Walk-Forward Learning with Regime Context
"""

import urllib.request
import urllib.error
import json
import math
import time
import datetime
from datetime import timezone
import hashlib
import hmac
import os
import sys
from collections import defaultdict
import base64

# ---------------------------------------------------------------------------
# DEFAULT CONFIG
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "paper_mode": True,
    "risk_per_trade_pct": 1.0,
    "max_daily_loss_pct": 3.0,
    "min_confidence": 55,
    "pairs": ["BTC_USDT", "ETH_USDT", "NEAR_USDT", "DOGE_USDT"]
}

# ---------------------------------------------------------------------------
# PFADE
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.expanduser("~/.hermes_trader/config.json")
JOURNAL_PATH = os.path.expanduser("~/.hermes_trader/journal/trades_v3.jsonl")
LEARN_PATH = os.path.expanduser("~/.hermes_trader/journal/learning_v3.json")
STRATEGY_MAP_PATH = os.path.expanduser("~/.hermes_trader/engine/strategy_map_v3.json")
LOG_PATH = os.path.expanduser("~/.hermes_trader/logs/agent_v3.log")
BASE_URL = "https://api.poloniex.com"

# ---------------------------------------------------------------------------
# KORRELATIONS-CLUSTER
# ---------------------------------------------------------------------------
CORR_CLUSTERS = {
    "BTC_USDT": "A", "ETH_USDT": "A",
    "NEAR_USDT": "B",
    "DOGE_USDT": "C"
}

# ---------------------------------------------------------------------------
# REGIME DEFINITIONEN
# ---------------------------------------------------------------------------
REGIMES = [
    "TRENDING_BULL", "TRENDING_BEAR",
    "SQUEEZE_RANGE", "NEUTRAL_RANGE",
    "VOLATILE", "TRANSITION"
]

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return DEFAULT_CONFIG

def log(msg):
    ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts} UTC] {msg}"
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def report(msg):
    print(msg)

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def api_get(endpoint):
    url = BASE_URL + endpoint
    req = urllib.request.Request(url, headers={"User-Agent": "HermesTrader/3.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def api_private(method, endpoint, cfg, body=None, params=None):
    if not cfg.get("api_key") or not cfg.get("api_secret"):
        return {"error": "API Key fehlt"}
    url = BASE_URL + endpoint
    secret = cfg["api_secret"].encode('utf-8')
    timestamp = str(int(time.time() * 1000))
    method = method.upper()

    if body and len(body) > 0:
        body_json = json.dumps(body, separators=(',', ':'))
        params_auth = f'requestBody={body_json}&signTimestamp={timestamp}'
        headers = {"Content-Type": "application/json"}
    else:
        body_json = ""
        params_internal = {"signTimestamp": timestamp}
        if params: params_internal.update(params)
        from urllib.parse import quote
        safe_chars = "~()*!'"
        params_auth = "&".join([f"{k}={quote(str(v), safe=safe_chars)}" for k, v in sorted(params_internal.items())])
        headers = {}

    payload = f"{method}\n{endpoint}\n{params_auth}"
    sig_hash = hmac.new(secret, payload.encode('utf-8'), hashlib.sha256).digest()
    signature = base64.b64encode(sig_hash).decode('utf-8')
    headers.update({"key": cfg["api_key"], "signature": signature, "signTimestamp": timestamp})

    data = body_json.encode('utf-8') if body_json else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:500]}"}
    except Exception as e:
        return {"error": str(e)}

def get_candles(symbol, interval="HOUR_1", limit=200):
    end = int(time.time() * 1000)
    start = end - (limit * 4 * 3600 * 1000)
    ep = f"/markets/{symbol}/candles?interval={interval}&startTime={start}&endTime={end}&limit={limit}"
    data = api_get(ep)
    if isinstance(data, list):
        return sorted(data, key=lambda x: x[9])
    return []

def to_ohlcv(candles):
    return [{"time": c[9], "open": float(c[2]), "high": float(c[1]),
             "low": float(c[0]), "close": float(c[3]), "volume": float(c[5])} for c in candles]

def get_ticker(symbol):
    return api_get(f"/markets/{symbol}/price")

# ---------------------------------------------------------------------------
# INDIKATOREN
# ---------------------------------------------------------------------------
def ema(closes, period):
    if len(closes) < period: return [None]*len(closes)
    k = 2/(period+1)
    r = [None]*(period-1) + [sum(closes[:period])/period]
    for c in closes[period:]: r.append(c*k + r[-1]*(1-k))
    return r

def sma(closes, period):
    if len(closes) < period: return [None]*len(closes)
    r = [None]*(period-1)
    for i in range(period-1, len(closes)):
        r.append(sum(closes[i-period+1:i+1])/period)
    return r

def atr(data, period=14):
    if len(data) < period+1: return [None]*len(data)
    r = [None]; trs = []
    for i in range(1, len(data)):
        trs.append(max(data[i]["high"]-data[i]["low"], abs(data[i]["high"]-data[i-1]["close"]), abs(data[i]["low"]-data[i-1]["close"])))
        if i == 1: continue
        if len(trs) < period: r.append(None)
        elif len(trs) == period: r.append(sum(trs)/period)
        else: r.append((r[-1]*(period-1) + trs[-1])/period)
    return r

def rsi(closes, period=14):
    if len(closes) < period+1: return [None]*len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        gains.append(max(closes[i]-closes[i-1], 0))
        losses.append(abs(min(closes[i]-closes[i-1], 0)))
    r = [None]*period
    avg_g, avg_l = sum(gains[:period])/period, sum(losses[:period])/period
    rs = avg_g/avg_l if avg_l != 0 else float('inf')
    r.append(100 - (100/(1+rs)))
    for i in range(period+1, len(closes)):
        avg_g = (avg_g*(period-1) + gains[i-1])/period
        avg_l = (avg_l*(period-1) + losses[i-1])/period
        rs = avg_g/avg_l if avg_l != 0 else float('inf')
        r.append(100 - (100/(1+rs)))
    return r

def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow: return [None]*len(closes), [None]*len(closes), [None]*len(closes)
    def _e(c, p):
        k = 2/(p+1); v = [sum(c[:p])/p]
        for x in c[p:]: v.append(x*k + v[-1]*(1-k))
        return v
    ef, es = _e(closes, fast), _e(closes, slow)
    ml = [f - s for f, s in zip(ef, es[-len(ef):])]
    sig = _e(ml, signal)
    hist = [m - s for m, s in zip(ml[-len(sig):], sig)]
    pad = len(closes) - len(ml)
    return [None]*pad+ml, [None]*(len(closes)-len(sig))+sig, [None]*(len(closes)-len(hist))+hist

def bbands(closes, period=20, mult=2.0):
    if len(closes) < period: return [None]*len(closes), [None]*len(closes), [None]*len(closes)
    m, u, l = [], [], []
    for i in range(period-1, len(closes)):
        w = closes[i-period+1:i+1]
        ma = sum(w)/period
        sd = math.sqrt(sum((c-ma)**2 for c in w)/period)
        m.append(ma); u.append(ma+mult*sd); l.append(ma-mult*sd)
    pad = len(closes)-len(m)
    return [None]*pad+m, [None]*pad+u, [None]*pad+l

def adx(data, period=14):
    """Average Directional Index — Trendstärke 0-100"""
    if len(data) < period * 2 + 1:
        return [None] * len(data)
    
    trs, plus_dm, minus_dm = [0], [0], [0]
    for i in range(1, len(data)):
        tr = max(data[i]["high"]-data[i]["low"], 
                 abs(data[i]["high"]-data[i-1]["close"]), 
                 abs(data[i]["low"]-data[i-1]["close"]))
        trs.append(tr)
        up = data[i]["high"] - data[i-1]["high"]
        down = data[i-1]["low"] - data[i]["low"]
        plus_dm.append(max(up, 0) if up > down else 0)
        minus_dm.append(max(down, 0) if down > up else 0)
    
    # Smoothed averages
    atr_vals = [None] * period
    plus_di_vals = [None] * period
    minus_di_vals = [None] * period
    dx_vals = [None] * period
    
    atr_smooth = sum(trs[1:period+1]) / period
    plus_smooth = sum(plus_dm[1:period+1]) / period
    minus_smooth = sum(minus_dm[1:period+1]) / period
    
    for i in range(period + 1, len(data)):
        atr_smooth = (atr_smooth * (period - 1) + trs[i]) / period
        plus_smooth = (plus_smooth * (period - 1) + plus_dm[i]) / period
        minus_smooth = (minus_smooth * (period - 1) + minus_dm[i]) / period
        
        plus_di = 100 * plus_smooth / atr_smooth if atr_smooth > 0 else 0
        minus_di = 100 * minus_smooth / atr_smooth if atr_smooth > 0 else 0
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0
        
        atr_vals.append(atr_smooth)
        plus_di_vals.append(plus_di)
        minus_di_vals.append(minus_di)
        dx_vals.append(dx)
    
    # ADX = smoothed DX
    adx_vals = [None] * (period + 1)
    if len(dx_vals) > period + 1 and dx_vals[period + 1] is not None:
        adx_smooth = sum(dx_vals[period + 1:period * 2 + 1]) / period
        adx_vals.append(adx_smooth)
        for i in range(period * 2 + 1, len(dx_vals)):
            if dx_vals[i] is not None:
                adx_smooth = (adx_smooth * (period - 1) + dx_vals[i]) / period
                adx_vals.append(adx_smooth)
            else:
                adx_vals.append(None)
    
    pad = len(data) - len(adx_vals)
    return [None] * pad + adx_vals

def stochastic(highs, lows, closes, k_period=14, d_period=3):
    n = len(closes)
    k_vals = [None] * (k_period - 1)
    for i in range(k_period - 1, n):
        lowest = min(lows[i - k_period + 1:i + 1])
        highest = max(highs[i - k_period + 1:i + 1])
        range_val = highest - lowest
        k_vals.append(100 * (closes[i] - lowest) / range_val if range_val > 0 else 50)
    d_vals = [None] * (k_period + d_period - 2)
    for i in range(k_period + d_period - 2, n):
        d_vals.append(sum(k_vals[i - d_period + 1:i + 1]) / d_period)
    pad = n - len(d_vals)
    if pad > 0:
        d_vals = [None] * pad + d_vals
    return k_vals, d_vals

def williams_r(highs, lows, closes, period=14):
    n = len(closes)
    r = [None] * (period - 1)
    for i in range(period - 1, n):
        highest = max(highs[i - period + 1:i + 1])
        lowest = min(lows[i - period + 1:i + 1])
        range_val = highest - lowest
        r.append(-100 * (highest - closes[i]) / range_val if range_val > 0 else -50)
    return r

def swing_highs_lows(highs, lows, lookback=3):
    n = len(highs)
    swing_h, swing_l = [], []
    for i in range(lookback, n - lookback):
        if highs[i] >= max(highs[j] for j in range(i - lookback, i + lookback + 1) if j != i):
            swing_h.append(i)
        if lows[i] <= min(lows[j] for j in range(i - lookback, i + lookback + 1) if j != i):
            swing_l.append(i)
    return swing_h, swing_l

# ---------------------------------------------------------------------------
# REGIME DETECTION
# ---------------------------------------------------------------------------
def detect_regime(ohlcv, closes, ema50):
    """
    Erkennt Markt-Regime pro Pair basierend auf:
    - ADX(14): Trendstärke
    - BB-Width(20,2): Volatilität/Squeeze
    - EMA50-Slope(10): Trendrichtung
    """
    if len(ohlcv) < 55 or len(closes) < 55:
        return "TRANSITION"
    
    # ADX
    adx_vals = adx(ohlcv, 14)
    adx_now = adx_vals[-1] if adx_vals and adx_vals[-1] is not None else 0
    
    # BB-Width
    bb_mid, bb_up, bb_low = bbands(closes, 20, 2.0)
    bbw = 0
    if bb_mid[-1] and bb_mid[-1] > 0 and bb_up[-1] and bb_low[-1]:
        bbw = (bb_up[-1] - bb_low[-1]) / bb_mid[-1] * 100
    
    # EMA50 Slope
    ema_slope = 0
    if ema50[-1] and ema50[-10]:
        ema_slope = (ema50[-1] - ema50[-10]) / ema50[-10] * 100
    
    # Regime Classification
    if adx_now > 25 and bbw > 3.0:
        if ema_slope > 0.5:
            return "TRENDING_BULL"
        elif ema_slope < -0.5:
            return "TRENDING_BEAR"
    
    if adx_now < 20 and bbw < 3.0:
        return "SQUEEZE_RANGE"
    
    if adx_now < 20 and 3.0 <= bbw <= 6.0:
        return "NEUTRAL_RANGE"
    
    if bbw > 6.0:
        return "VOLATILE"
    
    return "TRANSITION"

# ---------------------------------------------------------------------------
# STRATEGY MAP v3
# ---------------------------------------------------------------------------
DEFAULT_STRATEGY_MAP = {
    "version": "3.0-regime",
    "pairs": {
        "BTC_USDT": {
            "timeframe": "HOUR_1",
            "min_confidence": 60,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 3.0,
            "TRENDING_BULL": {
                "strategies": ["TREND_FOLLOW_LONG", "EMA_BOUNCE_LONG"],
                "confidence_boost": 5, "risk_mult": 1.2
            },
            "TRENDING_BEAR": {
                "strategies": ["STOCH_MR_SHORT", "LOWER_HIGH_BREAK", "WILLR_SHORT"],
                "confidence_boost": 0, "risk_mult": 1.0
            },
            "NEUTRAL_RANGE": {
                "strategies": ["EMA50_BOUNCE_LONG", "WILLR_LONG", "BB_SQUEEZE_LONG"],
                "confidence_boost": 10, "risk_mult": 0.9
            },
            "SQUEEZE_RANGE": {
                "strategies": ["BB_SQUEEZE_LONG", "EMA50_BOUNCE_LONG"],
                "confidence_boost": 10, "risk_mult": 1.0
            },
            "VOLATILE": {
                "strategies": ["MEAN_REVERSION_LONG", "MEAN_REVERSION_SHORT", "WILLR_LONG", "WILLR_SHORT"],
                "confidence_boost": 5, "risk_mult": 0.8
            },
            "TRANSITION": {
                "strategies": [],
                "confidence_boost": 0, "risk_mult": 0.5
            }
        },
        "ETH_USDT": {
            "timeframe": "HOUR_1",
            "min_confidence": 60,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 2.5,
            "TRENDING_BULL": {
                "strategies": ["TREND_FOLLOW_LONG", "EMA_BOUNCE_LONG"],
                "confidence_boost": 5, "risk_mult": 1.2
            },
            "TRENDING_BEAR": {
                "strategies": ["STOCH_MR_SHORT", "LOWER_HIGH_BREAK", "RANGE_BREAKOUT_SHORT"],
                "confidence_boost": 0, "risk_mult": 1.0
            },
            "NEUTRAL_RANGE": {
                "strategies": ["EMA50_BOUNCE_LONG", "WILLR_LONG", "BB_SQUEEZE_LONG"],
                "confidence_boost": 10, "risk_mult": 0.9
            },
            "SQUEEZE_RANGE": {
                "strategies": ["BB_SQUEEZE_LONG", "EMA50_BOUNCE_LONG"],
                "confidence_boost": 10, "risk_mult": 1.0
            },
            "VOLATILE": {
                "strategies": ["MEAN_REVERSION_LONG", "MEAN_REVERSION_SHORT", "WILLR_LONG", "WILLR_SHORT"],
                "confidence_boost": 5, "risk_mult": 0.8
            },
            "TRANSITION": {
                "strategies": [],
                "confidence_boost": 0, "risk_mult": 0.5
            }
        },
        "NEAR_USDT": {
            "timeframe": "HOUR_1",
            "min_confidence": 55,
            "sl_atr_mult": 1.0,
            "tp_atr_mult": 2.0,
            "TRENDING_BULL": {
                "strategies": ["TREND_FOLLOW_LONG", "EMA_BOUNCE_LONG"],
                "confidence_boost": 5, "risk_mult": 1.2
            },
            "TRENDING_BEAR": {
                "strategies": ["STOCH_MR_SHORT", "LOWER_HIGH_BREAK", "WILLR_SHORT"],
                "confidence_boost": 0, "risk_mult": 1.0
            },
            "NEUTRAL_RANGE": {
                "strategies": ["BB_BOUNCE_LONG", "WILLR_LONG", "EMA50_BOUNCE_LONG"],
                "confidence_boost": 10, "risk_mult": 0.9
            },
            "SQUEEZE_RANGE": {
                "strategies": ["BB_BOUNCE_LONG", "BB_SQUEEZE_LONG"],
                "confidence_boost": 10, "risk_mult": 1.0
            },
            "VOLATILE": {
                "strategies": ["MEAN_REVERSION_LONG", "MEAN_REVERSION_SHORT", "WILLR_LONG", "WILLR_SHORT"],
                "confidence_boost": 5, "risk_mult": 0.8
            },
            "TRANSITION": {
                "strategies": [],
                "confidence_boost": 0, "risk_mult": 0.5
            }
        },
        "DOGE_USDT": {
            "timeframe": "HOUR_1",
            "min_confidence": 55,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 2.5,
            "TRENDING_BULL": {
                "strategies": ["TREND_FOLLOW_LONG", "EMA_BOUNCE_LONG"],
                "confidence_boost": 5, "risk_mult": 1.2
            },
            "TRENDING_BEAR": {
                "strategies": ["STOCH_MR_SHORT", "LOWER_HIGH_BREAK", "WILLR_SHORT"],
                "confidence_boost": 0, "risk_mult": 1.0
            },
            "NEUTRAL_RANGE": {
                "strategies": ["BB_BOUNCE_LONG", "EMA50_BOUNCE_LONG", "WILLR_LONG"],
                "confidence_boost": 10, "risk_mult": 0.9
            },
            "SQUEEZE_RANGE": {
                "strategies": ["BB_BOUNCE_LONG", "BB_SQUEEZE_LONG"],
                "confidence_boost": 10, "risk_mult": 1.0
            },
            "VOLATILE": {
                "strategies": ["MEAN_REVERSION_LONG", "MEAN_REVERSION_SHORT", "WILLR_LONG", "WILLR_SHORT"],
                "confidence_boost": 5, "risk_mult": 0.8
            },
            "TRANSITION": {
                "strategies": [],
                "confidence_boost": 0, "risk_mult": 0.5
            }
        }
    }
}

def load_strategy_map():
    if os.path.exists(STRATEGY_MAP_PATH):
        with open(STRATEGY_MAP_PATH, "r") as f:
            data = json.load(f)
        if "pairs" in data:
            return data["pairs"]
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return DEFAULT_STRATEGY_MAP["pairs"]

# ---------------------------------------------------------------------------
# LERNJOURNAL v3 — Mit Regime-Kontext
# ---------------------------------------------------------------------------
def load_learning():
    if os.path.exists(LEARN_PATH):
        with open(LEARN_PATH, "r") as f:
            return json.load(f)
    return {}

def save_learning(data):
    os.makedirs(os.path.dirname(LEARN_PATH), exist_ok=True)
    with open(LEARN_PATH, "w") as f:
        json.dump(data, f, indent=2)

def update_learning(pair, strategy, regime, result, pnl_pct):
    """Aktualisiert Performance-Tracking pro Pair/Strategie/Regime"""
    learn = load_learning()
    key = f"{pair}:{strategy}:{regime}"
    if key not in learn:
        learn[key] = {"wins": 0, "losses": 0, "total_pnl": 0, 
                      "gross_profit": 0, "gross_loss": 0, "pf": 0, 
                      "weight": 1.0, "total_trades": 0}

    entry = learn[key]
    if result == "WIN" or (result == "TIME_STOP" and pnl_pct > 0):
        entry["wins"] += 1
        entry["gross_profit"] += pnl_pct
    else:
        entry["losses"] += 1
        entry["gross_loss"] += abs(pnl_pct)
    entry["total_pnl"] += pnl_pct
    entry["total_trades"] = entry["wins"] + entry["losses"]

    if entry["gross_loss"] > 0:
        entry["pf"] = round(entry["gross_profit"] / entry["gross_loss"], 2)
    else:
        entry["pf"] = float(entry["gross_profit"]) if entry["gross_profit"] > 0 else 0.0

    # Soft weight adjustment
    total = entry["total_trades"]
    if total >= 5:
        pf = entry["pf"]
        if pf >= 1.5:
            entry["weight"] = min(1.5, 1.0 + (pf - 1.0) * 0.2)
        elif pf >= 1.0:
            entry["weight"] = 1.0
        elif pf >= 0.5:
            entry["weight"] = max(0.5, pf)
        else:
            entry["weight"] = 0.3
    
    save_learning(learn)

def get_strategy_weight(pair, strategy, regime):
    """Soft confidence modulation — never hard-block"""
    learn = load_learning()
    key = f"{pair}:{strategy}:{regime}"
    entry = learn.get(key, {})
    
    if entry.get("total_trades", 0) < 3:
        return 1.0  # Neutral for untested
    
    return entry.get("weight", 1.0)

def get_legacy_weight(pair, strategy):
    """Fallback für alte Learning-Einträge ohne Regime"""
    learn = load_learning()
    key = f"{pair}:{strategy}"
    entry = learn.get(key, {})
    return entry.get("weight", 1.0)

# ---------------------------------------------------------------------------
# RISIKO-HILFSFUNKTIONEN (aus v2 übernommen)
# ---------------------------------------------------------------------------
def check_weekly_loss(cfg):
    today = datetime.datetime.now(timezone.utc)
    week_start = (today - datetime.timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    if not os.path.exists(JOURNAL_PATH):
        return True
    loss = 0.0
    with open(JOURNAL_PATH, "r") as f:
        for line in f:
            try:
                t = json.loads(line)
                close_day = t.get("close_time", "")[:10]
                if close_day >= week_start and t.get("pnl_pct", 0) < 0:
                    loss += abs(t["pnl_pct"])
            except:
                pass
    return loss < cfg.get("max_weekly_drawdown_pct", 6.0)

def check_daily_loss(cfg):
    today = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not os.path.exists(JOURNAL_PATH):
        return True, 0.0
    loss = 0.0
    pnl = 0.0
    with open(JOURNAL_PATH, "r") as f:
        for line in f:
            try:
                t = json.loads(line)
                close_day = t.get("close_time", "")[:10]
                if close_day == today:
                    p = t.get("pnl_pct", 0)
                    pnl += p
                    if p < 0:
                        loss += abs(p)
            except:
                pass
    max_daily = cfg.get("max_daily_loss_pct", 3.0)
    dd_warn = cfg.get("drawdown_warn_pct", max_daily * 0.7)
    if loss >= dd_warn and loss < max_daily:
        report(f"⚠️ DRAWDOWN WARNUNG: Tagesverlust {loss:.2f}% (Warnung bei {dd_warn:.1f}%, Limit bei {max_daily:.1f}%)")
    if loss >= max_daily:
        report(f"🚨 DRAWDOWN ALARM: Tagesverlust {loss:.2f}% überschreitet Limit von {max_daily:.1f}%! Bot pausiert für heute.")
        return False, loss
    return True, loss

def get_pair_cooldown_remaining(pair, hours=12):
    if not os.path.exists(JOURNAL_PATH):
        return 0
    now = datetime.datetime.now(timezone.utc)
    last_close = None
    with open(JOURNAL_PATH, "r") as f:
        for line in f:
            try:
                t = json.loads(line)
                if t.get("pair") == pair and t.get("status") == "CLOSED" and t.get("close_time"):
                    ct = datetime.datetime.fromisoformat(t["close_time"])
                    if last_close is None or ct > last_close:
                        last_close = ct
            except:
                pass
    if last_close is None:
        return 0
    cooldown_end = last_close + datetime.timedelta(hours=hours)
    remaining = int((cooldown_end - now).total_seconds())
    return max(0, remaining)

def _parse_journal_ts(ts_raw):
    if not ts_raw:
        return None
    try:
        if isinstance(ts_raw, (int, float)) or (isinstance(ts_raw, str) and ts_raw.isdigit()):
            ms = int(ts_raw)
            if ms > 1e12:
                ms = ms // 1000
            return datetime.datetime.fromtimestamp(ms, tz=timezone.utc)
        if "T" in ts_raw:
            return datetime.datetime.fromisoformat(ts_raw)
        return datetime.datetime.strptime(ts_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None

def check_strategy_pause(pair, strategy_name, min_losses=3, hours=48):
    if not os.path.exists(JOURNAL_PATH):
        return False
    results = []
    with open(JOURNAL_PATH, "r") as f:
        for line in f:
            try:
                t = json.loads(line)
                if t.get("pair") == pair and t.get("setup_type") == strategy_name and t.get("result") in ("WIN", "LOSS"):
                    ts = _parse_journal_ts(t.get("close_time") or t.get("timestamp") or t.get("date"))
                    if ts:
                        results.append((ts, t["result"]))
            except Exception:
                pass
    if len(results) < min_losses:
        return False
    results.sort(key=lambda x: x[0])
    last = results[-min_losses:]
    if not all(r == "LOSS" for _, r in last):
        return False
    now = datetime.datetime.now(timezone.utc)
    return (now - last[-1][0]) < datetime.timedelta(hours=hours)

def check_pair_pause(pair, min_losses=4, hours=24):
    if not os.path.exists(JOURNAL_PATH):
        return False
    results = []
    with open(JOURNAL_PATH, "r") as f:
        for line in f:
            try:
                t = json.loads(line)
                if t.get("pair") == pair and t.get("result") in ("WIN", "LOSS"):
                    ts = _parse_journal_ts(t.get("close_time") or t.get("timestamp") or t.get("date"))
                    if ts:
                        results.append((ts, t["result"]))
            except Exception:
                pass
    if len(results) < min_losses:
        return False
    results.sort(key=lambda x: x[0])
    last = results[-min_losses:]
    if not all(r == "LOSS" for _, r in last):
        return False
    now = datetime.datetime.now(timezone.utc)
    return (now - last[-1][0]) < datetime.timedelta(hours=hours)

def calculate_position_size(entry, sl, risk_pct, balance=10000):
    risk_amount = balance * (risk_pct / 100)
    sl_dist = abs(entry - sl)
    if sl_dist == 0:
        return 0
    qty = risk_amount / sl_dist
    return round(qty, 8)

# ---------------------------------------------------------------------------
# SUPPLY & DEMAND (aus v2 übernommen)
# ---------------------------------------------------------------------------
def find_order_blocks(ohlcv, min_impulse_pct=1.5, lookback=50):
    zones = {"demand": [], "supply": []}
    if len(ohlcv) < 10:
        return zones
    n = len(ohlcv)
    for i in range(n - 4, max(0, n - lookback), -1):
        c0, c1, c2, c3 = ohlcv[i], ohlcv[i+1], ohlcv[i+2], ohlcv[i+3]
        if c0["close"] < c0["open"] and c1["close"] > c1["open"] and c2["close"] > c2["open"] and c3["close"] > c3["open"]:
            if c3["close"] > c0["high"]:
                impulse = (c3["close"] - c0["close"]) / c0["close"] * 100
                if impulse >= min_impulse_pct:
                    zones["demand"].append({"low": min(c0["low"], c1["low"]), "high": max(c0["high"], c1["high"]), "type": "demand"})
        if c0["close"] > c0["open"] and c1["close"] < c1["open"] and c2["close"] < c2["open"] and c3["close"] < c3["open"]:
            if c3["close"] < c0["low"]:
                impulse = (c0["close"] - c3["close"]) / c0["close"] * 100
                if impulse >= min_impulse_pct:
                    zones["supply"].append({"low": min(c0["low"], c1["low"]), "high": max(c0["high"], c1["high"]), "type": "supply"})
    zones["demand"] = zones["demand"][:5]
    zones["supply"] = zones["supply"][:5]
    return zones

def is_near_zone(price, zone, threshold_pct=0.5):
    if not zone:
        return False
    ext_low = zone["low"] * (1 - threshold_pct/100)
    ext_high = zone["high"] * (1 + threshold_pct/100)
    return ext_low <= price <= ext_high

def get_zone_sl_tp(price, direction, zones):
    if direction == "LONG" and zones.get("demand"):
        for z in zones["demand"]:
            if is_near_zone(price, z, threshold_pct=0.8):
                sl = z["low"] * 0.998
                tp = None
                for s in zones.get("supply", []):
                    if s["low"] > price:
                        tp = s["high"]
                        break
                return sl, tp
    elif direction == "SHORT" and zones.get("supply"):
        for z in zones["supply"]:
            if is_near_zone(price, z, threshold_pct=0.8):
                sl = z["high"] * 1.002
                tp = None
                for d in zones.get("demand", []):
                    if d["high"] < price:
                        tp = d["low"]
                        break
                return sl, tp
    return None, None

def get_htf_trend(symbol, interval="HOUR_4"):
    candles_raw = get_candles(symbol, interval, limit=100)
    if not candles_raw:
        return "NEUTRAL"
    ohlcv = to_ohlcv(candles_raw)
    if len(ohlcv) < 50:
        return "NEUTRAL"
    closes = [c["close"] for c in ohlcv]
    ema9_h = ema(closes, 9)
    ema21_h = ema(closes, 21)
    ema50_h = ema(closes, 50)
    if ema9_h[-1] and ema21_h[-1] and ema50_h[-1]:
        if ema9_h[-1] > ema21_h[-1] > ema50_h[-1]:
            return "BULLISH"
        elif ema9_h[-1] < ema21_h[-1] < ema50_h[-1]:
            return "BEARISH"
    return "NEUTRAL"

# ---------------------------------------------------------------------------
# PAIR-ANALYSE v3 — Regime-Adaptive
# ---------------------------------------------------------------------------
def analyze_pair(symbol, interval="HOUR_1", strat_map=None):
    candles_raw = get_candles(symbol, interval, limit=200)
    if not candles_raw: return None, "Keine Daten"
    ohlcv = to_ohlcv(candles_raw)
    if len(ohlcv) < 50: return None, f"Zu wenig Kerzen: {len(ohlcv)}"

    closes = [c["close"] for c in ohlcv]
    highs = [c["high"] for c in ohlcv]
    lows = [c["low"] for c in ohlcv]
    volumes = [c["volume"] for c in ohlcv]
    current = ohlcv[-1]
    prev = ohlcv[-2]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)
    rsi14 = rsi(closes, 14)
    atr14 = atr(ohlcv, 14)
    macd_line, sig_line, hist = macd(closes)
    bb_mid, bb_up, bb_low = bbands(closes)
    stoch_k, stoch_d = stochastic(highs, lows, closes)
    will_r_vals = williams_r(highs, lows, closes)

    # VWAP rolling 50
    vwap_val = None
    if len(volumes) >= 50:
        vol = sum(volumes[-50:])
        pv = sum(closes[i]*volumes[i] for i in range(-50, 0))
        vwap_val = pv/vol if vol > 0 else None

    # Trend
    trend = "NEUTRAL"
    if ema9[-1] > ema21[-1] > ema50[-1]: trend = "BULLISH"
    elif ema9[-1] < ema21[-1] < ema50[-1]: trend = "BEARISH"

    momentum = "NEUTRAL"
    if macd_line[-1] and sig_line[-1]:
        if macd_line[-1] > sig_line[-1] and macd_line[-2] <= sig_line[-2]: momentum = "BULLISH_CROSS"
        elif macd_line[-1] < sig_line[-1] and macd_line[-2] >= sig_line[-2]: momentum = "BEARISH_CROSS"
        elif macd_line[-1] > sig_line[-1]: momentum = "BULLISH"
        else: momentum = "BEARISH"

    rsi_val = rsi14[-1]
    rsi_state = "NEUTRAL"
    if rsi_val:
        if rsi_val > 70: rsi_state = "OVERBOUGHT"
        elif rsi_val < 30: rsi_state = "OVERSOLD"

    bb_state = "NEUTRAL"
    if bb_low[-1] and current["close"] <= bb_low[-1]: bb_state = "LOWER_TOUCH"
    elif bb_up[-1] and current["close"] >= bb_up[-1]: bb_state = "UPPER_TOUCH"

    atr_val = atr14[-1] if atr14[-1] else current["close"]*0.01

    # Regime Detection
    regime = detect_regime(ohlcv, closes, ema50)

    # Higher-Timeframe Trend
    htf_trend = get_htf_trend(symbol)

    # HTF Supply/Demand Zonen
    htf_candles = get_candles(symbol, "HOUR_4", limit=100)
    htf_zones = {"demand": [], "supply": []}
    if htf_candles:
        htf_ohlcv = to_ohlcv(htf_candles)
        if len(htf_ohlcv) >= 10:
            htf_zones = find_order_blocks(htf_ohlcv, min_impulse_pct=1.0, lookback=40)

    # Pair-Preferences
    pair_prefs = strat_map.get(symbol, {}) if strat_map else {}
    regime_prefs = pair_prefs.get(regime, {})
    allowed_strategies = regime_prefs.get("strategies", [])
    regime_risk_mult = regime_prefs.get("risk_mult", 1.0)
    regime_conf_boost = regime_prefs.get("confidence_boost", 0)

    # Volume Filter
    vol_mult = pair_prefs.get("volume_mult", 1.0)
    if len(volumes) >= 21:
        avg_vol = sum(volumes[-21:-1]) / 20
        if avg_vol > 0 and volumes[-2] / avg_vol < vol_mult:
            return None, f"Volumen zu niedrig ({volumes[-2]/avg_vol:.2f}x, min={vol_mult}x)"

    # Setup-Erkennung
    setups = []
    avoid = pair_prefs.get("avoid", [])

    def add_setup(name, direction, conf, cond=True):
        if not cond: return
        if name in avoid: return
        # Optional: Regime-allowed-Filter (nur wenn explizit gesetzt)
        if allowed_strategies and name not in allowed_strategies:
            return
        # HTF-Filter
        if htf_trend == "BULLISH" and direction == "SHORT":
            log(f"{symbol}: {name} abgelehnt — SHORT widerspricht H4-BULLISH")
            return
        if htf_trend == "BEARISH" and direction == "LONG" and trend != "BULLISH":
            log(f"{symbol}: {name} abgelehnt — LONG bei H4-BEARISH und schwachem H1")
            return
        setups.append({"name": name, "direction": direction, "confidence": conf})

    # 1) Trendfolge
    if rsi_state in ("NEUTRAL", "OVERSOLD"):
        conf = 75
        if bb_state == "LOWER_TOUCH": conf += 10
        add_setup("TREND_FOLLOW_LONG", "LONG", min(conf, 95))
    if trend == "BEARISH" and momentum != "BULLISH_CROSS" and rsi_val and rsi_val >= 40:
        conf = 70
        if momentum in ("BEARISH", "BEARISH_CROSS"): conf = 75
        if bb_state == "UPPER_TOUCH": conf += 10
        add_setup("TREND_FOLLOW_SHORT", "SHORT", min(conf, 90))
    elif trend == "BEARISH" and momentum == "NEUTRAL" and rsi_val and rsi_val >= 40:
        add_setup("TREND_FOLLOW_SHORT", "SHORT", 65)

    # 2) Mean Reversion
    if rsi_val and rsi_val < 25 and bb_state == "LOWER_TOUCH":
        add_setup("MEAN_REVERSION_LONG", "LONG", 65)
    if rsi_val and rsi_val > 70 and bb_state == "UPPER_TOUCH":
        add_setup("MEAN_REVERSION_SHORT", "SHORT", 65)

    # 3) EMA Bounce
    ema_bounce_dist = pair_prefs.get("ema_bounce_dist", 0.5)
    dist21 = abs(current["close"] - ema21[-1]) / current["close"] * 100 if ema21[-1] else 999
    if dist21 < ema_bounce_dist and current["close"] > ema21[-1] and current["low"] <= ema21[-1]:
        add_setup("EMA_BOUNCE_LONG", "LONG", 60)
    bounce_short_rsi = pair_prefs.get("ema_bounce_short_rsi_min", 40)
    if dist21 < ema_bounce_dist and current["close"] < ema21[-1] and current["high"] >= ema21[-1] and rsi_val and rsi_val >= bounce_short_rsi:
        add_setup("EMA_BOUNCE_SHORT", "SHORT", 60)

    # 4) VWAP Retest
    vwap_dist = pair_prefs.get("vwap_dist", 0.3)
    if vwap_val:
        dv = abs(current["close"] - vwap_val) / current["close"] * 100
        if dv < vwap_dist and current["close"] > vwap_val and prev["close"] <= vwap_val and rsi_val and rsi_val < 45:
            add_setup("VWAP_RETAIL_LONG", "LONG", 65)
        vwap_short_rsi = pair_prefs.get("vwap_short_rsi_min", 40)
        if dv < vwap_dist and current["close"] < vwap_val and prev["close"] >= vwap_val and rsi_val and rsi_val >= vwap_short_rsi:
            add_setup("VWAP_RETAIL_SHORT", "SHORT", 65)

    # 5) EMA50 Bounce
    if ema50[-1] is not None:
        dist50 = abs(current["close"] - ema50[-1]) / current["close"] * 100
        if dist50 < 0.3 and current["low"] <= ema50[-1] <= current["high"]:
            if rsi_val and rsi_val < 55:
                add_setup("EMA50_BOUNCE_LONG", "LONG", 60)

    # 6) BB Bounce
    bb_bounce_dist = pair_prefs.get("bb_bounce_dist", 0.5)
    if bb_low[-1] is not None and bb_up[-1] is not None:
        # LONG: Preis berührt/unterschreitet bb_low und schließt wieder darüber
        if current["low"] <= bb_low[-1] * (1 + bb_bounce_dist/100) and current["close"] > bb_low[-1] and current["close"] < bb_mid[-1]:
            if rsi_val and rsi_val < 60:
                add_setup("BB_BOUNCE_LONG", "LONG", 65)
        # SHORT: Preis berührt/überschreitet bb_up und schließt wieder darunter
        if current["high"] >= bb_up[-1] * (1 - bb_bounce_dist/100) and current["close"] < bb_up[-1] and current["close"] > bb_mid[-1]:
            if rsi_val and rsi_val > 40:
                add_setup("BB_BOUNCE_SHORT", "SHORT", 60)

    # 7) Range Breakout Short
    if macd_line[-1] is not None and sig_line[-1] is not None:
        if abs(macd_line[-1] - sig_line[-1]) < atr_val * 0.1:
            if current["low"] < prev["low"] and current["close"] < prev["close"]:
                add_setup("RANGE_BREAKOUT_SHORT", "SHORT", 55)

    # 8) RSI Divergenz
    rsi_div_long_max = pair_prefs.get("rsi_div_rsi_long_max", 45)
    rsi_div_short_min = pair_prefs.get("rsi_div_rsi_short_min", 55)
    if rsi_val and rsi14[-5] and current["close"] < closes[-5] and rsi_val > rsi14[-5] and rsi_val < rsi_div_long_max:
        add_setup("RSI_DIVERGENCE_LONG", "LONG", 70)
    if rsi_val and rsi14[-5] and current["close"] > closes[-5] and rsi_val < rsi14[-5] and rsi_val > rsi_div_short_min:
        add_setup("RSI_DIVERGENCE_SHORT", "SHORT", 70)

    # 9) Lower High Break
    if trend == "BEARISH" and len(ohlcv) >= 30:
        swing_h_all, swing_l_all = swing_highs_lows(highs, lows, lookback=pair_prefs.get("lh_swing_lb", 2))
        swing_h_past = [s for s in swing_h_all if s < len(ohlcv) - 1]
        swing_l_past = [s for s in swing_l_all if s < len(ohlcv) - 1]
        if len(swing_h_past) >= 2 and len(swing_l_past) >= 2:
            last_sh, prev_sh = swing_h_past[-1], swing_h_past[-2]
            last_sl, prev_sl = swing_l_past[-1], swing_l_past[-2]
            if highs[last_sh] < highs[prev_sh] * 0.995 and lows[last_sl] <= lows[prev_sl] * 1.005:
                if current["close"] < lows[last_sl]:
                    if rsi_val is None or rsi_val >= 25:
                        conf = pair_prefs.get("lh_conf_base", 65)
                        add_setup("LOWER_HIGH_BREAK", "SHORT", conf)

    # 10) EMA Compression Break
    if trend == "BEARISH" and len(ohlcv) >= 55:
        ec_lookback = pair_prefs.get("ec_lookback", 8)
        ec_compress = pair_prefs.get("ec_compress", 0.6)
        ec_vol_mult = pair_prefs.get("ec_vol_mult", 1.5)
        compressed_count = 0
        for j in range(-ec_lookback, 0):
            idx_j = len(ohlcv) + j
            if ema9[idx_j] is None or ema21[idx_j] is None or ema50[idx_j] is None:
                continue
            max_em = max(ema9[idx_j], ema21[idx_j], ema50[idx_j])
            min_em = min(ema9[idx_j], ema21[idx_j], ema50[idx_j])
            if (max_em - min_em) / min_em * 100 < ec_compress:
                compressed_count += 1
        if compressed_count >= ec_lookback // 2:
            if current["close"] < min(ema9[-1], ema21[-1], ema50[-1]):
                avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
                if avg_vol and volumes[-1] >= avg_vol * ec_vol_mult:
                    cross_ok = False
                    for j in range(len(ohlcv) - 4, len(ohlcv)):
                        if j > 0 and ema9[j] < ema21[j] and ema9[j - 1] >= ema21[j - 1]:
                            cross_ok = True
                            break
                    if not cross_ok and ema9[-1] < ema21[-1]:
                        cross_ok = True
                    if cross_ok:
                        conf = 65
                        if volumes[-1] / avg_vol > 2.0:
                            conf += 10
                        add_setup("EMA_COMPRESSION_BREAK", "SHORT", min(conf, 90))

    # 11) Stochastic MR
    if stoch_k is not None and stoch_d is not None:
        if stoch_k[-1] > 75 and stoch_k[-1] < stoch_d[-1] and stoch_k[-1] < (stoch_k[-2] if len(stoch_k) > 1 else stoch_k[-1]):
            conf = 70
            if trend == "BEARISH": conf += 10
            if bb_up is not None and current["close"] > bb_up[-1]: conf += 5
            add_setup("STOCH_MR_SHORT", "SHORT", min(conf, 90))

    # 12) Williams %R
    if will_r_vals is not None and len(will_r_vals) >= 3:
        wr_now = will_r_vals[-1]
        wr_slope = will_r_vals[-1] - will_r_vals[-3]
        if wr_now < -85 and wr_slope > -0.5:
            conf = 70
            if trend == "BULLISH": conf += 10
            add_setup("WILLR_LONG", "LONG", min(conf, 90))
        if wr_now > -15 and wr_slope < 0.5:
            conf = 70
            if trend == "BEARISH": conf += 10
            add_setup("WILLR_SHORT", "SHORT", min(conf, 90))

    # 13) BB Squeeze
    if bb_up is not None and bb_low is not None:
        bb_width = (bb_up[-1] - bb_low[-1]) / bb_mid[-1] * 100
        bb_width_hist = []
        for j in range(len(ohlcv) - 14, len(ohlcv)):
            if j > 0 and bb_up[j] is not None and bb_low[j] is not None and bb_mid[j] is not None and bb_mid[j] > 0:
                bb_width_hist.append((bb_up[j] - bb_low[j]) / bb_mid[j] * 100)
        if bb_width_hist and bb_width == min(bb_width_hist) and bb_width < 4.0:
            if current["close"] < bb_mid[-1] and trend != "BEARISH":
                add_setup("BB_SQUEEZE_LONG", "LONG", 75)

    # Regime Confidence Boost
    for s in setups:
        s["confidence"] = min(100, s["confidence"] + regime_conf_boost)

    # Soft Weight Modulation (per regime)
    for s in setups:
        weight = get_strategy_weight(symbol, s["name"], regime)
        # Fallback auf alte Einträge
        if weight == 1.0:
            weight = get_legacy_weight(symbol, s["name"])
        s["confidence"] = min(100, int(s["confidence"] * weight))

    # S/D Zone Confluence
    active_zone = None
    zone_type = None
    price = current["close"]
    for s in setups:
        if s["direction"] == "LONG":
            for z in htf_zones["demand"]:
                if is_near_zone(price, z, threshold_pct=0.8):
                    s["confidence"] = min(100, s["confidence"] + 15)
                    active_zone = z; zone_type = "demand"
                    break
            if not active_zone:
                for z in htf_zones["supply"]:
                    if is_near_zone(price, z, threshold_pct=0.8):
                        s["confidence"] = max(0, s["confidence"] - 10)
                        break
        elif s["direction"] == "SHORT":
            for z in htf_zones["supply"]:
                if is_near_zone(price, z, threshold_pct=0.8):
                    s["confidence"] = min(100, s["confidence"] + 15)
                    active_zone = z; zone_type = "supply"
                    break
            if not active_zone:
                for z in htf_zones["demand"]:
                    if is_near_zone(price, z, threshold_pct=0.8):
                        s["confidence"] = max(0, s["confidence"] - 10)
                        break

    # MULTI-FACTOR CONFLUENCE BOOST
    # Wenn 2+ Strategien in gleiche Richtung signalisieren
    direction_groups = {}
    for s in setups:
        d = s["direction"]
        if d not in direction_groups:
            direction_groups[d] = []
        direction_groups[d].append(s)
    
    for direction, group in direction_groups.items():
        if len(group) >= 2:
            # Sortiere nach Confidence
            group.sort(key=lambda x: x["confidence"], reverse=True)
            # Boost für alle außer dem besten
            for i, s in enumerate(group):
                if i == 0:
                    continue  # Bestes bekommt keinen Extra-Boost
                boost = min(15, (len(group) - 1) * 5)
                s["confidence"] = min(100, s["confidence"] + boost)
                log(f"{symbol}: {s['name']} Confluence-Boost +{boost} ({len(group)} {direction} Signals)")

    if not setups:
        return None, f"Kein Setup (Regime={regime}, Trend={trend}, H4={htf_trend})"
    
    best = max(setups, key=lambda x: x["confidence"])

    # ETH Momentum-Filter für SHORTs
    if symbol == "ETH_USDT" and best["direction"] == "SHORT" and momentum == "BULLISH":
        log(f"{symbol}: {best['name']} abgelehnt — SHORT bei BULLISH Momentum")
        return None, f"ETH SHORT bei BULLISH Momentum blockiert"

    # RSI-Filter für LONGs
    if best["direction"] == "LONG" and rsi_val is not None and 50 <= rsi_val < 60:
        return None, f"LONG im toten RSI-Bereich (50-59) blockiert"

    return {"symbol": symbol, "timeframe": pair_prefs.get("timeframe", "HOUR_1"),
        "timestamp": datetime.datetime.now(timezone.utc).isoformat(),
        "price": current["close"], "vwap": vwap_val, "trend": trend,
        "htf_trend": htf_trend, "momentum": momentum, "rsi": rsi_val,
        "rsi_state": rsi_state, "bb_state": bb_state, "atr": atr_val,
        "regime": regime,
        "setup": best["name"], "direction": best["direction"],
        "confidence": best["confidence"],
        "all_setups": [{"name": s["name"], "conf": s["confidence"], "dir": s["direction"]} for s in setups],
        "htf_zones": htf_zones,
        "active_zone": active_zone,
        "zone_type": zone_type,
        "regime_risk_mult": regime_risk_mult
    }, None

# ---------------------------------------------------------------------------
# ADAPTIVE RISIKO v3 — Mit Regime-Kontext
# ---------------------------------------------------------------------------
def get_adaptive_risk(pair, strategy, direction, regime, base_risk=1.0):
    """Skaliert Risiko basierend auf Strategie-Performance UND Regime."""
    learn = load_learning()
    key = f"{pair}:{strategy}:{regime}"
    entry = learn.get(key, {})
    
    # Fallback auf alte Einträge
    if not entry:
        key_old = f"{pair}:{strategy}"
        entry = learn.get(key_old, {})
    
    total = entry.get("total_trades", 0)
    pf = entry.get("pf", 0)

    # Regime-Transition = reduziertes Risiko
    if regime == "TRANSITION":
        base_risk *= 0.5

    # Shorts ohne Track Record starten mit halbem Risiko
    if direction == "SHORT" and total < 3:
        return base_risk * 0.5

    if direction == "SHORT" and total >= 3:
        if pf < 1.0:
            return min(base_risk, 0.25)
        elif pf < 1.5:
            return min(base_risk, 0.5)
        elif pf >= 1.5:
            return min(base_risk, 0.75)

    if total >= 6 and pf >= 3.0:
        return base_risk
    if total >= 3 and pf >= 1.5:
        return min(base_risk, 0.75)
    if total >= 3 and entry.get("wins", 0) == 0:
        return min(base_risk, 0.5)
    if total >= 3 and pf < 1.5:
        return min(base_risk, 0.5)
    return base_risk

def build_trade(analysis, cfg, pair_prefs=None):
    if not analysis or not analysis.get("setup"): return None
    
    pp = pair_prefs or {}
    setup_name = analysis["setup"]
    direction = analysis["direction"]
    symbol = analysis["symbol"]
    
    # Strategie-spezifische Parameter > Pair-Default > Global-Default
    strat_params = pp.get("strategy_params", {}).get(setup_name, {})
    min_conf = strat_params.get("min_confidence",
                   pp.get("min_confidence",
                     cfg.get("min_confidence", 70)))
    sl_mult = strat_params.get("sl_atr_mult",
                   pp.get("sl_atr_mult", 1.5))
    tp_mult = strat_params.get("tp_atr_mult",
                   pp.get("tp_atr_mult", 3.0))
    
    if analysis["confidence"] < min_conf: return None

    # ETH Momentum-Filter
    if symbol == "ETH_USDT" and direction == "SHORT" and analysis.get("momentum") == "BULLISH":
        return None

    # BTC H4-Trend-Filter für Altcoins
    htf_trend = analysis.get("htf_trend", "NEUTRAL")
    if symbol != "BTC_USDT" and htf_trend == "BEARISH" and direction == "LONG":
        return None

    # RSI-Filter für LONGs
    rsi = analysis.get("rsi")
    if direction == "LONG" and rsi is not None and 50 <= rsi < 60:
        return None

    price = analysis["price"]
    atr_val = analysis["atr"]
    
    # Adaptive Risiko mit Regime
    base_risk = cfg.get("risk_per_trade_pct", 1.0)
    regime_risk_mult = analysis.get("regime_risk_mult", 1.0)
    risk_pct = get_adaptive_risk(
        symbol, setup_name, direction, 
        analysis.get("regime", "NEUTRAL"), base_risk
    )
    risk_pct *= regime_risk_mult

    sl_dist = atr_val * sl_mult
    tp_dist = atr_val * tp_mult
    zone_sl, zone_tp = get_zone_sl_tp(price, direction, analysis.get("htf_zones", {"demand": [], "supply": []}))
    if direction == "LONG":
        entry = price
        sl = zone_sl if zone_sl and zone_sl < price - (atr_val * 0.5) else price - sl_dist
        tp = zone_tp if zone_tp and zone_tp > price else price + tp_dist
    else:
        entry = price
        sl = zone_sl if zone_sl and zone_sl > price + (atr_val * 0.5) else price + sl_dist
        tp = zone_tp if zone_tp and zone_tp < price else price - tp_dist
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)

    # Time-Stop: Strategie-spezifisch > Pair-Default
    ts_key = "time_stop_bullish" if direction == "LONG" else "time_stop_bearish"
    time_stop = strat_params.get("time_stop",
                     pp.get(ts_key,
                       48 if direction == "LONG" else 24))

    return {
        "pair": symbol, "direction": direction,
        "entry": round(entry, 8), "stop_loss": round(sl, 8),
        "take_profit": round(tp, 8), "risk_pct": round(risk_pct, 2),
        "risk_reward": round(tp_dist/sl_dist, 2) if sl_dist > 0 else 0,
        "time_stop": time_stop,
        "timeframe": analysis["timeframe"], "setup_type": setup_name,
        "confidence": analysis["confidence"], "atr": round(atr_val, 8),
        "timestamp": analysis["timestamp"], "trend": analysis["trend"],
        "momentum": analysis["momentum"], "rsi": round(analysis["rsi"], 2) if analysis["rsi"] else None,
        "rsi_state": analysis["rsi_state"], "bb_state": analysis["bb_state"],
        "regime": analysis.get("regime", "UNKNOWN"),
        "status": "OPEN"
    }

# ---------------------------------------------------------------------------
# JOURNAL & STATS (aus v2 übernommen)
# ---------------------------------------------------------------------------
def log_trade(trade, result=None):
    os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
    entry = {**trade, "date": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d"), "result": result}
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

def get_open_pairs():
    if not os.path.exists(JOURNAL_PATH):
        return set()
    open_pairs = set()
    with open(JOURNAL_PATH, "r") as f:
        for line in f:
            try:
                t = json.loads(line)
                if t.get("status") == "OPEN":
                    open_pairs.add(t.get("pair"))
            except:
                pass
    return open_pairs

def get_open_clusters():
    if not os.path.exists(JOURNAL_PATH):
        return set()
    clusters = set()
    with open(JOURNAL_PATH, "r") as f:
        for line in f:
            try:
                t = json.loads(line)
                if t.get("status") == "OPEN":
                    c = CORR_CLUSTERS.get(t.get("pair"))
                    if c: clusters.add(c)
            except:
                pass
    return clusters

def get_stats():
    if not os.path.exists(JOURNAL_PATH): return {}
    trades = []
    with open(JOURNAL_PATH, "r") as f:
        for line in f:
            try: trades.append(json.loads(line))
            except: pass
    wins = [t for t in trades if t.get("result") == "WIN"]
    losses = [t for t in trades if t.get("result") == "LOSS"]
    total = len(wins) + len(losses)
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    today = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = [t for t in trades if t.get("close_time", "").startswith(today) or t.get("date") == today]
    today_wins = [t for t in today_trades if t.get("result") == "WIN"]
    today_losses = [t for t in today_trades if t.get("result") == "LOSS"]
    today_pnl = sum(t.get("pnl_pct", 0) for t in today_trades)
    return {
        "total": total, "wins": len(wins), "losses": len(losses),
        "wr": round(len(wins)/total*100, 1) if total else 0,
        "open": len(open_trades),
        "today_wins": len(today_wins), "today_losses": len(today_losses),
        "today_pnl": round(today_pnl, 4)
    }

# ---------------------------------------------------------------------------
# SIMULATION (Paper Trading)
# ---------------------------------------------------------------------------
def simulate_outcomes():
    if not os.path.exists(JOURNAL_PATH): return
    with open(JOURNAL_PATH, "r") as f:
        lines = f.readlines()
    updated = []
    changed = False
    for line in lines:
        try: t = json.loads(line)
        except: updated.append(line); continue
        if t.get("status") != "OPEN":
            updated.append(line); continue
        
        tk = get_ticker(t["pair"])
        if "error" in tk: updated.append(line); continue
        price = float(tk.get("price", 0))
        if price == 0: updated.append(line); continue

        direction, sl, tp, entry = t["direction"], t["stop_loss"], t["take_profit"], t["entry"]
        result = None; pnl = 0
        sl_dist = abs(entry - sl)
        risk_pct = t.get("risk_pct", 1.0)

        entry_time = datetime.datetime.fromisoformat(t["timestamp"])
        age_hours = (datetime.datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
        # Time-Stop: Trade-spezifisch > Pair-Default
        trade_time_stop = t.get("time_stop")
        if trade_time_stop is not None:
            time_stop_hours = trade_time_stop
        else:
            trade_trend = t.get("trend", "NEUTRAL")
            time_stop_hours = 48 if trade_trend == "BULLISH" else 24
        if age_hours >= time_stop_hours:
            if direction == "LONG":
                price_move = price - entry
            else:
                price_move = entry - price
            if price_move < 0 and sl_dist > 0:
                pnl = risk_pct * (price_move / sl_dist)
                result = "TIME_STOP"
                t["time_stop"] = True

        if not result:
            if direction == "LONG":
                if price <= sl:
                    result, pnl = "LOSS", -risk_pct
                elif price >= tp:
                    rr = abs(tp - entry) / sl_dist if sl_dist > 0 else 1
                    result, pnl = "WIN", risk_pct * rr
            else:
                if price >= sl:
                    result, pnl = "LOSS", -risk_pct
                elif price <= tp:
                    rr = abs(entry - tp) / sl_dist if sl_dist > 0 else 1
                    result, pnl = "WIN", risk_pct * rr

        if result:
            t["status"] = "CLOSED"
            t["result"] = result
            t["exit_price"] = price
            t["pnl_pct"] = round(pnl, 4)
            t["close_time"] = datetime.datetime.now(timezone.utc).isoformat()
            log(f"[CLOSED] {t['pair']} {direction} {result} @ {price:.4f} PnL={pnl:.2f}%")
            report(f"TRADE CLOSED: {t['pair']} {direction} {result} @ {price:.4f} | PnL: {pnl:.2f}% | Setup: {t.get('setup_type','N/A')} | Regime: {t.get('regime','N/A')}")
            update_learning(t["pair"], t["setup_type"], t.get("regime", "UNKNOWN"), result, pnl)
            changed = True
            updated.append(json.dumps(t) + "\n")
        else:
            updated.append(line)

    if changed:
        with open(JOURNAL_PATH, "w") as f:
            f.writelines(updated)

# ---------------------------------------------------------------------------
# HAUPTSCANNER v3
# ---------------------------------------------------------------------------
def run_scan(cfg):
    log("HERMES AGENT v3.0 SCAN")
    
    strat_map = load_strategy_map()
    pairs = cfg.get("pairs", [])
    signals = []
    
    open_pairs = get_open_pairs()
    
    open_count = 0
    if os.path.exists(JOURNAL_PATH):
        with open(JOURNAL_PATH, "r") as f:
            for line in f:
                try:
                    if json.loads(line).get("status") == "OPEN":
                        open_count += 1
                except:
                    pass
    
    max_open = cfg.get("max_open_trades", 3)
    if open_count >= max_open:
        log(f"max_open_trades erreicht ({open_count}/{max_open}) — Skip Scan")
        return

    open_clusters = get_open_clusters()

    for pair in pairs:
        if pair in open_pairs:
            log(f"{pair}: Bereits offener Trade aktiv — skip")
            continue

        cluster = CORR_CLUSTERS.get(pair)
        if cluster and cluster in open_clusters:
            log(f"{pair}: Cluster {cluster} bereits offen — skip")
            continue

        cooldown = get_pair_cooldown_remaining(pair, hours=4)
        if cooldown > 0:
            log(f"{pair}: Cooldown aktiv ({cooldown//60}m verbleibend) — skip")
            continue
        
        pair_prefs = strat_map.get(pair, {})
        pair_tf = pair_prefs.get("timeframe", "HOUR_1")
        analysis, err = analyze_pair(pair, pair_tf, strat_map)
        if err:
            log(f"{pair}: {err}")
            continue
        
        setup_names = ", ".join([f"{s['name']}({s['conf']})" for s in analysis.get("all_setups", [])[:3]])
        log(f"{pair} | {analysis['price']:.4f} | Regime={analysis['regime']} | Trend={analysis['trend']} | Best={analysis['setup']} | Conf={analysis['confidence']}% | Setups=[{setup_names}]")
        
        strategy_name = analysis['setup']
        # Strategie-Pause & Pair-Pause — DEAKTIVIERT für Setup-Performance-Test
        # if check_strategy_pause(pair, strategy_name):
        #     log(f"{pair}: Strategie {strategy_name} pausiert (3+ Losses) — skip")
        #     continue
        # if check_pair_pause(pair):
        #     log(f"{pair}: Pair pausiert (4+ Losses in Folge) — skip")
        #     continue
        
        trade = build_trade(analysis, cfg, pair_prefs)
        if trade:
            qty = calculate_position_size(trade["entry"], trade["stop_loss"], trade["risk_pct"])
            trade["qty"] = qty
            signals.append(trade)

    if not signals:
        log("Kein Setup")
        return

    signals.sort(key=lambda x: (x["confidence"], x["risk_reward"]), reverse=True)
    best = signals[0]

    best_pair_prefs = strat_map.get(best["pair"], {})
    min_conf_for_signal = best_pair_prefs.get("min_confidence", cfg.get("min_confidence", 70))
    if best["confidence"] >= min_conf_for_signal:
        report(f"SIGNAL: {best['pair']} {best['direction']} | Entry: {best['entry']:.4f} | SL: {best['stop_loss']:.4f} | TP: {best['take_profit']:.4f} | Conf: {best['confidence']}% | Setup: {best['setup_type']} | Regime: {best.get('regime', 'N/A')}")
        if cfg.get("paper_mode"):
            log(f"[PAPER ORDER] {best['direction']} {best['pair']} @ {best['entry']:.4f}")
            log_trade(best)
            report(f"TRADE OPENED: {best['pair']} {best['direction']} @ {best['entry']:.4f} | SL: {best['stop_loss']:.4f} | TP: {best['take_profit']:.4f} | Setup: {best['setup_type']} | Regime: {best.get('regime', 'N/A')}")
        else:
            log("LIVE MODE wäre aktiv - paper_mode=False setzen")
    else:
        log("Kein Setup")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    cfg = load_config()
    log(f"v3.0 | Paper={cfg.get('paper_mode')} | Pairs={cfg['pairs']}")

    simulate_outcomes()

    # Daily / Weekly Loss Limit — DEAKTIVIERT für Setup-Performance-Test
    # if not check_daily_loss(cfg)[0]:
    #     log("Daily Loss Limit erreicht — Scan abgebrochen")
    #     return
    # if not check_weekly_loss(cfg):
    #     log("Weekly Loss Limit erreicht — Scan abgebrochen")
    #     return

    run_scan(cfg)
    stats = get_stats()
    log(f"Stats: {stats}")

if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
