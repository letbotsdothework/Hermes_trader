#!/usr/bin/env python3
"""
ETH-spezifische Strategie-Optimierung
Testet alle Long- und Short-Strategien isoliert über 360 Tage.
"""

import sys, json, os
from datetime import timezone, datetime

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from backtest_360d import (
    fetch_candles, to_ohlcv, precompute_all,
    analyze_pair_backtest_fast, build_trade, calc_stats,
    RESULT_DIR
)

SYMBOL = "ETH_USDT"
DAYS = 360

ALL_STRATEGIES = [
    ("TREND_FOLLOW_LONG", "LONG"),
    ("MEAN_REVERSION_LONG", "LONG"),
    ("EMA_BOUNCE_LONG", "LONG"),
    ("VWAP_RETAIL_LONG", "LONG"),
    ("RSI_DIVERGENCE_LONG", "LONG"),
    ("WILLR_LONG", "LONG"),
    ("BB_SQUEEZE_LONG", "LONG"),
    ("BB_BOUNCE_LONG", "LONG"),
    ("EMA50_BOUNCE_LONG", "LONG"),
    ("HIGHER_LOW_BREAK", "LONG"),
    ("LOWER_HIGH_BREAK", "SHORT"),
    ("TREND_FOLLOW_SHORT", "SHORT"),
    ("MEAN_REVERSION_SHORT", "SHORT"),
    ("EMA_BOUNCE_SHORT", "SHORT"),
    ("VWAP_RETAIL_SHORT", "SHORT"),
    ("RSI_DIVERGENCE_SHORT", "SHORT"),
    ("EMA_COMPRESSION_BREAK", "SHORT"),
    ("STOCH_MR_SHORT", "SHORT"),
    ("WILLR_SHORT", "SHORT"),
    ("RANGE_BREAKOUT_SHORT", "SHORT"),
]

def run_isolated(symbol, ohlcv, htf_ohlcv, strat_name, direction):
    pair_prefs = {
        "timeframe": "HOUR_1",
        "avoid": [s for s, d in ALL_STRATEGIES if s != strat_name],
        "min_confidence": 55,
        "ema_dist_min": 0.0,
        "volume_mult": 1.0,
        "sl_atr_mult": 1.5,
        "tp_atr_mult": 3.0 if direction == "LONG" else 2.5,
        "time_stop_bullish": 96,
        "time_stop_bearish": 48,
    }
    strat_map = {symbol: pair_prefs}
    cfg = {
        "risk_per_trade_pct": 1.0,
        "sl_atr_mult": 1.5,
        "tp_atr_mult": 3.0 if direction == "LONG" else 2.5,
        "min_confidence": 55,
        "time_stop_bullish": 96,
        "time_stop_bearish": 48,
    }

    trades = []
    open_trade = None
    cooldown_bars = 0
    pre = precompute_all(ohlcv, htf_ohlcv, symbol, strat_map)
    n = len(ohlcv)

    for i in range(55, n):
        candle = ohlcv[i]
        if open_trade:
            entry = open_trade["entry"]
            sl = open_trade["stop_loss"]
            tp = open_trade["take_profit"]
            direction = open_trade["direction"]
            low, high, close = candle["low"], candle["high"], candle["close"]
            result = None; pnl = 0
            bars_alive = i - open_trade["entry_bar"]
            ts = 96 if direction == "LONG" else 48

            if bars_alive >= ts:
                unrealized = (close - entry)/entry*100 if direction == "LONG" else (entry - close)/entry*100
                if unrealized < 0:
                    pnl = unrealized; result = "TIME_STOP"
            if not result:
                if direction == "LONG":
                    if low <= sl: result, pnl = "LOSS", (sl - entry)/entry*100
                    elif high >= tp: result, pnl = "WIN", (tp - entry)/entry*100
                else:
                    if high >= sl: result, pnl = "LOSS", (entry - sl)/entry*100
                    elif low <= tp: result, pnl = "WIN", (entry - tp)/entry*100
            if result:
                open_trade.update({"status": "CLOSED", "result": result,
                                   "exit_price": close, "pnl_pct": round(pnl, 4),
                                   "close_time": candle["time"], "bars_alive": bars_alive})
                trades.append(open_trade); open_trade = None; cooldown_bars = 4; continue

        if cooldown_bars > 0:
            cooldown_bars -= 1; continue

        analysis, err = analyze_pair_backtest_fast(ohlcv, pre, i, symbol, strat_map)
        if err or not analysis or analysis.get("direction") != direction: continue
        trade = build_trade(analysis, cfg, strat_map)
        if not trade: continue
        trade["entry_bar"] = i; open_trade = trade

    if open_trade:
        last = ohlcv[-1]
        pnl = (open_trade["entry"] - last["close"])/open_trade["entry"]*100 if open_trade["direction"] == "SHORT" else (last["close"] - open_trade["entry"])/open_trade["entry"]*100
        open_trade.update({"status": "CLOSED", "result": "OPEN_END",
                           "exit_price": last["close"], "pnl_pct": round(pnl, 4),
                           "close_time": last["time"],
                           "bars_alive": len(ohlcv) - open_trade["entry_bar"]})
        trades.append(open_trade)

    return trades

def main():
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - DAYS * 24 * 3600 * 1000

    print(f"=== ETH Strategie-Test | {DAYS} Tage ===")
    h1 = fetch_candles(SYMBOL, "HOUR_1", start_ms, end_ms)
    ohlcv = to_ohlcv(h1)
    h4 = fetch_candles(SYMBOL, "HOUR_4", start_ms, end_ms)
    htf = to_ohlcv(h4)
    print(f"H1: {len(ohlcv)} | H4: {len(htf)}\n")

    results = []
    for strat_name, direction in ALL_STRATEGIES:
        trades = run_isolated(SYMBOL, ohlcv, htf, strat_name, direction)
        stats = calc_stats(trades)
        pf = stats.get("profit_factor", 0)
        wr = stats.get("win_rate", 0)
        pnl = stats.get("total_pnl_pct", 0)
        total = stats.get("total_trades", 0)
        print(f"{strat_name:30s} ({direction:5s}) | Trades: {total:3d} | PF: {pf:5.2f} | WR: {wr:5.1f}% | PnL: {pnl:+7.2f}%")
        results.append({"strategy": strat_name, "direction": direction, "trades": total, "pf": pf, "wr": wr, "pnl": pnl})

    print("\n=== BESTE ETH STRATEGIEN ===")
    longs = [r for r in results if r["direction"] == "LONG" and r["trades"] >= 5 and r["pf"] >= 1.0]
    shorts = [r for r in results if r["direction"] == "SHORT" and r["trades"] >= 5 and r["pf"] >= 1.0]
    longs.sort(key=lambda x: (-x["pf"], -x["pnl"]))
    shorts.sort(key=lambda x: (-x["pf"], -x["pnl"]))

    print("LONGS:")
    for r in longs[:5]:
        print(f"  {r['strategy']}: PF={r['pf']:.2f} WR={r['wr']:.1f}% PnL={r['pnl']:+.2f}% Trades={r['trades']}")
    if not longs: print("  Keine profitable Long-Strategie")

    print("SHORTS:")
    for r in shorts[:5]:
        print(f"  {r['strategy']}: PF={r['pf']:.2f} WR={r['wr']:.1f}% PnL={r['pnl']:+.2f}% Trades={r['trades']}")
    if not shorts: print("  Keine profitable Short-Strategie")

if __name__ == "__main__":
    main()
