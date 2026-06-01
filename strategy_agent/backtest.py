"""
strategy_agent/backtest.py — 歷史回測模組

對過去 N 個月的歷史資料逐日滑動視窗跑指標派策略，
結果寫入 signals DB（is_backtest=1），
使 get_strategy_winrate() 在上線初期就有足夠樣本可用。

使用：
  python -m strategy_agent.backtest
  python -m strategy_agent.backtest --months 18

注意：
  - 只回測「指標派」策略（籌碼派需要 TWSE 真實資料，不適合模擬）
  - 回測信號寫入時已預計算 5d/10d 報酬，不需 update_pending_results() 二次計算
  - UNIQUE(date, symbol, strategy) 確保冪等，重跑安全
"""
from __future__ import annotations

import os
import sys
import datetime
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from strategy_agent.signal_db    import init_db, add_backtest_signal
from strategy_agent.strategies   import ALL_STRATEGIES
from strategy_agent.etf_universe import get_universe
from strategy_agent.runner       import fetch_ohlcv, _NAME_CACHE

WIN_PCT  = 3.0   # 漲超過 3% → 算贏
LOSS_PCT = 3.0   # 跌超過 3% → 算輸
MIN_BARS = 32    # 策略最多需要 32 根 K 棒（pullback_ma20 要 32 根）
FWD_DAYS = 10    # 最遠觀察 10 個交易日


def _calc_result(entry: float, exit_price: float) -> tuple:
    """回傳 (報酬%, win) — win 為 1/0/None（中性不計）"""
    pct = (exit_price - entry) / entry * 100
    if pct >= WIN_PCT:
        win = 1
    elif pct <= -LOSS_PCT:
        win = 0
    else:
        win = None
    return round(pct, 2), win


def _backtest_one_stock(
    sym: str,
    etf_source: str,
    ohlcv: list[dict],
) -> tuple[int, int]:
    """
    對單一股票執行滑動視窗回測。
    回傳 (新增筆數, 跳過重複筆數)。
    """
    name   = _NAME_CACHE.get(sym, sym)
    added  = 0
    skipped = 0

    # 最後 FWD_DAYS 根不跑（沒有足夠未來資料可驗證 10d 結果）
    max_i = len(ohlcv) - FWD_DAYS - 1

    for i in range(MIN_BARS, max_i + 1):
        window      = ohlcv[:i + 1]   # 滑動視窗（到 i 日為止）
        bar         = window[-1]
        date_str    = bar["date"]
        entry_price = bar["close"]

        for strat_key, (_label, strat_fn) in ALL_STRATEGIES.items():
            try:
                triggered, _detail = strat_fn(window)
            except Exception:
                continue

            if not triggered:
                continue

            # 計算 5d / 10d 結果（從真實歷史資料）
            future = ohlcv[i + 1: i + 1 + FWD_DAYS]

            price_5d = result_5d = win_5d = None
            if len(future) >= 5:
                price_5d = future[4]["close"]
                result_5d, win_5d = _calc_result(entry_price, price_5d)

            price_10d = result_10d = win_10d = None
            if len(future) >= FWD_DAYS:
                price_10d = future[FWD_DAYS - 1]["close"]
                result_10d, win_10d = _calc_result(entry_price, price_10d)

            ok = add_backtest_signal(
                date        = date_str,
                symbol      = sym,
                name        = name,
                etf_source  = etf_source,
                strategy    = strat_key,
                entry_price = entry_price,
                price_5d    = price_5d,
                price_10d   = price_10d,
                result_5d   = result_5d,
                result_10d  = result_10d,
                win_5d      = win_5d,
                win_10d     = win_10d,
            )
            if ok:
                added += 1
            else:
                skipped += 1

    return added, skipped


def run_backtest(months: int = 18, max_workers: int = 4) -> dict:
    """
    對過去 months 個月執行完整歷史回測。

    Args:
        months:      回測期間（建議 12~24，預設 18）
        max_workers: 並行股票數（Railway 低記憶體建議 4）

    Returns:
        {"signals_added": int, "signals_skipped": int, "stocks_ok": int}
    """
    print(f"\n{'='*55}")
    print(f"📊 歷史回測啟動（過去 {months} 個月）")
    print(f"   策略：{list(ALL_STRATEGIES.keys())}")
    print(f"{'='*55}")

    init_db()

    # 取得選股宇宙
    universe = get_universe()
    print(f"  📋 選股宇宙：{len(universe)} 支")

    # 並行抓取歷史 OHLCV
    print(f"\n📥 抓取 {months} 個月歷史資料（並行 {max_workers} 執行緒）...")
    ohlcv_map: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fetch_ohlcv, sym, months): sym for sym in universe}
        done = 0
        for fut in as_completed(futs):
            sym  = futs[fut]
            try:
                data = fut.result()
            except Exception as e:
                print(f"    ⚠️  {sym} 下載失敗：{e}")
                data = []
            if data and len(data) > MIN_BARS:
                ohlcv_map[sym] = data
            done += 1
            if done % 10 == 0:
                print(f"    下載進度：{done}/{len(universe)}")
            time.sleep(0.02)   # 避免打爆 Yahoo Finance rate limit

    ok_count = len(ohlcv_map)
    print(f"  ✅ 可用歷史資料：{ok_count}/{len(universe)} 支（>= {MIN_BARS} 根）")

    # 滑動視窗回測（依序，不並行，SQLite WAL 足夠）
    print(f"\n🔄 滑動視窗回測...")
    t0 = time.time()
    total_added = total_skipped = processed = 0

    for sym, etf_source in universe.items():
        ohlcv = ohlcv_map.get(sym)
        if not ohlcv:
            continue
        a, s = _backtest_one_stock(sym, etf_source, ohlcv)
        total_added   += a
        total_skipped += s
        processed     += 1
        if processed % 5 == 0:
            elapsed = time.time() - t0
            print(f"    回測進度：{processed}/{ok_count}  +{total_added} 筆  ({elapsed:.0f}s)")

    elapsed = time.time() - t0
    print(f"\n{'='*55}")
    print(f"✅ 歷史回測完成  ({elapsed:.0f}s)")
    print(f"   新增信號：{total_added} 筆")
    print(f"   跳過重複：{total_skipped} 筆")
    print(f"   處理股票：{processed}/{len(universe)} 支")
    print(f"{'='*55}\n")

    return {
        "signals_added":   total_added,
        "signals_skipped": total_skipped,
        "stocks_ok":       processed,
    }


# ── CLI ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="台股策略 Agent — 歷史回測")
    parser.add_argument("--months",      type=int, default=18,
                        help="回測期間（月，預設 18）")
    parser.add_argument("--max-workers", type=int, default=4,
                        help="並行下載執行緒數（預設 4，Railway 低記憶體建議 ≤ 4）")
    args = parser.parse_args()
    run_backtest(months=args.months, max_workers=args.max_workers)
