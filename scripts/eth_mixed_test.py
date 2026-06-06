#!/usr/bin/env python3
"""ETH Mixed-Backtest mit BB_SQUEEZE_LONG + TREND_FOLLOW_SHORT"""
import sys, json, os
from datetime import timezone, datetime
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from backtest_360d import fetch_candles, to_ohlcv, precompute_all, analyze_pair_backtest_fast, build_trade, calc_stats, run_pair_backtest

SYMBOL = "ETH_USDT"
DAYS = 360

def main():
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - DAYS * 24 * 3600 * 1000
    h1 = fetch_candles(SYMBOL, "HOUR_1", start_ms, end_ms)
    ohlcv = to_ohlcv(h1)
    h4 = fetch_candles(SYMBOL, "HOUR_4", start_ms, end_ms)
    htf = to_ohlcv(h4)

    # Test verschiedene Konfigurationen
    configs = [
        {"name": "SL1.5_TP3.0", "sl": 1.5, "tp": 3.0, "ts_long": 96, "ts_short": 48},
        {"name": "SL1.5_TP2.5", "sl": 1.5, "tp": 2.5, "ts_long": 96, "ts_short": 48},
        {"name": "SL2.0_TP3.0", "sl": 2.0, "tp": 3.0, "ts_long": 96, "ts_short": 48},
        {"name": "SL1.5_TP4.0", "sl": 1.5, "tp": 4.0, "ts_long": 96, "ts_short": 48},
    ]

    print(f"=== ETH Mixed Test | {DAYS} Tage ===\n")
    for cfg_params in configs:
        strat_map = {
            SYMBOL: {
                "timeframe": "HOUR_1",
                "avoid": ["TREND_FOLLOW_LONG", "MEAN_REVERSION_LONG", "MEAN_REVERSION_SHORT",
                          "EMA_BOUNCE_LONG", "EMA_BOUNCE_SHORT", "VWAP_RETAIL_LONG", "VWAP_RETAIL_SHORT",
                          "RSI_DIVERGENCE_LONG", "RSI_DIVERGENCE_SHORT", "WILLR_LONG", "WILLR_SHORT",
                          "HIGHER_LOW_BREAK", "LOWER_HIGH_BREAK", "EMA_COMPRESSION_BREAK",
                          "STOCH_MR_SHORT", "BB_BOUNCE_LONG", "EMA50_BOUNCE_LONG", "RANGE_BREAKOUT_SHORT"],
                "min_confidence": 55,
                "ema_dist_min": 0.0,
                "volume_mult": 1.0,
                "sl_atr_mult": cfg_params["sl"],
                "tp_atr_mult": cfg_params["tp"],
                "time_stop_bullish": cfg_params["ts_long"],
                "time_stop_bearish": cfg_params["ts_short"],
            }
        }
        base_cfg = {
            "risk_per_trade_pct": 1.0,
            "sl_atr_mult": cfg_params["sl"],
            "tp_atr_mult": cfg_params["tp"],
            "time_stop_bullish": cfg_params["ts_long"],
            "time_stop_bearish": cfg_params["ts_short"],
            "min_confidence": 55,
        }
        trades = run_pair_backtest(SYMBOL, ohlcv, htf, strat_map, base_cfg)
        stats = calc_stats(trades)
        print(f"{cfg_params['name']:15s} | Trades: {stats.get('total_trades',0):3d} | PF: {stats.get('profit_factor',0):5.2f} | WR: {stats.get('win_rate',0):5.1f}% | PnL: {stats.get('total_pnl_pct',0):+7.2f}%")

if __name__ == "__main__":
    main()
