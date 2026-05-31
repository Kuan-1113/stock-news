"""
strategy_agent/runner.py — 策略 Agent 每日主程式

執行順序：
  1. 初始化 signal_db（冪等）
  2. 更新歷史待結算信號（5d / 10d 報酬）
  3. 取得 ETF 選股宇宙（0050 + 00981A）
  4. 抓取各股 OHLCV（Yahoo Finance，最近 6 個月）
  5. 逐股逐策略掃描信號
  6. 寫入新信號到 signal_db
  7. 計算各策略勝率
  8. Claude 審核 → 生成 Discord 明牌報告
  9. 發送 Discord Webhook

環境變數：
  DISCORD_STRATEGY  — 明牌專屬 Webhook（若無，使用 DISCORD_TW）
  DISCORD_TW        — 備用
  SIGNAL_DB_PATH    — SQLite 路徑（預設：專案根目錄）
  ANTHROPIC_API_KEY — Claude API Key
"""

from __future__ import annotations

import os
import sys
import json
import time
import datetime
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 確保 package root 在 sys.path ─────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from strategy_agent.signal_db   import (
    init_db, add_signal, get_pending_signals, update_result,
    get_all_strategy_stats, get_today_signals, mark_sent, get_summary_stats,
)
from strategy_agent.strategies  import ALL_STRATEGIES
from strategy_agent.etf_universe import get_universe

try:
    from shared.claude_client import simple_call as claude_call
except ImportError:
    def claude_call(prompt: str, max_tokens: int = 1800, **kw) -> str:  # type: ignore[misc]
        """備用：直接呼叫 Anthropic REST API"""
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return "（無 ANTHROPIC_API_KEY）"
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": max_tokens,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        return r.json()["content"][0]["text"].strip()


# ── Discord Webhook ────────────────────────────────────────────────

DISCORD_STRATEGY = os.environ.get("DISCORD_STRATEGY", "") or os.environ.get("DISCORD_TW", "")


def send_discord(content: str, webhook_url: str = "") -> bool:
    """送出 Discord Webhook 訊息（自動分割 2000 字元）"""
    url = webhook_url or DISCORD_STRATEGY
    if not url:
        print("  ⚠️  未設定 DISCORD_STRATEGY / DISCORD_TW，跳過發送")
        return False
    chunks = [content[i:i+1990] for i in range(0, len(content), 1990)]
    success = True
    for chunk in chunks:
        try:
            r = requests.post(url, json={"content": chunk}, timeout=10)
            if r.status_code not in (200, 204):
                print(f"  ❌ Discord 發送失敗：HTTP {r.status_code}")
                success = False
            time.sleep(0.5)   # 避免 rate limit
        except Exception as e:
            print(f"  ❌ Discord 發送例外：{e}")
            success = False
    return success


# ── OHLCV 抓取 ────────────────────────────────────────────────────

# 台股股票名稱快取（從已知 ETF 成分股的 TWSE 查詢結果）
_NAME_CACHE: dict[str, str] = {}


def fetch_ohlcv(symbol: str, months: int = 6) -> list[dict]:
    """
    從 Yahoo Finance v8 API 取得日線資料。
    回傳 list[dict]，格式：
      {"date": "YYYY-MM-DD", "open": float, "high": float,
       "low": float, "close": float, "volume": int}
    由舊到新排列。
    """
    yf_sym = f"{symbol}.TW"
    range_str = f"{months}mo"
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}",
            params={"interval": "1d", "range": range_str},
            headers={"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"},
            timeout=12,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        result = data.get("chart", {}).get("result", [None])[0]
        if not result:
            return []

        # 快取股票名稱
        short_name = result.get("meta", {}).get("shortName", "")
        if short_name and symbol not in _NAME_CACHE:
            _NAME_CACHE[symbol] = short_name

        timestamps = result.get("timestamp", [])
        q = result["indicators"]["quote"][0]
        opens   = q.get("open",   [])
        highs   = q.get("high",   [])
        lows    = q.get("low",    [])
        closes  = q.get("close",  [])
        volumes = q.get("volume", [])

        rows = []
        for i, ts in enumerate(timestamps):
            o = opens[i]  if i < len(opens)   else None
            h = highs[i]  if i < len(highs)   else None
            l = lows[i]   if i < len(lows)    else None
            c = closes[i] if i < len(closes)  else None
            v = volumes[i] if i < len(volumes) else 0
            if None in (o, h, l, c):
                continue
            dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
            rows.append({
                "date":   dt.strftime("%Y-%m-%d"),
                "open":   float(o),
                "high":   float(h),
                "low":    float(l),
                "close":  float(c),
                "volume": int(v) if v else 0,
            })
        return rows
    except Exception as e:
        print(f"    ⚠️  fetch_ohlcv({symbol}) 失敗：{e}")
        return []


