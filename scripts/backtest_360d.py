#!/usr/bin/env python3
"""
Hermes Backtester v1.1 — 360 Tage Walk-Forward (optimiert)
Vorab-Berechnung aller Indikatoren für O(n) statt O(n²).
"""

import urllib.request
import urllib.error
import json
import os
import math
import time
from datetime import timezone, datetime

BASE_URL = "https://api.poloniex.com"
CACHE_DIR = os.path.expanduser("~/.hermes_trader/backtests/cache")
RESULT_DIR = os.path.expanduser("~/.hermes_trader/backtests")
CONFIG_PATH = os.path.expanduser("~/.hermes_trader/config.json")
STRATEGY_MAP_PATH = os.path.expanduser("~/.hermes_trader/engine/strategy_map_v3.json")

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)

def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

CONFIG = load_json(CONFIG_PATH)
STRATEGY_MAP_FULL = load_json(STRATEGY_MAP_PATH)
# Unterstützt sowohl flache als auch verschachtelte {"pairs": {...}} Struktur
if "pairs" in STRATEGY_MAP_FULL:
    STRATEGY_MAP = STRATEGY_MAP_FULL["pairs"]
else:
    STRATEGY_MAP = {k: v for k, v in STRATEGY_MAP_FULL.items() if not k.startswith("_")}
PAIRS = CONFIG.get("pairs", ["BTC_USDT", "ETH_USDT", "DOGE_USDT", "NEAR_USDT"])

