#!/usr/bin/env python3
"""
Short-Strategie Grid-Search für BTC, NEAR, DOGE
Testet jede Short-Strategie isoliert über 360 Tage.
"""

import sys, json, os, math, time
from datetime import timezone, datetime

# Backtest-Engine importieren
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from backtest_360d import (
    fetch_candles, to_ohlcv, precompute_all,
    analyze_pair_backtest_fast, build_trade, calc_stats,
    BASE_URL, CACHE_DIR, RESULT_DIR
)

PAIRS = ["BTC_USDT", "NEAR_USDT", "DOGE_USDT"]
DAYS = 360

SHORT_STRATEGIES = [
    "TREND_FOLLOW_SHORT",
    "MEAN_REVERSION_SHORT",
    "EMA_BOUNCE_SHORT",
    "VWAP_RETAIL_SHORT",
    "RSI_DIVERGENCE_SHORT",
    "LOWER_HIGH_BREAK",
    "EMA_COMPRESSION_BREAK",
    "STOCH_MR_SHORT",
    "WILLR_SHORT",
]

ALL_LONG_STRATEGIES = [
    "TREND_FOLLOW_LONG", "MEAN_REVERSION_LONG", "EMA_BOUNCE_LONG",
    "VWAP_RETAIL_LONG", "RSI_DIVERGENCE_LONG", "WILLR_LONG",
    "BB_SQUEEZE_LONG", "BB_BOUNCE_LONG", "EMA50_BOUNCE_LONG",
    "HIGHER_LOW_BREAK"
]

def run_short_only_backtest(symbol, ohlcv, htf_ohlcv, allowed_short, base_cfg):
    """Führt Backtest aus, erlaubt NUR eine einzelne Short-Strategie."""
    pair_prefs = {
        "timeframe": "HOUR_1",
        "avoid": [s for s in SHORT_STRATEGIES if s != allowed_short] + ALL_LONG_STRATEGIES,
        "min_confidence": 55,
        "ema_dist_min": 0.0,
        "volume_mult": 1.0,
        "sl_atr_mult": base_cfg.get("sl_atr_mult", 1.5),
        "tp_atr_mult": base_cfg.get("tp_atr_mult", 3.0),
        "time_stop_bearish": base_cfg.get("time_stop_bearish", 48),
    }
    strat_map = {symbol: pair_prefs}

    trades = []
    open_trade = None
    cooldown_bars = 0
    bar_step = 1
    start_idx = 55

    cfg = dict(base_cfg)
    cfg["sl_atr_mult"] = pair_prefs["sl_atr_mult"]
    cfg["tp_atr_mult"] = pair_prefs["tp_atr_mult"]
    cfg["time_stop_bearish"] = pair_prefs["time_stop_bearish"]
    cfg["min_confidence"] = pair_prefs["min_confidence"]

    pre = precompute_all(ohlcv, htf_ohlcv, symbol, strat_map)
    n = len(ohlcv)

    for i in range(start_idx, n, bar_step):
        candle = ohlcv[i]

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
            ts_bear = cfg.get("time_stop_bearish", 48)

            if bars_alive >= ts_bear:
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
                open_trade["status"] = "CLOSED"
                open_trade["result"] = result
                open_trade["exit_price"] = close
                open_trade["pnl_pct"] = round(pnl, 4)
                open_trade["close_time"] = candle["time"]
                open_trade["bars_alive"] = bars_alive
                trades.append(open_trade)
                open_trade = None
                cooldown_bars = 4
                continue

        if cooldown_bars > 0:
            cooldown_bars -= bar_step
            continue

        analysis, err = analyze_pair_backtest_fast(ohlcv, pre, i, symbol, strat_map)
        if err or not analysis:
            continue
        if analysis.get("direction") != "SHORT":
            continue

        trade = build_trade(analysis, cfg)
        if not trade:
            continue

        trade["entry_bar"] = i
        open_trade = trade

    if open_trade:
        last = ohlcv[-1]
        entry = open_trade["entry"]
        close = last["close"]
        pnl = (entry - close) / entry * 100
        open_trade["status"] = "CLOSED"
        open_trade["result"] = "OPEN_END"
        open_trade["exit_price"] = close
        open_trade["pnl_pct"] = round(pnl, 4)
        open_trade["close_time"] = last["time"]
        open_trade["bars_alive"] = len(ohlcv) - open_trade["entry_bar"]
        trades.append(open_trade)

    return trades