def get_stock_name(symbol: str) -> str:
    """取得股票中文名稱（從快取或 TWSE OpenAPI）"""
    if symbol in _NAME_CACHE:
        return _NAME_CACHE[symbol]
    try:
        r = requests.get(
            "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        if r.status_code == 200:
            for item in r.json():
                code = str(item.get("Code", "")).strip()
                name = str(item.get("Name", "")).strip()
                if code:
                    _NAME_CACHE[code] = name
        return _NAME_CACHE.get(symbol, symbol)
    except Exception:
        return symbol


# ── 步驟 2：更新歷史待結算信號 ────────────────────────────────────

def update_pending_results() -> int:
    """
    查找還沒有 result 的信號，用當前股價更新 win_5d / win_10d。
    回傳更新筆數。
    """
    today = datetime.date.today()
    pending = get_pending_signals(max_days=15)
    if not pending:
        print("  ℹ️  無待結算歷史信號")
        return 0

    # 收集需要抓 OHLCV 的股票（不重複）
    symbols_needed = list({row["symbol"] for row in pending})
    print(f"  📋 待結算信號：{len(pending)} 筆，涉及 {len(symbols_needed)} 支股票")

    # 平行抓 OHLCV（快一點）
    ohlcv_map: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        fut_map = {ex.submit(fetch_ohlcv, sym): sym for sym in symbols_needed}
        for fut in as_completed(fut_map):
            sym = fut_map[fut]
            ohlcv_map[sym] = fut.result()

    updated = 0
    for row in pending:
        sig_id     = row["id"]
        sig_date   = datetime.date.fromisoformat(row["date"])
        symbol     = row["symbol"]
        entry_px   = row["entry_price"]
        ohlcv      = ohlcv_map.get(symbol, [])

        # 建立 date → close 的對應
        date_close = {r["date"]: r["close"] for r in ohlcv}

        # 計算信號日後第 N 個交易日的日期（只算有資料的交易日）
        dates_after = sorted(
            d for d in date_close
            if datetime.date.fromisoformat(d) > sig_date
        )

        needs_5d  = row["win_5d"]  is None
        needs_10d = row["win_10d"] is None

        if needs_5d and len(dates_after) >= 5:
            price_5d = date_close[dates_after[4]]   # index 4 = 第5個交易日
            update_result(sig_id, 5, price_5d, entry_px)
            updated += 1

        if needs_10d and len(dates_after) >= 10:
            price_10d = date_close[dates_after[9]]   # index 9 = 第10個交易日
            update_result(sig_id, 10, price_10d, entry_px)
            updated += 1

    print(f"  ✅ 歷史信號更新：{updated} 筆")
    return updated


# ── 步驟 5：執行策略掃描 ──────────────────────────────────────────

def run_screener(
    universe: dict[str, str],
    ohlcv_map: dict[str, list[dict]],
    today: str,
) -> list[dict]:
    """
    對每支股票逐策略掃描，回傳今日觸發信號清單。
    每筆：{"symbol", "name", "etf_source", "strategy", "strategy_label",
           "entry_price", "detail"}
    """
    signals = []
    strategies = list(ALL_STRATEGIES.items())   # [(key, (label, fn)), ...]

    for symbol, etf_source in universe.items():
        ohlcv = ohlcv_map.get(symbol, [])
        if not ohlcv:
            continue
        # 確認今日有資料（最後一筆是今日）
        if ohlcv[-1]["date"] != today:
            continue

        entry_price = ohlcv[-1]["close"]
        name        = _NAME_CACHE.get(symbol, symbol)

        for strat_key, (strat_label, strat_fn) in strategies:
            try:
                triggered, detail = strat_fn(ohlcv)
            except Exception as e:
                print(f"    ⚠️  {symbol} / {strat_key} 例外：{e}")
                continue
            if triggered:
                signals.append({
                    "symbol":         symbol,
                    "name":           name,
                    "etf_source":     etf_source,
                    "strategy":       strat_key,
                    "strategy_label": strat_label,
                    "entry_price":    entry_price,
                    "detail":         detail,
                })

    return signals


# ── 步驟 8：Claude 審核報告 ───────────────────────────────────────

def build_claude_report(
    today: str,
    signals: list[dict],
    strategy_stats: dict,
    summary: dict,
) -> str:
    """
    呼叫 Claude 對今日信號做審核與摘要，回傳 Discord 可直接發送的文字。
    若信號為空，回傳空字串（代表不發送）。
    """
    if not signals:
        return ""

    # ── 整理策略勝率表 ────────────────────────────────────────────
    stats_lines = []
    for key, (label, _) in ALL_STRATEGIES.items():
        wr, cnt, avg_r = strategy_stats.get(key, (None, 0, 0.0))
        if wr is not None:
            stats_lines.append(
                f"  {label}：勝率 {wr:.0f}%（{cnt} 筆，平均報酬 {avg_r:+.1f}%）"
            )
        else:
            stats_lines.append(f"  {label}：無歷史資料")
    stats_text = "\n".join(stats_lines)

    # ── 整理今日信號 ──────────────────────────────────────────────
    sig_lines = []
    for s in signals:
        wr, cnt, avg_r = strategy_stats.get(s["strategy"], (None, 0, 0.0))
        wr_str = f"（勝率{wr:.0f}%）" if wr else "（無歷史）"
        sig_lines.append(
            f"  • {s['symbol']} {s['name']} [{s['strategy_label']}]{wr_str}"
            f"  進場 ${s['entry_price']:.1f}，{s['detail']}"
            f"  ETF：{s['etf_source']}"
        )
    sigs_text = "\n".join(sig_lines)

    # ── Claude 提示 ───────────────────────────────────────────────
    prompt = f"""你是台股量化策略助理，今天 {today} 的策略選股系統找到以下信號，請幫我整理成 Discord 明牌推薦報告。

【策略歷史勝率（近60筆信號）】
{stats_text}

【今日觸發信號（{len(signals)} 筆）】
{sigs_text}

【整體統計】
總信號：{summary['total']}筆｜整體勝率：{summary.get('winrate') or '積累中'}%

請依照以下要求輸出：
1. 只推薦有正向勝率（>50%）或有強力技術面的股票，勝率不明的也可提（標示「無歷史，觀察中」）
2. 每支股票給一句點評（技術面強弱、注意事項）
3. 尾段加一句整體市場觀察（當日信號數量、哪種策略今日較多）
4. 用繁體中文，Emoji 可適量使用，不超過 1800 字
5. 開頭用：「📊 今日策略明牌 {today}」
6. 末尾加：「⚠️ 以上為技術面信號，非投資建議，請自行評估風險」"""

    print("  🤖 呼叫 Claude 生成明牌報告...")
    try:
        return claude_call(prompt, max_tokens=1800)
    except Exception as e:
        print(f"  ❌ Claude 呼叫失敗：{e}")
        # 降級：直接組一個純文字版本
        return _fallback_report(today, signals, strategy_stats, summary)


def _fallback_report(
    today: str,
    signals: list[dict],
    strategy_stats: dict,
    summary: dict,
) -> str:
    """Claude 失敗時的備用純文字報告"""
    lines = [f"📊 今日策略明牌 {today}\n"]
    for s in signals:
        wr, cnt, avg_r = strategy_stats.get(s["strategy"], (None, 0, 0.0))
        wr_str = f"勝率{wr:.0f}%" if wr else "無歷史"
        lines.append(
            f"• **{s['symbol']} {s['name']}** [{s['strategy_label']}]（{wr_str}）\n"
            f"  進場：${s['entry_price']:.1f}　{s['detail']}\n"
        )
    lines.append(f"\n整體勝率：{summary.get('winrate') or '積累中'}%（共 {summary['total']} 筆歷史信號）")
    lines.append("\n⚠️ 以上為技術面信號，非投資建議，請自行評估風險")
    return "\n".join(lines)


# ── 主流程 ────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> dict:
    """
    執行一次完整的策略 Agent 流程。

    Args:
        dry_run: True = 不發 Discord，只回傳結果 dict

    Returns:
        {
          "date":        str,
          "signals":     list[dict],
          "stats":       dict,
          "discord_sent": bool,
          "report":      str,
        }
    """
    today = datetime.date.today().isoformat()
    print(f"\n{'='*55}")
    print(f"🚀 Strategy Agent 啟動  {today}")
    print(f"{'='*55}")

    # ── 1. 初始化 DB ──────────────────────────────────────────────
    init_db()

    # ── 2. 更新歷史信號結算 ───────────────────────────────────────
    print("\n[步驟 2] 更新歷史信號...")
    update_pending_results()

    # ── 3. 取得 ETF 宇宙 ─────────────────────────────────────────
    print("\n[步驟 3] 取得 ETF 選股宇宙...")
    universe = get_universe()
    print(f"  → 宇宙：{len(universe)} 支股票")

    # ── 4. 批量抓取 OHLCV ────────────────────────────────────────
    print(f"\n[步驟 4] 抓取日線資料（{len(universe)} 支，6 個月）...")
    ohlcv_map: dict[str, list[dict]] = {}

    def _fetch_one(sym: str) -> tuple[str, list[dict]]:
        data = fetch_ohlcv(sym)
        time.sleep(0.05)   # 輕微節流，避免 429
        return sym, data

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_fetch_one, sym): sym for sym in universe}
        done = 0
        for fut in as_completed(futs):
            sym, data = fut.result()
            ohlcv_map[sym] = data
            done += 1
            if done % 10 == 0:
                print(f"    進度：{done}/{len(universe)}")

    ok_count = sum(1 for v in ohlcv_map.values() if v)
    print(f"  ✅ 成功抓取：{ok_count}/{len(universe)} 支")

    # ── 5. 策略掃描 ───────────────────────────────────────────────
    print(f"\n[步驟 5] 執行策略掃描（{len(universe)} 支 × {len(ALL_STRATEGIES)} 策略）...")
    new_signals = run_screener(universe, ohlcv_map, today)
    print(f"  📡 今日觸發信號：{len(new_signals)} 筆")

    # ── 6. 寫入新信號 ─────────────────────────────────────────────
    print("\n[步驟 6] 寫入信號資料庫...")
    added = 0
    for s in new_signals:
        ok = add_signal(
            date        = today,
            symbol      = s["symbol"],
            name        = s["name"],
            etf_source  = s["etf_source"],
            strategy    = s["strategy"],
            entry_price = s["entry_price"],
        )
        if ok:
            added += 1
    print(f"  ✅ 新增 {added} 筆（含重複跳過）")

    # ── 7. 計算勝率 ───────────────────────────────────────────────
    print("\n[步驟 7] 計算各策略勝率...")
    strategy_keys = list(ALL_STRATEGIES.keys())
    strategy_stats = get_all_strategy_stats(strategy_keys)
    for key, (wr, cnt, avg_r) in strategy_stats.items():
        label = ALL_STRATEGIES[key][0]
        if wr is not None:
            print(f"  {label}：{wr:.0f}%（{cnt} 筆，均報酬 {avg_r:+.1f}%）")
        else:
            print(f"  {label}：無歷史資料")

    summary = get_summary_stats()

    # ── 8. Claude 生成報告 ────────────────────────────────────────
    print("\n[步驟 8] 生成 Discord 明牌報告...")
    report = build_claude_report(today, new_signals, strategy_stats, summary)

    if not report:
        print("  ℹ️  今日無信號，略過 Discord 發送")
        return {
            "date":         today,
            "signals":      new_signals,
            "stats":        strategy_stats,
            "discord_sent": False,
            "report":       "",
        }

    # ── 9. 發送 Discord ───────────────────────────────────────────
    sent = False
    if not dry_run:
        print("\n[步驟 9] 發送 Discord 明牌...")
        sent = send_discord(report)
        if sent:
            # 標記已發送
            today_sigs = get_today_signals(today)
            ids = [r["id"] for r in today_sigs]
            mark_sent(ids)
            print(f"  ✅ Discord 發送完成（{len(ids)} 筆標記）")
    else:
        print("\n[步驟 9] dry_run 模式，略過 Discord 發送")
        print("─── 報告預覽 ───────────────────────────────────")
        print(report)
        print("────────────────────────────────────────────────")

    print(f"\n✅ Strategy Agent 完成  {datetime.datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*55}\n")

    return {
        "date":         today,
        "signals":      new_signals,
        "stats":        strategy_stats,
        "discord_sent": sent,
        "report":       report,
    }