# ---------------------------------------------------------------------------
# KERZEN-LADEN (mit Cache)
# ---------------------------------------------------------------------------
def fetch_candles(symbol, interval, start_ms, end_ms):
    cache_file = os.path.join(CACHE_DIR, f"{symbol}_{interval}_{start_ms}_{end_ms}.json")
    if os.path.exists(cache_file):
        with open(cache_file, "r") as f:
            return json.load(f)
    all_candles = []
    chunk = 500
    current_end = end_ms
    while current_end > start_ms:
        ep = f"/markets/{symbol}/candles?interval={interval}&startTime={start_ms}&endTime={current_end}&limit={chunk}"
        req = urllib.request.Request(BASE_URL + ep, headers={"User-Agent": "HermesBacktest/1.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            if not isinstance(data, list) or len(data) == 0:
                break
            all_candles.extend(data)
            current_end = data[0][12] - 1
            time.sleep(0.25)
        except Exception as e:
            print(f"  [API-Fehler] {symbol} {interval}: {e}")
            break
    all_candles.sort(key=lambda x: x[12])
    seen = set()
    deduped = []
    for c in all_candles:
        sid = c[12]
        if sid not in seen:
            seen.add(sid)
            deduped.append(c)
    with open(cache_file, "w") as f:
        json.dump(deduped, f)
    return deduped

def to_ohlcv(candles):
    return [{"time": c[9], "open": float(c[2]), "high": float(c[1]),
             "low": float(c[0]), "close": float(c[3]), "volume": float(c[5])} for c in candles]

# ---------------------------------------------------------------------------
# INDIKATOREN (vektorisiert für ganze Liste)
# ---------------------------------------------------------------------------
def ema(closes, period):
    n = len(closes)
    if n < period:
        return [None] * n
    k = 2 / (period + 1)
    r = [None] * (period - 1)
    ema_val = sum(closes[:period]) / period
    r.append(ema_val)
    for c in closes[period:]:
        ema_val = c * k + ema_val * (1 - k)
        r.append(ema_val)
    return r

def rsi(closes, period=14):
    n = len(closes)
    if n < period + 1:
        return [None] * n
    gains, losses = [], []
    for i in range(1, n):
        gains.append(max(closes[i] - closes[i - 1], 0))
        losses.append(abs(min(closes[i] - closes[i - 1], 0)))
    r = [None] * period
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    rs = avg_g / avg_l if avg_l != 0 else float('inf')
    r.append(100 - (100 / (1 + rs)))
    for i in range(period, n - 1):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l != 0 else float('inf')
        r.append(100 - (100 / (1 + rs)))
    return r

def atr(data, period=14):
    n = len(data)
    if n < period + 1:
        return [None] * n
    r = [None]
    trs = []
    for i in range(1, n):
        tr = max(data[i]["high"] - data[i]["low"],
                 abs(data[i]["high"] - data[i - 1]["close"]),
                 abs(data[i]["low"] - data[i - 1]["close"]))
        trs.append(tr)
        if i == 1:
            r.append(None)
        elif len(trs) < period:
            r.append(None)
        elif len(trs) == period:
            r.append(sum(trs) / period)
        else:
            r.append((r[-1] * (period - 1) + trs[-1]) / period)
    return r

def macd(closes, fast=12, slow=26, signal=9):
    n = len(closes)
    if n < slow:
        return [None] * n, [None] * n, [None] * n
    def _e(c, p):
        k = 2 / (p + 1)
        v = [sum(c[:p]) / p]
        for x in c[p:]:
            v.append(x * k + v[-1] * (1 - k))
        return v
    ef, es = _e(closes, fast), _e(closes, slow)
    # Korrekt: Beide EMAs auf gleichen Zeitpunkt ausrichten (ab Bar slow-1)
    ml = [f - s for f, s in zip(ef[slow - fast:], es)]
    sig = _e(ml, signal)
    hist = [m - s for m, s in zip(ml[-len(sig):], sig)]
    pad = n - len(ml)
    return [None] * pad + ml, [None] * (n - len(sig)) + sig, [None] * (n - len(hist)) + hist

def bbands(closes, period=20, mult=2.0):
    n = len(closes)
    if n < period:
        return [None] * n, [None] * n, [None] * n
    m, u, l = [], [], []
    for i in range(period - 1, n):
        w = closes[i - period + 1:i + 1]
        ma = sum(w) / period
        sd = math.sqrt(sum((c - ma) ** 2 for c in w) / period)
        m.append(ma)
        u.append(ma + mult * sd)
        l.append(ma - mult * sd)
    pad = n - len(m)
    return [None] * pad + m, [None] * pad + u, [None] * pad + l

def rolling_vwap(closes, volumes, window=50):
    n = len(closes)
    out = [None] * n
    for i in range(window - 1, n):
        vol = sum(volumes[i - window + 1:i + 1])
        pv = sum(closes[j] * volumes[j] for j in range(i - window + 1, i + 1))
        out[i] = pv / vol if vol > 0 else None
    return out

def stochastic(highs, lows, closes, k_period=14, d_period=3):
    """Stochastic Oscillator %K und %D."""
    n = len(closes)
    if n < k_period:
        return [None] * n, [None] * n
    k_vals = [None] * (k_period - 1)
    for i in range(k_period - 1, n):
        window_low = min(lows[i - k_period + 1:i + 1])
        window_high = max(highs[i - k_period + 1:i + 1])
        range_ = window_high - window_low
        if range_ == 0:
            k_vals.append(50.0)
        else:
            k_vals.append((closes[i] - window_low) / range_ * 100)
    d_vals = [None] * (k_period + d_period - 2)
    for i in range(k_period + d_period - 2, n):
        d_window = k_vals[i - d_period + 1:i + 1]
        d_vals.append(sum(d_window) / d_period)
    return k_vals, d_vals

def williams_r(highs, lows, closes, period=14):
    """Williams %R."""
    n = len(closes)
    if n < period:
        return [None] * n
    r = [None] * (period - 1)
    for i in range(period - 1, n):
        window_low = min(lows[i - period + 1:i + 1])
        window_high = max(highs[i - period + 1:i + 1])
        range_ = window_high - window_low
        if range_ == 0:
            r.append(-50.0)
        else:
            r.append((window_high - closes[i]) / range_ * -100)
    return r

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

def detect_regime(ohlcv, closes, ema50):
    """
    Erkennt Markt-Regime pro Pair basierend auf:
    - ADX(14): Trendstärke
    - BB-Width(20,2): Volatilität/Squeeze
    - EMA50-Slope(10): Trendrichtung
    """
    if len(ohlcv) < 55 or len(closes) < 55:
        return "TRANSITION"
    adx_vals = adx(ohlcv, 14)
    adx_now = adx_vals[-1] if adx_vals and adx_vals[-1] is not None else 0
    bb_mid, bb_up, bb_low = bbands(closes, 20, 2.0)
    bbw = 0
    if bb_mid[-1] and bb_mid[-1] > 0 and bb_up[-1] and bb_low[-1]:
        bbw = (bb_up[-1] - bb_low[-1]) / bb_mid[-1] * 100
    ema_slope = 0
    if ema50[-1] and ema50[-10]:
        ema_slope = (ema50[-1] - ema50[-10]) / ema50[-10] * 100
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

def swing_highs_lows(highs, lows, lookback=3):
    n = len(highs)
    swing_h, swing_l = [], []
    for i in range(lookback, n):
        if highs[i] >= max(highs[j] for j in range(i - lookback, i + 1) if j != i):
            swing_h.append(i)
        if lows[i] <= min(lows[j] for j in range(i - lookback, i + 1) if j != i):
            swing_l.append(i)
    return swing_h, swing_l

def find_order_blocks(ohlcv, min_impulse_pct=1.5, lookback=50):
    zones = {"demand": [], "supply": []}
    if len(ohlcv) < 10:
        return zones
    n = len(ohlcv)
    for i in range(n - 4, max(0, n - lookback), -1):
        c0, c1, c2, c3 = ohlcv[i], ohlcv[i + 1], ohlcv[i + 2], ohlcv[i + 3]
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
    ext_low = zone["low"] * (1 - threshold_pct / 100)
    ext_high = zone["high"] * (1 + threshold_pct / 100)
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

# ---------------------------------------------------------------------------
# VORAB-BERECHNUNG ALLER INDIKATOREN
# ---------------------------------------------------------------------------
def precompute_all(ohlcv, htf_ohlcv, symbol, strat_map):
    n = len(ohlcv)
    closes = [c["close"] for c in ohlcv]
    highs = [c["high"] for c in ohlcv]
    lows = [c["low"] for c in ohlcv]
    volumes = [c["volume"] for c in ohlcv]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)
    rsi14 = rsi(closes, 14)
    atr14 = atr(ohlcv, 14)
    macd_line, sig_line, hist = macd(closes)
    bb_mid, bb_up, bb_low = bbands(closes)
    vwap = rolling_vwap(closes, volumes, 50)
    stoch_k, stoch_d = stochastic(highs, lows, closes)
    will_r_vals = williams_r(highs, lows, closes)

    pair_prefs = strat_map.get(symbol, {})
    lb = pair_prefs.get("lh_swing_lb", 2)
    swing_h_all, swing_l_all = swing_highs_lows(highs, lows, lookback=lb)

    # HTF-Trend vorab berechnen pro H1-Index
    htf_trend_per_i = ["NEUTRAL"] * n
    htf_zones_per_i = [None] * n

    if htf_ohlcv and len(htf_ohlcv) >= 50:
        htf_closes = [c["close"] for c in htf_ohlcv]
        htf_ema9 = ema(htf_closes, 9)
        htf_ema21 = ema(htf_closes, 21)
        htf_ema50 = ema(htf_closes, 50)
        htf_trend_per_h4 = []
        for idx in range(len(htf_ohlcv)):
            if idx < 50 or htf_ema9[idx] is None:
                htf_trend_per_h4.append("NEUTRAL")
            elif htf_ema9[idx] > htf_ema21[idx] > htf_ema50[idx]:
                htf_trend_per_h4.append("BULLISH")
            elif htf_ema9[idx] < htf_ema21[idx] < htf_ema50[idx]:
                htf_trend_per_h4.append("BEARISH")
            else:
                htf_trend_per_h4.append("NEUTRAL")

        htf_zones_per_h4 = []
        for idx in range(len(htf_ohlcv)):
            if idx < 10:
                htf_zones_per_h4.append({"demand": [], "supply": []})
            else:
                hslice = htf_ohlcv[max(0, idx - 39):idx]
                htf_zones_per_h4.append(find_order_blocks(hslice, min_impulse_pct=1.0, lookback=40))

        # Mapping H1 -> H4
        h4_idx_for_h1 = [None] * n
        h4_ptr = 0
        h4_len = len(htf_ohlcv)
        for i in range(n):
            t = ohlcv[i]["time"]
            while h4_ptr < h4_len and not (htf_ohlcv[h4_ptr]["time"] <= t < htf_ohlcv[h4_ptr]["time"] + 4 * 3600 * 1000):
                h4_ptr += 1
            if h4_ptr < h4_len:
                h4_idx_for_h1[i] = h4_ptr

        for i in range(n):
            h4i = h4_idx_for_h1[i]
            if h4i is not None:
                htf_trend_per_i[i] = htf_trend_per_h4[h4i]
                htf_zones_per_i[i] = htf_zones_per_h4[h4i]

    # Regime pro Bar vorab berechnen (O(n) statt O(n²))
    adx_vals = adx(ohlcv, 14)
    regime_per_i = ["TRANSITION"] * n
    for i in range(55, n):
        adx_now = adx_vals[i] if adx_vals[i] is not None else 0
        bbw = 0
        if bb_mid[i] and bb_mid[i] > 0 and bb_up[i] and bb_low[i]:
            bbw = (bb_up[i] - bb_low[i]) / bb_mid[i] * 100
        ema_slope = 0
        if ema50[i] and i >= 10 and ema50[i - 10]:
            ema_slope = (ema50[i] - ema50[i - 10]) / ema50[i - 10] * 100
        if adx_now > 25 and bbw > 3.0:
            if ema_slope > 0.5:
                regime_per_i[i] = "TRENDING_BULL"
            elif ema_slope < -0.5:
                regime_per_i[i] = "TRENDING_BEAR"
        elif adx_now < 20 and bbw < 3.0:
            regime_per_i[i] = "SQUEEZE_RANGE"
        elif adx_now < 20 and 3.0 <= bbw <= 6.0:
            regime_per_i[i] = "NEUTRAL_RANGE"
        elif bbw > 6.0:
            regime_per_i[i] = "VOLATILE"
        else:
            regime_per_i[i] = "TRANSITION"

    return {
        "closes": closes, "highs": highs, "lows": lows, "volumes": volumes,
        "ema9": ema9, "ema21": ema21, "ema50": ema50,
        "rsi14": rsi14, "atr14": atr14,
        "macd_line": macd_line, "sig_line": sig_line,
        "bb_up": bb_up, "bb_low": bb_low, "bb_mid": bb_mid,
        "vwap": vwap,
        "stoch_k": stoch_k, "stoch_d": stoch_d,
        "will_r": will_r_vals,
        "swing_h": swing_h_all, "swing_l": swing_l_all,
        "htf_trend": htf_trend_per_i,
        "htf_zones": htf_zones_per_i,
        "regime": regime_per_i,
    }

# ---------------------------------------------------------------------------
# SETUP-ERKENNUNG AN INDEX i (nur Lookups)
# ---------------------------------------------------------------------------
def analyze_pair_backtest_fast(ohlcv, pre, i, symbol, strat_map):
    if i < 55:
        return None, "Zu wenig Daten"

    current = ohlcv[i]
    prev = ohlcv[i - 1]
    closes = pre["closes"]
    highs = pre["highs"]
    lows = pre["lows"]
    volumes = pre["volumes"]

    e9 = pre["ema9"][i]
    e21 = pre["ema21"][i]
    e50 = pre["ema50"][i]
    e9_prev = pre["ema9"][i - 1]
    e21_prev = pre["ema21"][i - 1]

    if e9 is None or e21 is None or e50 is None:
        return None, "Indikatoren nicht bereit"

    trend = "NEUTRAL"
    if e9 > e21 > e50:
        trend = "BULLISH"
    elif e9 < e21 < e50:
        trend = "BEARISH"

    macd_line_i = pre["macd_line"][i]
    sig_line_i = pre["sig_line"][i]
    macd_line_prev = pre["macd_line"][i - 1]
    sig_line_prev = pre["sig_line"][i - 1]

    momentum = "NEUTRAL"
    if macd_line_i is not None and sig_line_i is not None and macd_line_prev is not None and sig_line_prev is not None:
        if macd_line_i > sig_line_i and macd_line_prev <= sig_line_prev:
            momentum = "BULLISH_CROSS"
        elif macd_line_i < sig_line_i and macd_line_prev >= sig_line_prev:
            momentum = "BEARISH_CROSS"
        elif macd_line_i > sig_line_i:
            momentum = "BULLISH"
        else:
            momentum = "BEARISH"

    rsi_val = pre["rsi14"][i]
    rsi_state = "NEUTRAL"
    if rsi_val is not None:
        if rsi_val > 70:
            rsi_state = "OVERBOUGHT"
        elif rsi_val < 30:
            rsi_state = "OVERSOLD"

    bb_up_i = pre["bb_up"][i]
    bb_low_i = pre["bb_low"][i]
    bb_mid_i = pre["bb_mid"][i]
    bb_state = "NEUTRAL"
    if bb_low_i is not None and current["close"] <= bb_low_i:
        bb_state = "LOWER_TOUCH"
    elif bb_up_i is not None and current["close"] >= bb_up_i:
        bb_state = "UPPER_TOUCH"

    atr_val = pre["atr14"][i] if pre["atr14"][i] is not None else current["close"] * 0.01
    vwap_val = pre["vwap"][i]
    htf_trend = pre["htf_trend"][i]
    htf_zones = pre["htf_zones"][i] if pre["htf_zones"][i] else {"demand": [], "supply": []}
    regime = pre["regime"][i]

    setups = []
    pair_prefs = strat_map.get(symbol, {})
    avoid = pair_prefs.get("avoid", [])
    regime_prefs = pair_prefs.get(regime, {})
    allowed_strategies = regime_prefs.get("strategies", None)
    regime_conf_boost = regime_prefs.get("confidence_boost", 0)

    ema_dist_min = pair_prefs.get("ema_dist_min", 0.3)
    ema_dist = abs(e9 - e21) / e21 * 100 if e21 else 0
    if ema_dist < ema_dist_min:
        return None, f"Trend zu schwach ({ema_dist:.2f}%)"

    vol_mult = pair_prefs.get("volume_mult", 1.2)
    if len(volumes) >= 21:
        avg_vol = sum(volumes[i - 20:i]) / 20
        if avg_vol > 0 and volumes[i - 1] / avg_vol < vol_mult:
            return None, f"Volumen zu niedrig ({volumes[i-1]/avg_vol:.2f}x)"

    def add_setup(name, direction, conf, cond=True):
        if name in avoid:
            return
        if not cond:
            return
        # H1-Trend-Guard: LONG nur bei BULLISH, SHORT nur bei BEARISH
        if trend != "BULLISH" and direction == "LONG":
            return
        if trend != "BEARISH" and direction == "SHORT":
            return
        # HTF-Trend-Filter
        if htf_trend == "BULLISH" and direction == "SHORT":
            return
        if htf_trend == "BEARISH" and direction == "LONG" and trend != "BULLISH":
            return
        # Regime-Filter (sogar leere Liste blockt)
        if allowed_strategies is not None and name not in allowed_strategies:
            return
        # Regime Confidence-Boost
        conf = conf + regime_conf_boost
        setups.append({"name": name, "direction": direction, "confidence": conf})

    # 1) Trendfolge
    if rsi_state in ("NEUTRAL", "OVERSOLD"):
        conf = 75
        if bb_state == "LOWER_TOUCH":
            conf += 10
        add_setup("TREND_FOLLOW_LONG", "LONG", min(conf, 95))
    if trend == "BEARISH" and momentum != "BULLISH_CROSS" and rsi_val is not None and rsi_val >= 40:
        conf = 70
        if momentum in ("BEARISH", "BEARISH_CROSS"):
            conf = 75
        if bb_state == "UPPER_TOUCH":
            conf += 10
        add_setup("TREND_FOLLOW_SHORT", "SHORT", min(conf, 90))
    elif trend == "BEARISH" and momentum == "NEUTRAL" and rsi_val is not None and rsi_val >= 40:
        conf = 65
        add_setup("TREND_FOLLOW_SHORT", "SHORT", min(conf, 85))

    # 2) Mean Reversion
    mrl = pair_prefs.get("mean_rev_rsi_long", 25)
    mrs = pair_prefs.get("mean_rev_rsi_short", 70)
    if rsi_val is not None and rsi_val < mrl and bb_state == "LOWER_TOUCH":
        add_setup("MEAN_REVERSION_LONG", "LONG", 65)
    if rsi_val is not None and rsi_val > mrs and bb_state == "UPPER_TOUCH":
        add_setup("MEAN_REVERSION_SHORT", "SHORT", 65)

    # 3) EMA Bounce
    ema_bounce_dist = pair_prefs.get("ema_bounce_dist", 0.5)
    dist21 = abs(current["close"] - e21) / current["close"] * 100 if e21 else 999
    if dist21 < ema_bounce_dist and current["close"] > e21 and current["low"] <= e21:
        add_setup("EMA_BOUNCE_LONG", "LONG", 60)
    bounce_short_rsi = pair_prefs.get("ema_bounce_short_rsi_min", 40)
    if dist21 < ema_bounce_dist and current["close"] < e21 and current["high"] >= e21 and rsi_val is not None and rsi_val >= bounce_short_rsi:
        add_setup("EMA_BOUNCE_SHORT", "SHORT", 60)

    # 4) Bollinger Band Bounce (BB_BOUNCE_LONG)
    bb_bounce_dist = pair_prefs.get("bb_bounce_dist", 0.5)
    if bb_low_i is not None and bb_up_i is not None:
        # LONG: Preis berührt/unterschreitet bb_low und schließt wieder darüber
        if current["low"] <= bb_low_i * (1 + bb_bounce_dist/100) and current["close"] > bb_low_i and current["close"] < bb_mid_i:
            if rsi_val is not None and rsi_val < 60:
                add_setup("BB_BOUNCE_LONG", "LONG", 65)
        # SHORT: Preis berührt/überschreitet bb_up und schließt wieder darunter
        if current["high"] >= bb_up_i * (1 - bb_bounce_dist/100) and current["close"] < bb_up_i and current["close"] > bb_mid_i:
            if rsi_val is not None and rsi_val > 40:
                add_setup("BB_BOUNCE_SHORT", "SHORT", 60)

    # 5) VWAP Retest (VWAP_RETAIL_LONG nur bei RSI < 45)
    vwap_dist = pair_prefs.get("vwap_dist", 0.3)
    if vwap_val is not None:
        dv = abs(current["close"] - vwap_val) / current["close"] * 100
        if dv < vwap_dist and current["close"] > vwap_val and prev["close"] <= vwap_val and rsi_val is not None and rsi_val < 45:
            add_setup("VWAP_RETAIL_LONG", "LONG", 65)
        vwap_short_rsi = pair_prefs.get("vwap_short_rsi_min", 40)
        if dv < vwap_dist and current["close"] < vwap_val and prev["close"] >= vwap_val and rsi_val is not None and rsi_val >= vwap_short_rsi:
            add_setup("VWAP_RETAIL_SHORT", "SHORT", 65)

    # 5) RSI Divergenz
    rsi_div_long_max = pair_prefs.get("rsi_div_rsi_long_max", 45)
    rsi_div_short_min = pair_prefs.get("rsi_div_rsi_short_min", 55)
    rsi5 = pre["rsi14"][i - 5] if i >= 5 else None
    if rsi_val is not None and rsi5 is not None and current["close"] < closes[i - 5] and rsi_val > rsi5 and rsi_val < rsi_div_long_max:
        add_setup("RSI_DIVERGENCE_LONG", "LONG", 70)
    if rsi_val is not None and rsi5 is not None and current["close"] > closes[i - 5] and rsi_val < rsi5 and rsi_val > rsi_div_short_min:
        add_setup("RSI_DIVERGENCE_SHORT", "SHORT", 70)

    # 6) Lower High Break
    if trend == "BEARISH" and i >= 30:
        swing_h_past = [s for s in pre["swing_h"] if s < i]
        swing_l_past = [s for s in pre["swing_l"] if s < i]
        if len(swing_h_past) >= 2 and len(swing_l_past) >= 2:
            last_sh, prev_sh = swing_h_past[-1], swing_h_past[-2]
            last_sl, prev_sl = swing_l_past[-1], swing_l_past[-2]
            if highs[last_sh] < highs[prev_sh] * 0.995 and lows[last_sl] <= lows[prev_sl] * 1.005:
                if current["close"] < lows[last_sl]:
                    if rsi_val is None or rsi_val >= 25:
                        conf = pair_prefs.get("lh_conf_base", 65)
                        add_setup("LOWER_HIGH_BREAK", "SHORT", conf)

    # 7) EMA Compression Break
    if trend == "BEARISH" and i >= 55:
        ec_lookback = pair_prefs.get("ec_lookback", 8)
        ec_compress = pair_prefs.get("ec_compress", 0.6)
        ec_vol_mult = pair_prefs.get("ec_vol_mult", 1.5)
        compressed_count = 0
        for j in range(-ec_lookback, 0):
            idx_j = i + 1 + j
            ej9 = pre["ema9"][idx_j]
            ej21 = pre["ema21"][idx_j]
            ej50 = pre["ema50"][idx_j]
            if ej9 is None or ej21 is None or ej50 is None:
                continue
            max_em = max(ej9, ej21, ej50)
            min_em = min(ej9, ej21, ej50)
            if (max_em - min_em) / min_em * 100 < ec_compress:
                compressed_count += 1
        if compressed_count >= ec_lookback // 2:
            if current["close"] < min(pre["ema9"][i], pre["ema21"][i], pre["ema50"][i]):
                avg_vol = sum(volumes[i - 20:i]) / 20 if len(volumes) >= 21 else None
                if avg_vol and volumes[i] >= avg_vol * ec_vol_mult:
                    cross_ok = False
                    for j in range(i - 3, i + 1):
                        if j > 0 and pre["ema9"][j] < pre["ema21"][j] and pre["ema9"][j - 1] >= pre["ema21"][j - 1]:
                            cross_ok = True
                            break
                    if not cross_ok and pre["ema9"][i] < pre["ema21"][i]:
                        cross_ok = True
                    if cross_ok:
                        conf = 65
                        if volumes[i] / avg_vol > 2.0:
                            conf += 10
                        add_setup("EMA_COMPRESSION_BREAK", "SHORT", min(conf, 90))

    # 8) Stochastic Mean Reversion (STOCH_MR_LONG deaktiviert - 0% WR in Backtest)
    stoch_k_i = pre["stoch_k"][i]
    stoch_d_i = pre["stoch_d"][i]
    if stoch_k_i is not None and stoch_d_i is not None:
        # SHORT: Overbought-Reversal
        stoch_falling = stoch_k_i < (pre["stoch_k"][i-1] if i > 0 and pre["stoch_k"][i-1] is not None else stoch_k_i)
        if stoch_k_i > 75 and stoch_k_i < stoch_d_i and stoch_falling:
            conf = 70
            if trend == "BEARISH":
                conf += 10
            if bb_up_i is not None and current["close"] > bb_up_i:
                conf += 5
            add_setup("STOCH_MR_SHORT", "SHORT", min(conf, 90))

    # 9) Williams %R Extreme Mean Reversion
    will_r_vals = pre["will_r"]
    if will_r_vals is not None and i >= 3 and will_r_vals[i] is not None and will_r_vals[i-3] is not None:
        wr_now = will_r_vals[i]
        wr_slope = will_r_vals[i] - will_r_vals[i-3]
        # LONG
        if wr_now < -85 and wr_slope > -0.5 and momentum != "BEARISH" and momentum != "BEARISH_CROSS":
            conf = 70
            if trend == "BULLISH":
                conf += 10
            add_setup("WILLR_LONG", "LONG", min(conf, 90))
        # SHORT
        if wr_now > -15 and wr_slope < 0.5 and momentum != "BULLISH" and momentum != "BULLISH_CROSS":
            conf = 70
            if trend == "BEARISH":
                conf += 10
            add_setup("WILLR_SHORT", "SHORT", min(conf, 90))

    # 10) Bollinger Squeeze Breakout
    if bb_up_i is not None and bb_low_i is not None and bb_mid_i is not None and bb_mid_i > 0:
        bb_width = (bb_up_i - bb_low_i) / bb_mid_i * 100
        bb_width_hist = []
        for j in range(max(0, i - 13), i + 1):
            if pre["bb_up"][j] is not None and pre["bb_low"][j] is not None and pre["bb_mid"][j] is not None and pre["bb_mid"][j] > 0:
                bb_width_hist.append((pre["bb_up"][j] - pre["bb_low"][j]) / pre["bb_mid"][j] * 100)
        if bb_width_hist and bb_width == min(bb_width_hist) and bb_width < 4.0:
            # Validierung: Preis in unterer Hälfte + bullische Kerze (close > open)
            if current["close"] < bb_mid_i and trend != "BEARISH" and current["close"] > current["open"]:
                add_setup("BB_SQUEEZE_LONG", "LONG", 75)

    # Primary/Secondary Boost
    primary = pair_prefs.get("primary", "")
    secondary = pair_prefs.get("secondary", "")
    for s in setups:
        if s["name"] == primary:
            s["confidence"] = min(95, s["confidence"] + 10)
        elif s["name"] == secondary:
            s["confidence"] = min(90, s["confidence"] + 5)

    # S/D Zone Confluence
    active_zone = None
    zone_type = None
    price = current["close"]
    for s in setups:
        if s["direction"] == "LONG":
            for z in htf_zones["demand"]:
                if is_near_zone(price, z, threshold_pct=0.8):
                    s["confidence"] = min(100, s["confidence"] + 15)
                    active_zone = z
                    zone_type = "demand"
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
                    active_zone = z
                    zone_type = "supply"
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
            group.sort(key=lambda x: x["confidence"], reverse=True)
            best_signal = group[0]
            boost = min(15, (len(group) - 1) * 5)
            best_signal["confidence"] = min(100, best_signal["confidence"] + boost)

    if not setups:
        return None, f"Kein Setup (Trend={trend}, H4={htf_trend})"

    best = max(setups, key=lambda x: x["confidence"])

    return {
        "symbol": symbol, "timeframe": pair_prefs.get("timeframe", "HOUR_1"),
        "timestamp": current["time"],
        "price": current["close"], "vwap": vwap_val, "trend": trend,
        "htf_trend": htf_trend, "momentum": momentum, "rsi": rsi_val,
        "rsi_state": rsi_state, "bb_state": bb_state, "atr": atr_val,
        "setup": best["name"], "direction": best["direction"],
        "confidence": best["confidence"],
        "all_setups": [s["name"] for s in setups],
        "htf_zones": htf_zones,
        "active_zone": active_zone,
        "zone_type": zone_type,
        "regime": regime,
    }, None

# ---------------------------------------------------------------------------
# TRADE-BUILDER
# ---------------------------------------------------------------------------
def build_trade(analysis, cfg, strat_map_override=None):
    if not analysis or not analysis.get("setup"):
        return None

    symbol = analysis["symbol"]
    setup_name = analysis["setup"]
    direction = analysis["direction"]

    # Pair-Defaults laden
    pair_prefs = (strat_map_override or STRATEGY_MAP).get(symbol, {})
    strat_params = pair_prefs.get("strategy_params", {}).get(setup_name, {})

    # Min Confidence: Strategie-spezifisch > Pair-Default > Global-Default
    min_conf = strat_params.get("min_confidence",
                   pair_prefs.get("min_confidence",
                     cfg.get("min_confidence", 70)))
    if analysis["confidence"] < min_conf:
        return None

    # SL/TP Multiplikatoren: Strategie-spezifisch > Pair-Default > Global-Default
    sl_mult = strat_params.get("sl_atr_mult",
                   pair_prefs.get("sl_atr_mult",
                     cfg.get("sl_atr_mult", 1.5)))
    tp_mult = strat_params.get("tp_atr_mult",
                   pair_prefs.get("tp_atr_mult",
                     cfg.get("tp_atr_mult", 3.0)))

    # Time-Stop: Strategie-spezifisch > Pair-Default > Global-Default
    time_stop_key = "time_stop_bullish" if direction == "LONG" else "time_stop_bearish"
    time_stop = strat_params.get("time_stop",
                     pair_prefs.get(time_stop_key,
                       cfg.get(time_stop_key, 48)))

    # NEU: Momentum-Filter für SHORTs (ETH-spezifisch)
    if symbol == "ETH_USDT" and direction == "SHORT" and analysis.get("momentum") == "BULLISH":
        return None

    # H4-Trend-Filter (paar-spezifisch, default: true für non-BTC)
    filters = pair_prefs.get("filters", {})
    htf_trend = analysis.get("htf_trend", "NEUTRAL")
    block_htf_default = symbol != "BTC_USDT"
    if filters.get("block_long_on_bearish_htf", block_htf_default) and htf_trend == "BEARISH" and direction == "LONG":
        return None

    # RSI-Filter (paar-spezifisch, default: true)
    rsi = analysis.get("rsi")
    if filters.get("block_long_on_rsi_50_59", True) and direction == "LONG" and rsi is not None and 50 <= rsi < 60:
        return None

    price = analysis["price"]
    atr_val = analysis["atr"]
    base_risk = cfg.get("risk_per_trade_pct", 1.0)
    risk_pct = base_risk

    # Regime-Risk-Multiplikator
    regime = analysis.get("regime", "TRANSITION")
    regime_prefs = pair_prefs.get(regime, {})
    regime_risk_mult = regime_prefs.get("risk_mult", 1.0)
    risk_pct = base_risk * regime_risk_mult

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

    return {
        "pair": analysis["symbol"], "direction": direction,
        "entry": round(entry, 8), "stop_loss": round(sl, 8),
        "take_profit": round(tp, 8), "risk_pct": risk_pct,
        "risk_reward": round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0,
        "time_stop": time_stop,
        "timeframe": analysis["timeframe"], "setup_type": setup_name,
        "confidence": analysis["confidence"], "atr": round(atr_val, 8),
        "timestamp": analysis["timestamp"], "trend": analysis["trend"],
        "htf_trend": analysis.get("htf_trend", "NEUTRAL"),
        "momentum": analysis["momentum"], "rsi": round(analysis["rsi"], 2) if analysis["rsi"] else None,
        "rsi_state": analysis["rsi_state"], "bb_state": analysis["bb_state"],
        "status": "OPEN",
        "regime": regime,
    }

# ---------------------------------------------------------------------------
# BACKTEST ENGINE
# ---------------------------------------------------------------------------
def run_pair_backtest(symbol, ohlcv, htf_ohlcv, strat_map, cfg):
    trades = []
    open_trade = None
    cooldown_bars = 0

    pair_prefs = strat_map.get(symbol, {})
    pair_tf = pair_prefs.get("timeframe", "HOUR_1")
    bar_step = 4 if pair_tf == "HOUR_4" else 1
    start_idx = 55

    # Pair-spezifische Config mit Fallback auf Global-Config
    pair_cfg = dict(cfg)
    pair_cfg["sl_atr_mult"] = pair_prefs.get("sl_atr_mult", cfg.get("sl_atr_mult", 1.5))
    pair_cfg["tp_atr_mult"] = pair_prefs.get("tp_atr_mult", cfg.get("tp_atr_mult", 3.0))
    pair_cfg["time_stop_bullish"] = pair_prefs.get("time_stop_bullish", cfg.get("time_stop_bullish", 48))
    pair_cfg["time_stop_bearish"] = pair_prefs.get("time_stop_bearish", cfg.get("time_stop_bearish", 24))
    pair_cfg["min_confidence"] = pair_prefs.get("min_confidence", cfg.get("min_confidence", 60))

    print(f"  Vorab-Berechnung {symbol}...")
    pre = precompute_all(ohlcv, htf_ohlcv, symbol, strat_map)
    print(f"  Backtest-Simulation {symbol}...")

    n = len(ohlcv)
    for i in range(start_idx, n, bar_step):
        candle = ohlcv[i]

        # Offenen Trade prüfen
        if open_trade:
            entry = open_trade["entry"]
            sl = open_trade["stop_loss"]
            tp = open_trade["take_profit"]
            direction = open_trade["direction"]
            low = candle["low"]
            high = candle["high"]
            close = candle["close"]

            result = None
            pnl = 0

            entry_bar = open_trade["entry_bar"]
            bars_alive = i - entry_bar
            trade_trend = open_trade.get("trend", "NEUTRAL")
            # Time-Stop: Trade-spezifisch > Pair-Default
            time_stop_bars = open_trade.get("time_stop",
                              pair_cfg.get("time_stop_bullish", 48) if trade_trend == "BULLISH"
                              else pair_cfg.get("time_stop_bearish", 24))
            if pair_tf == "HOUR_4":
                time_stop_bars = (time_stop_bars // 4) if time_stop_bars else time_stop_bars

            if bars_alive >= time_stop_bars:
                if direction == "LONG":
                    pnl = (close - entry) / entry * 100
                else:
                    pnl = (entry - close) / entry * 100
                result = "TIME_STOP"
                # Hinweis: Time-Stop schliesst den Trade immer nach Ablauf der Frist,
                # unabhaengig davon ob er gerade im Plus oder Minus ist.
                # Das verhindert, dass Gewinntrades unbegrenzt offen bleiben.

            if not result:
                if direction == "LONG":
                    if low <= sl:
                        result = "LOSS"
                        pnl = (sl - entry) / entry * 100
                    elif high >= tp:
                        result = "WIN"
                        pnl = (tp - entry) / entry * 100
                else:
                    if high >= sl:
                        result = "LOSS"
                        pnl = (entry - sl) / entry * 100
                    elif low <= tp:
                        result = "WIN"
                        pnl = (entry - tp) / entry * 100

            if result:
                open_trade["status"] = "CLOSED"
                open_trade["result"] = result
                open_trade["exit_price"] = close
                open_trade["pnl_pct"] = round(pnl, 4)
                open_trade["close_time"] = candle["time"]
                open_trade["bars_alive"] = bars_alive
                trades.append(open_trade)
                open_trade = None
                cooldown_bars = 4 if pair_tf != "HOUR_4" else 1
                continue

        # Cooldown
        if cooldown_bars > 0:
            cooldown_bars -= bar_step
            continue

        # Neues Setup
        analysis, err = analyze_pair_backtest_fast(ohlcv, pre, i, symbol, strat_map)
        if err or not analysis:
            continue

        trade = build_trade(analysis, pair_cfg, strat_map)
        if not trade:
            continue

        trade["entry_bar"] = i
        open_trade = trade

    # Offenen Trade am Ende schließen
    if open_trade:
        last = ohlcv[-1]
        entry = open_trade["entry"]
        direction = open_trade["direction"]
        close = last["close"]
        if direction == "LONG":
            pnl = (close - entry) / entry * 100
        else:
            pnl = (entry - close) / entry * 100
        open_trade["status"] = "CLOSED"
        open_trade["result"] = "OPEN_END"
        open_trade["exit_price"] = close
        open_trade["pnl_pct"] = round(pnl, 4)
        open_trade["close_time"] = last["time"]
        open_trade["bars_alive"] = len(ohlcv) - open_trade["entry_bar"]
        trades.append(open_trade)

    return trades

# ---------------------------------------------------------------------------
# STATISTIK
# ---------------------------------------------------------------------------
def calc_stats(trades):
    closed = [t for t in trades if t.get("result")]
    if not closed:
        return {}
    wins = [t for t in closed if t["result"] == "WIN"]
    losses = [t for t in closed if t["result"] == "LOSS"]
    time_stops = [t for t in closed if t["result"] == "TIME_STOP"]
    others = [t for t in closed if t["result"] not in ("WIN", "LOSS", "TIME_STOP")]

    total = len(wins) + len(losses) + len(time_stops) + len(others)
    gross_profit = sum(t["pnl_pct"] for t in closed if t["pnl_pct"] > 0)
    gross_loss = sum(abs(t["pnl_pct"]) for t in closed if t["pnl_pct"] < 0)
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0)
    avg_win = round(sum(t["pnl_pct"] for t in wins) / len(wins), 4) if wins else 0
    avg_loss = round(sum(t["pnl_pct"] for t in losses) / len(losses), 4) if losses else 0
    total_pnl = round(sum(t["pnl_pct"] for t in closed), 4)

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "time_stops": len(time_stops),
        "other": len(others),
        "win_rate": round(len(wins) / total * 100, 1) if total else 0,
        "profit_factor": pf,
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "total_pnl_pct": total_pnl
    }

