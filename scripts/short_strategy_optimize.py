#!/usr/bin/env python3
"""
Pair-Strategie-spezifische Short-Parameter-Optimierung
Grid-Search über SL/TP/Time-Stop für jede Short-Strategie jedes Pairs
"""

import sys, json, os, copy
from datetime import timezone, datetime

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from backtest_360d import (
    fetch_candles, to_ohlcv, precompute_all,
    analyze_pair_backtest_fast, build_trade, calc_stats,
    RESULT_DIR
)

DAYS = 360
PAIRS_SHORT_MAP = {
    "BTC_USDT": ["TREND_FOLLOW_SHORT", "EMA_BOUNCE_SHORT", "VWAP_RETAIL_SHORT", "STOCH_MR_SHORT", "WILLR_SHORT"],
    "ETH_USDT": ["RANGE_BREAKOUT_SHORT"],
    "NEAR_USDT": ["EMA_COMPRESSION_BREAK", "STOCH_MR_SHORT"],
    "DOGE_USDT": ["TREND_FOLLOW_SHORT", "EMA_COMPRESSION_BREAK", "STOCH_MR_SHORT", "VWAP_RETAIL_SHORT"],
}

ALL_STRATEGIES = [
    "TREND_FOLLOW_LONG", "MEAN_REVERSION_LONG", "EMA_BOUNCE_LONG",
    "VWAP_RETAIL_LONG", "RSI_DIVERGENCE_LONG", "WILLR_LONG",
    "BB_SQUEEZE_LONG", "BB_BOUNCE_LONG", "EMA50_BOUNCE_LONG",
    "HIGHER_LOW_BREAK", "LOWER_HIGH_BREAK",
    "TREND_FOLLOW_SHORT", "MEAN_REVERSION_SHORT", "EMA_BOUNCE_SHORT",
    "VWAP_RETAIL_SHORT", "RSI_DIVERGENCE_SHORT",
    "EMA_COMPRESSION_BREAK", "STOCH_MR_SHORT", "WILLR_SHORT",
    "RANGE_BREAKOUT_SHORT"
]

# Grid
SL_MULTS = [1.0, 1.5, 2.0, 2.5, 3.0]
TP_MULTS = [2.0, 2.5, 3.0, 3.5, 4.0, 5.0]
TIME_STOPS = [24, 48, 72, 96]

def make_strat_map(symbol, allowed_short, sl, tp, ts):
    """Erzeugt ein strat_map, das NUR die erlaubte Short-Strategie + Longs zulässt."""
    other_shorts = [s for s in ALL_STRATEGIES if s.endswith("_SHORT") and s != allowed_short]
    avoid = other_shorts + ["MEAN_REVERSION_LONG", "RSI_DIVERGENCE_LONG", "RSI_DIVERGENCE_SHORT",
                            "STOCH_MR_LONG", "WILLR_LONG", "WILLR_SHORT", "BB_SQUEEZE_LONG"]

    return {
        symbol: {
            "timeframe": "HOUR_1",
            "primary": "",
            "secondary": "",
            "avoid": avoid,
            "min_confidence": 55,
            "ema_dist_min": 0.0,
            "volume_mult": 1.0,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 3.0,
            "time_stop_bullish": 96,
            "time_stop_bearish": 48,
            "strategy_params": {
                allowed_short: {
                    "direction": "SHORT",
                    "sl_atr_mult": sl,
                    "tp_atr_mult": tp,
                    "time_stop": ts,
                    "min_confidence": 55,
                }
            }
        }
    }

def run_isolated_backtest(symbol, ohlcv, htf_ohlcv, strat_map):
    cfg = {
        "risk_per_trade_pct": 1.0,
        "sl_atr_mult": 1.5,
        "tp_atr_mult": 3.0,
        "min_confidence": 55,
        "time_stop_bearish": 48,
    }
    trades = []
    open_trade = None
    cooldown_bars = 0
    bar_step = 1
    start_idx = 55

    pre = precompute_all(ohlcv, htf_ohlcv, symbol, strat_map)
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
            ts = open_trade.get("time_stop", 48)

            if bars_alive >= ts:
                # Time-Stop schliesst den Trade immer nach Ablauf der Frist
                pnl = (entry - close) / entry * 100
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

        analysis, err = analyze_pair_backtest_fast(ohlcv, pre, i, symbol, strat_map)
        if err or not analysis or analysis.get("direction") != "SHORT":
            continue

        trade = build_trade(analysis, cfg, strat_map)
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