def main():
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - DAYS * 24 * 3600 * 1000

    base_cfg = {
        "risk_per_trade_pct": 1.0,
        "sl_atr_mult": 1.5,
        "tp_atr_mult": 2.5,
        "min_confidence": 55,
        "time_stop_bearish": 48,
    }

    print(f"=== Short Grid-Search | {DAYS} Tage ===")
    print(f"Paare: {PAIRS}")
    print(f"Strategien: {len(SHORT_STRATEGIES)}")
    print()

    all_results = []

    for symbol in PAIRS:
        print(f"Lade Daten für {symbol}...")
        h1_candles = fetch_candles(symbol, "HOUR_1", start_ms, end_ms)
        ohlcv = to_ohlcv(h1_candles)
        h4_candles = fetch_candles(symbol, "HOUR_4", start_ms, end_ms)
        htf_ohlcv = to_ohlcv(h4_candles)
        print(f"  H1: {len(ohlcv)} | H4: {len(htf_ohlcv)}")

        if len(ohlcv) < 100:
            print(f"  SKIP: Zu wenig Daten")
            continue

        pair_best = None

        for strat in SHORT_STRATEGIES:
            trades = run_short_only_backtest(symbol, ohlcv, htf_ohlcv, strat, base_cfg)
            stats = calc_stats(trades)

            pf = stats.get("profit_factor", 0)
            wr = stats.get("win_rate", 0)
            pnl = stats.get("total_pnl_pct", 0)
            total = stats.get("total_trades", 0)

            print(f"  {strat:30s} | Trades: {total:3d} | PF: {pf:5.2f} | WR: {wr:5.1f}% | PnL: {pnl:+.2f}%")

            all_results.append({
                "symbol": symbol,
                "strategy": strat,
                "trades": total,
                "pf": pf,
                "wr": wr,
                "pnl": pnl,
                "stats": stats,
            })

            if total >= 5 and pf >= 1.0:
                if pair_best is None or pf > pair_best["pf"] or (pf == pair_best["pf"] and pnl > pair_best["pnl"]):
                    pair_best = {
                        "strategy": strat,
                        "pf": pf,
                        "wr": wr,
                        "pnl": pnl,
                        "trades": total,
                    }

        if pair_best:
            print(f"  >>> BESTE Short-Strategie für {symbol}: {pair_best['strategy']} (PF {pair_best['pf']}, WR {pair_best['wr']}%, PnL {pair_best['pnl']:+.2f}%, {pair_best['trades']} Trades)")
        else:
            print(f"  >>> KEINE profitable Short-Strategie für {symbol} gefunden (PF>=1.0, min. 5 Trades)")
        print()

    # Zusammenfassung
    print("=== ZUSAMMENFASSUNG ===")
    for symbol in PAIRS:
        symbol_results = [r for r in all_results if r["symbol"] == symbol and r["trades"] >= 5 and r["pf"] >= 1.0]
        if symbol_results:
            symbol_results.sort(key=lambda x: (-x["pf"], -x["pnl"]))
            best = symbol_results[0]
            print(f"{symbol}: {best['strategy']} | PF={best['pf']} | WR={best['wr']}% | PnL={best['pnl']:+.2f}% | Trades={best['trades']}")
        else:
            print(f"{symbol}: Keine profitable Short-Strategie")

    result_file = os.path.join(RESULT_DIR, f"short_grid_search_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(RESULT_DIR, exist_ok=True)
    with open(result_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nErgebnis gespeichert: {result_file}")

if __name__ == "__main__":
    main()