# ---------------------------------------------------------------------------
# HAUPTPROGRAMM
# ---------------------------------------------------------------------------
def main():
    days = 360
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000

    print(f"=== Hermes Backtest | Letzte {days} Tage ===")
    print(f"Paare: {PAIRS}")
    print(f"Zeitraum: {datetime.fromtimestamp(start_ms/1000, tz=timezone.utc).isoformat()} -> {datetime.fromtimestamp(end_ms/1000, tz=timezone.utc).isoformat()}")
    print()

    all_trades = []
    pair_results = {}

    for symbol in PAIRS:
        print(f"Lade Daten für {symbol}...")
        h1_candles = fetch_candles(symbol, "HOUR_1", start_ms, end_ms)
        ohlcv = to_ohlcv(h1_candles)
        print(f"  H1 Kerzen geladen: {len(ohlcv)}")

        h4_candles = fetch_candles(symbol, "HOUR_4", start_ms, end_ms)
        htf_ohlcv = to_ohlcv(h4_candles)
        print(f"  H4 Kerzen geladen: {len(htf_ohlcv)}")

        if len(ohlcv) < 100:
            print(f"  SKIP: Zu wenig H1-Daten für {symbol}")
            continue

        trades = run_pair_backtest(symbol, ohlcv, htf_ohlcv, STRATEGY_MAP, CONFIG)
        stats = calc_stats(trades)
        pair_results[symbol] = stats
        all_trades.extend(trades)

        print(f"  Trades: {stats.get('total_trades', 0)} | PF: {stats.get('profit_factor', 0)} | WR: {stats.get('win_rate', 0)}% | PnL: {stats.get('total_pnl_pct', 0)}%")
        print()

    total_stats = calc_stats(all_trades)
    print("=== GESAMTERGEBNIS ===")
    print(f"Trades gesamt: {total_stats.get('total_trades', 0)}")
    print(f"Wins: {total_stats.get('wins', 0)} | Losses: {total_stats.get('losses', 0)} | Time-Stops: {total_stats.get('time_stops', 0)}")
    print(f"Win Rate: {total_stats.get('win_rate', 0)}%")
    print(f"Profit Factor: {total_stats.get('profit_factor', 0)}")
    print(f"Gross Profit: {total_stats.get('gross_profit', 0)}%")
    print(f"Gross Loss: {total_stats.get('gross_loss', 0)}%")
    print(f"Gesamt-PnL: {total_stats.get('total_pnl_pct', 0)}%")
    print()

    for sym, st in pair_results.items():
        print(f"{sym}: Trades={st.get('total_trades',0)} PF={st.get('profit_factor',0)} WR={st.get('win_rate',0)}% PnL={st.get('total_pnl_pct',0)}%")

    result_file = os.path.join(RESULT_DIR, f"backtest_360d_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json")
    with open(result_file, "w") as f:
        json.dump({
            "pairs": pair_results,
            "total": total_stats,
            "trades": all_trades
        }, f, indent=2, default=str)
    print(f"\nErgebnis gespeichert: {result_file}")

if __name__ == "__main__":
    main()