# ── 純文字版勝率摘要（供 /明牌 Discord 指令用）──────────────────

def get_winrate_summary() -> str:
    """
    回傳各策略勝率的 Discord 格式摘要文字。
    供 /明牌 指令即時查詢。
    """
    lines = ["📊 **策略勝率統計**（近 60 筆有效信號）\n"]
    for key, (label, _) in ALL_STRATEGIES.items():
        wr, cnt, avg_r = get_all_strategy_stats([key]).get(key, (None, 0, 0.0))
        if wr is not None:
            bar = "█" * int(wr / 10) + "░" * (10 - int(wr / 10))
            lines.append(f"{label}\n  [{bar}] {wr:.0f}%，{cnt} 筆，均報酬 {avg_r:+.1f}%")
        else:
            lines.append(f"{label}\n  [──────────] 積累中（尚無足夠歷史）")

    summary = get_summary_stats()
    lines.append(
        f"\n總信號：{summary['total']} 筆"
        f"｜整體勝率：{summary.get('winrate') or '積累中'}%"
        f"｜勝 {summary['wins']} / 敗 {summary['losses']}"
        f"｜待結算 {summary['pending']}"
    )
    return "\n".join(lines)


def get_today_summary() -> str:
    """
    回傳今日信號摘要（供 /明牌 指令使用）。
    """
    today = datetime.date.today().isoformat()
    sigs = get_today_signals(today)
    if not sigs:
        return f"📭 今日（{today}）尚無策略信號\n（策略 Agent 每日盤後自動執行）"

    strategy_keys = list(ALL_STRATEGIES.keys())
    stats = get_all_strategy_stats(strategy_keys)

    lines = [f"📊 今日策略信號 {today}（{len(sigs)} 筆）\n"]
    for s in sigs:
        key   = s["strategy"]
        label = ALL_STRATEGIES[key][0] if key in ALL_STRATEGIES else key
        wr, cnt, _ = stats.get(key, (None, 0, 0.0))
        wr_str = f"勝率{wr:.0f}%" if wr else "無歷史"
        lines.append(
            f"• **{s['symbol']} {s['name']}** [{label}]（{wr_str}）"
            f"  進場 ${s['entry_price']:.1f}｜ETF：{s['etf_source']}"
        )
    lines.append("\n⚠️ 技術面信號，非投資建議")
    return "\n".join(lines)


# ── CLI 入口 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="台股策略 Agent — 每日明牌系統")
    parser.add_argument("--dry-run",  action="store_true", help="不發 Discord，僅列印報告")
    parser.add_argument("--stats",    action="store_true", help="只顯示勝率統計，不跑掃描")
    parser.add_argument("--today",    action="store_true", help="只顯示今日信號，不跑掃描")
    args = parser.parse_args()

    if args.stats:
        init_db()
        print(get_winrate_summary())
    elif args.today:
        init_db()
        print(get_today_summary())
    else:
        run(dry_run=args.dry_run)