def optimize_pair_strategy(symbol, ohlcv, htf_ohlcv, strategy):
    print(f"    Optimiere {strategy}...")
    best = None
    results = []

    for sl in SL_MULTS:
        for tp in TP_MULTS:
            for ts in TIME_STOPS:
                strat_map = make_strat_map(symbol, strategy, sl, tp, ts)
                trades = run_isolated_backtest(symbol, ohlcv, htf_ohlcv, strat_map)
                stats = calc_stats(trades)
                pf = stats.get("profit_factor", 0)
                wr = stats.get("win_rate", 0)
                pnl = stats.get("total_pnl_pct", 0)
                total = stats.get("total_trades", 0)

                results.append({"sl": sl, "tp": tp, "ts": ts, "pf": pf, "wr": wr, "pnl": pnl, "trades": total})

                if total >= 5:
                    if best is None or pf > best["pf"] or (abs(pf - best["pf"]) < 0.01 and pnl > best["pnl"]):
                        best = {"sl": sl, "tp": tp, "ts": ts, "pf": pf, "wr": wr, "pnl": pnl, "trades": total}

    return best, results

def main():
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - DAYS * 24 * 3600 * 1000

    print(f"=== Pair-Strategie Short-Optimierung | {DAYS} Tage ===")
    print(f"SL: {SL_MULTS} | TP: {TP_MULTS} | TS: {TIME_STOPS}")
    print(f"Kombinationen pro Strategie: {len(SL_MULTS)*len(TP_MULTS)*len(TIME_STOPS)}")
    print()

    all_best = []
    all_raw = []

    for symbol, strategies in PAIRS_SHORT_MAP.items():
        if not strategies:
            continue

        print(f"Lade Daten für {symbol}...")
        h1 = fetch_candles(symbol, "HOUR_1", start_ms, end_ms)
        ohlcv = to_ohlcv(h1)
        h4 = fetch_candles(symbol, "HOUR_4", start_ms, end_ms)
        htf = to_ohlcv(h4)
        print(f"  H1: {len(ohlcv)} | H4: {len(htf)}")

        if len(ohlcv) < 100:
            print(f"  SKIP: Zu wenig Daten\n")
            continue

        for strategy in strategies:
            best, raw = optimize_pair_strategy(symbol, ohlcv, htf, strategy)
            all_raw.extend([{**r, "symbol": symbol, "strategy": strategy} for r in raw])

            if best:
                print(f"    BESTE {strategy}: SL={best['sl']} TP={best['tp']} TS={best['ts']} | PF={best['pf']:.2f} WR={best['wr']:.1f}% PnL={best['pnl']:+.2f}% Trades={best['trades']}")
                all_best.append({
                    "symbol": symbol,
                    "strategy": strategy,
                    "sl_atr_mult": best["sl"],
                    "tp_atr_mult": best["tp"],
                    "time_stop": best["ts"],
                    "pf": best["pf"],
                    "wr": best["wr"],
                    "pnl": best["pnl"],
                    "trades": best["trades"],
                })
            else:
                print(f"    {strategy}: Keine profitable Konfiguration (min. 5 Trades)")
        print()

    print("=== ERGEBNIS ZUSAMMENFASSUNG ===")
    for sym in PAIRS_SHORT_MAP:
        sym_best = [b for b in all_best if b["symbol"] == sym]
        if sym_best:
            sym_best.sort(key=lambda x: -x["pf"])
            for b in sym_best:
                print(f"{sym} {b['strategy']}: SL={b['sl_atr_mult']} TP={b['tp_atr_mult']} TS={b['time_stop']} | PF={b['pf']:.2f} WR={b['wr']:.1f}% PnL={b['pnl']:+.2f}% Trades={b['trades']}")
        else:
            print(f"{sym}: Keine profitable Short-Strategie gefunden")

    result_file = os.path.join(RESULT_DIR, f"short_opt_full_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(RESULT_DIR, exist_ok=True)
    with open(result_file, "w") as f:
        json.dump({"best": all_best, "raw": all_raw}, f, indent=2, default=str)
    print(f"\nErgebnis gespeichert: {result_file}")

if __name__ == "__main__":
    main()
