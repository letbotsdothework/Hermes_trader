#!/usr/bin/env python3
"""
BTC TREND_FOLLOW_SHORT Parameter-Optimierung
Grid-Search über SL/TP-Multiplikatoren und Time-Stop.
"""

import sys, json, os
from datetime import timezone, datetime

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from backtest_360d import (
    fetch_candles, to_ohlcv, precompute_all,
    analyze_pair_backtest_fast, build_trade, calc_stats,
    RESULT_DIR
)

SYMBOL = "BTC_USDT"
DAYS = 360

# Grid
SL_MULTS = [1.0, 1.5, 2.0, 2.5]
TP_MULTS = [2.0, 3.0, 4.0, 5.0]
TIME_STOPS = [24, 48, 72, 96]

def run_btc_short_backtest(ohlcv, htf_ohlcv, sl_mult, tp_mult, time_stop):
    pair_prefs = {
        "timeframe": "HOUR_1",
        "avoid": [
            "TREND_FOLLOW_LONG", "MEAN_REVERSION_LONG", "MEAN_REVERSION_SHORT",
            "EMA_BOUNCE_LONG", "EMA_BOUNCE_SHORT", "VWAP_RETAIL_LONG", "VWAP_RETAIL_SHORT",
            "RSI_DIVERGENCE_LONG", "RSI_DIVERGENCE_SHORT", "LOWER_HIGH_BREAK",
            "EMA_COMPRESSION_BREAK", "STOCH_MR_LONG", "STOCH_MR_SHORT",
            "WILLR_LONG", "WILLR_SHORT", "BB_SQUEEZE_LONG", "BB_SQUEEZE_SHORT",
            "EMA50_BOUNCE_LONG", "HIGHER_LOW_BREAK", "BB_BOUNCE_LONG"
        ],
        "min_confidence": 55,
        "ema_dist_min": 0.0,
        "volume_mult": 1.0,
        "sl_atr_mult": sl_mult,
        "tp_atr_mult": tp_mult,
        "time_stop_bearish": time_stop,
    }
    strat_map = {SYMBOL: pair_prefs}
    cfg = {
        "risk_per_trade_pct": 1.0,
        "sl_atr_mult": sl_mult,
        "tp_atr_mult": tp_mult,
        "min_confidence": 55,
        "time_stop_bearish": time_stop,
    }

    trades = []
    open_trade = None
    cooldown_bars = 0
    bar_step = 1
    start_idx = 55

    pre = precompute_all(ohlcv, htf_ohlcv, SYMBOL, strat_map)
    n = len(ohlcv)

    for i in range(start_idx, n, bar_step):
        candle = ohlcv[i]

        if open_trade:
            entry = open_trade["entry"]
            sl = open_trade["stop_loss"]
            tp = open_trade["take_profit"]
            low = candle["low"]
            high = candle["high"]
            close = candle["close"]

            result = None
            pnl = 0
            bars_alive = i - open_trade["entry_bar"]

            if bars_alive >= time_stop:
                unrealized = (entry - close) / entry * 100
                if unrealized < 0:
                    pnl = unrealized
                    result = "TIME_STOP"

            if not result:
                if high >= sl:
                    result = "LOSS"
                    pnl = (entry - sl) / entry * 100
                elif low <= tp:
                    result = "WIN"
                    pnl = (entry - tp) / entry * 100

            if result:
                open_trade.update({"status": "CLOSED", "result": result,
                                   "exit_price": close, "pnl_pct": round(pnl, 4),
                                   "close_time": candle["time"], "bars_alive": bars_alive})
                trades.append(open_trade)
                open_trade = None
                cooldown_bars = 4
                continue

        if cooldown_bars > 0:
            cooldown_bars -= bar_step
            continue

        analysis, err = analyze_pair_backtest_fast(ohlcv, pre, i, SYMBOL, strat_map)
        if err or not analysis or analysis.get("direction") != "SHORT":
            continue

        trade = build_trade(analysis, cfg)
        if not trade:
            continue
        trade["entry_bar"] = i
        open_trade = trade

    if open_trade:
        last = ohlcv[-1]
        pnl = (open_trade["entry"] - last["close"]) / open_trade["entry"] * 100
        open_trade.update({"status": "CLOSED", "result": "OPEN_END",
                           "exit_price": last["close"], "pnl_pct": round(pnl, 4),
                           "close_time": last["time"],
                           "bars_alive": len(ohlcv) - open_trade["entry_bar"]})
        trades.append(open_trade)

    return trades

def main():
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - DAYS * 24 * 3600 * 1000

    print(f"=== BTC TREND_FOLLOW_SHORT Optimierung | {DAYS} Tage ===")
    print(f"SL: {SL_MULTS} | TP: {TP_MULTS} | TS: {TIME_STOPS}")
    print()

    print("Lade Daten...")
    h1 = fetch_candles(SYMBOL, "HOUR_1", start_ms, end_ms)
    ohlcv = to_ohlcv(h1)
    h4 = fetch_candles(SYMBOL, "HOUR_4", start_ms, end_ms)
    htf = to_ohlcv(h4)
    print(f"H1: {len(ohlcv)} | H4: {len(htf)}\n")

    best = None
    results = []

    for sl in SL_MULTS:
        for tp in TP_MULTS:
            for ts in TIME_STOPS:
                trades = run_btc_short_backtest(ohlcv, htf, sl, tp, ts)
                stats = calc_stats(trades)
                pf = stats.get("profit_factor", 0)
                wr = stats.get("win_rate", 0)
                pnl = stats.get("total_pnl_pct", 0)
                total = stats.get("total_trades", 0)

                print(f"SL={sl:.1f} TP={tp:.1f} TS={ts:2d} | Trades={total:3d} PF={pf:5.2f} WR={wr:5.1f}% PnL={pnl:+7.2f}%")

                results.append({"sl": sl, "tp": tp, "ts": ts, "pf": pf, "wr": wr, "pnl": pnl, "trades": total})

                if total >= 10:
                    if best is None or pf > best["pf"] or (pf == best["pf"] and pnl > best["pnl"]):
                        best = {"sl": sl, "tp": tp, "ts": ts, "pf": pf, "wr": wr, "pnl": pnl, "trades": total}

    print("\n=== BESTE KONFIGURATION ===")
    if best:
        print(f"SL={best['sl']} TP={best['tp']} TS={best['ts']} | PF={best['pf']} WR={best['wr']}% PnL={best['pnl']:+.2f}% Trades={best['trades']}")
    else:
        print("Keine Konfiguration mit >=10 Trades und PF>0 gefunden.")

    result_file = os.path.join(RESULT_DIR, f"btc_short_opt_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(RESULT_DIR, exist_ok=True)
    with open(result_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nErgebnis: {result_file}")

if __name__ == "__main__":
    main()
