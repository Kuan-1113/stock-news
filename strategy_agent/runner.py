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
    update_confidence_stars,
)
from strategy_agent.strategies  import ALL_STRATEGIES, CHIP_STRATEGIES, ALL_STRATEGIES_COMBINED
from strategy_agent.etf_universe import get_universe
from strategy_agent.chip_data   import init_chip_db, update_chip_data, get_chip_map

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
    """送出 Discord Webhook 訊息（按換行智慧分割，避免截斷句子）"""
    url = webhook_url or DISCORD_STRATEGY
    if not url:
        print("  ⚠️  未設定 DISCORD_STRATEGY / DISCORD_TW，跳過發送")
        return False

    # 按換行切，每段不超過 1900 字元（留 100 字元 buffer）
    chunks: list[str] = []
    current = ""
    for line in content.split("\n"):
        addition = (line + "\n")
        if len(current) + len(addition) > 1900:
            if current:
                chunks.append(current.rstrip())
            # 單行超長才硬切
            while len(addition) > 1900:
                chunks.append(addition[:1900])
                addition = addition[1900:]
            current = addition
        else:
            current += addition
    if current.strip():
        chunks.append(current.rstrip())

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
    # 若代碼已含「.」（如 2330.TW）或以「^」開頭（指數），直接使用；否則補 .TW
    if symbol.startswith("^") or "." in symbol:
        yf_sym = symbol
    else:
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
    universe:           dict[str, str],
    ohlcv_map:          dict[str, list[dict]],
    today:              str,
    chip_map:           dict[str, list[dict]] | None = None,
    active_strategies:  frozenset | None = None,
) -> list[dict]:
    """
    對每支股票執行指標派 + 籌碼派策略掃描，計算信心星級後回傳。

    Args:
        active_strategies: 限制只跑這些指標派策略 key（None = 全部）。
                           籌碼派策略不受此限制（市況無關）。

    每筆信號格式：
      {"symbol", "name", "etf_source", "strategy", "strategy_label",
       "strategy_type", "entry_price", "detail", "confidence_stars"}

    信心星級定義：
      ⭐   (1) 只有單一維度（純指標 或 純籌碼）
      ⭐⭐  (2) 同支股票有兩個以上信號（同維度）
      ⭐⭐⭐ (3) 同支股票同時有指標派 AND 籌碼派信號確認
    """
    chip_map = chip_map or {}
    signals  = []

    # ── 指標派（OHLCV）掃描 ───────────────────────────────────────
    for symbol, etf_source in universe.items():
        ohlcv = ohlcv_map.get(symbol, [])
        if not ohlcv or ohlcv[-1]["date"] != today:
            continue
        entry_price = ohlcv[-1]["close"]
        name        = _NAME_CACHE.get(symbol, symbol)

        for strat_key, (strat_label, strat_fn) in ALL_STRATEGIES.items():
            # 市場狀態過濾：若有 active_strategies 限制，跳過不在清單的策略
            if active_strategies is not None and strat_key not in active_strategies:
                continue
            try:
                triggered, detail = strat_fn(ohlcv)
            except Exception as e:
                print(f"    ⚠️  {symbol}/{strat_key} 例外：{e}")
                continue
            if triggered:
                signals.append({
                    "symbol":         symbol,
                    "name":           name,
                    "etf_source":     etf_source,
                    "strategy":       strat_key,
                    "strategy_label": strat_label,
                    "strategy_type":  "technical",
                    "entry_price":    entry_price,
                    "detail":         detail,
                    "confidence_stars": 1,
                })

    # ── 籌碼派掃描 ────────────────────────────────────────────────
    chip_triggered: set[str] = set()   # 本次有籌碼信號的 symbol
    for symbol, etf_source in universe.items():
        chip_hist = chip_map.get(symbol, [])
        if not chip_hist:
            continue

        # 抓 entry_price（從 OHLCV，若無則跳過）
        ohlcv       = ohlcv_map.get(symbol, [])
        entry_price = ohlcv[-1]["close"] if (ohlcv and ohlcv[-1]["date"] == today) else None
        if entry_price is None:
            continue
        name = _NAME_CACHE.get(symbol, symbol)

        for strat_key, (strat_label, strat_fn) in CHIP_STRATEGIES.items():
            try:
                triggered, detail = strat_fn(chip_hist)
            except Exception as e:
                print(f"    ⚠️  {symbol}/{strat_key} 例外：{e}")
                continue
            if triggered:
                chip_triggered.add(symbol)
                signals.append({
                    "symbol":         symbol,
                    "name":           name,
                    "etf_source":     etf_source,
                    "strategy":       strat_key,
                    "strategy_label": strat_label,
                    "strategy_type":  "chip",
                    "entry_price":    entry_price,
                    "detail":         detail,
                    "confidence_stars": 1,
                })

    # ── 計算信心星級 ──────────────────────────────────────────────
    from collections import defaultdict
    sym_types: dict[str, set] = defaultdict(set)
    sym_count: dict[str, int] = defaultdict(int)
    for sig in signals:
        sym_types[sig["symbol"]].add(sig["strategy_type"])
        sym_count[sig["symbol"]] += 1

    for sig in signals:
        sym = sig["symbol"]
        types = sym_types[sym]
        if "technical" in types and "chip" in types:
            sig["confidence_stars"] = 3   # ⭐⭐⭐ 指標 + 籌碼雙確認
        elif sym_count[sym] >= 2:
            sig["confidence_stars"] = 2   # ⭐⭐  同維度多個信號
        else:
            sig["confidence_stars"] = 1   # ⭐   單一信號

    # 依星級 → 股票代號 排序（⭐⭐⭐ 優先顯示）
    signals.sort(key=lambda s: (-s["confidence_stars"], s["symbol"]))
    return signals


# ── 步驟 8：Claude 審核報告 ───────────────────────────────────────

def build_claude_report(
    today: str,
    signals: list[dict],
    strategy_stats: dict,
    summary: dict,
    regime: str = "unknown",
) -> str:
    """
    呼叫 Claude 對今日信號做審核與摘要，回傳 Discord 可直接發送的文字。
    若信號為空，回傳空字串（代表不發送）。
    """
    if not signals:
        return ""

    # ── 整理策略勝率表 ────────────────────────────────────────────
    stats_lines = ["【指標派勝率】"]
    for key, (label, _) in ALL_STRATEGIES.items():
        wr, cnt, avg_r = strategy_stats.get(key, (None, 0, 0.0))
        if wr is not None:
            bar = "█" * int(wr / 10) + "░" * (10 - int(wr / 10))
            stats_lines.append(f"  {label}：[{bar}] {wr:.0f}%（{cnt}筆，均報酬{avg_r:+.1f}%）")
        else:
            stats_lines.append(f"  {label}：積累中")
    stats_lines.append("【籌碼派勝率】")
    for key, (label, _) in CHIP_STRATEGIES.items():
        wr, cnt, avg_r = strategy_stats.get(key, (None, 0, 0.0))
        if wr is not None:
            bar = "█" * int(wr / 10) + "░" * (10 - int(wr / 10))
            stats_lines.append(f"  {label}：[{bar}] {wr:.0f}%（{cnt}筆，均報酬{avg_r:+.1f}%）")
        else:
            stats_lines.append(f"  {label}：積累中")
    stats_text = "\n".join(stats_lines)

    # ── 整理今日信號（依星級分組）────────────────────────────────
    star_emoji = {3: "⭐⭐⭐", 2: "⭐⭐", 1: "⭐"}
    star_label = {3: "指標+籌碼雙確認", 2: "多重信號", 1: "單一信號"}

    sig_lines = []
    current_stars = None
    for s in signals:
        stars = s.get("confidence_stars", 1)
        if stars != current_stars:
            current_stars = stars
            sig_lines.append(
                f"\n{star_emoji[stars]} **{star_label[stars]}**"
            )
        wr, cnt, avg_r = strategy_stats.get(s["strategy"], (None, 0, 0.0))
        wr_str  = f"勝率{wr:.0f}%" if wr else "無歷史"
        type_tag = "📊指標" if s["strategy_type"] == "technical" else "💹籌碼"
        sig_lines.append(
            f"  • **{s['symbol']} {s['name']}** [{type_tag}｜{s['strategy_label']}]（{wr_str}）\n"
            f"    進場 ${s['entry_price']:.1f}　{s['detail']}"
        )
    sigs_text = "\n".join(sig_lines)

    # 計算星級分布
    stars3 = sum(1 for s in signals if s.get("confidence_stars") == 3)
    stars2 = sum(1 for s in signals if s.get("confidence_stars") == 2)
    stars1 = sum(1 for s in signals if s.get("confidence_stars") == 1)

    # ── Claude 提示 ───────────────────────────────────────────────
    regime_zh = {"trend": "趨勢市（ADX≥25）📈", "range": "震盪盤（ADX<25）📉"}.get(regime, "狀態未知")
    prompt = f"""你是台股量化策略助理，今天 {today} 同時使用了「指標派」和「籌碼派」雙系統選股。
大盤市場狀態：{regime_zh}（已自動過濾適合本狀態的策略）
請整理成 Discord 明牌推薦報告。

【策略歷史勝率】
{stats_text}

【今日觸發信號（共 {len(signals)} 筆，⭐⭐⭐{stars3} / ⭐⭐{stars2} / ⭐{stars1}）】
{sigs_text}

【整體統計】
歷史總信號：{summary['total']}筆｜整體勝率：{summary.get('winrate') or '積累中'}%

輸出規範：
1. 開頭用「📊 今日策略明牌 {today}」，依星級排序（⭐⭐⭐優先）
2. ⭐⭐⭐的股票重點分析（指標面+籌碼面各一句）；⭐⭐簡短點評；⭐僅列名觀察
3. 指出今日哪個策略/族群信號最集中
4. 末尾加：「⚠️ 量化信號，非投資建議，請自行評估風險」
5. 繁體中文，1800 字以內，可用 Emoji"""

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
    star_emoji = {3: "⭐⭐⭐", 2: "⭐⭐", 1: "⭐"}
    lines = [f"📊 今日策略明牌 {today}\n"]
    for s in signals:
        wr, cnt, avg_r = strategy_stats.get(s["strategy"], (None, 0, 0.0))
        wr_str = f"勝率{wr:.0f}%" if wr else "無歷史"
        stars  = star_emoji.get(s.get("confidence_stars", 1), "⭐")
        type_tag = "📊指標" if s["strategy_type"] == "technical" else "💹籌碼"
        lines.append(
            f"{stars} **{s['symbol']} {s['name']}** [{type_tag}｜{s['strategy_label']}]（{wr_str}）\n"
            f"  進場：${s['entry_price']:.1f}　{s['detail']}\n"
        )
    lines.append(f"\n整體勝率：{summary.get('winrate') or '積累中'}%（共 {summary['total']} 筆歷史信號）")
    lines.append("\n⚠️ 量化信號，非投資建議，請自行評估風險")
    return "\n".join(lines)


# ── ⭐⭐⭐ 即時警報 ─────────────────────────────────────────────────

def _fire_immediate_alert(signals_3star: list[dict], today: str) -> None:
    """
    ⭐⭐⭐ 即時警報：指標派 + 籌碼派雙重確認時立即發送 Discord，
    不等每日 17:00 匯總報告，讓用戶第一時間知道最高信心的機會。
    """
    if not signals_3star:
        return

    # 以股票為單位，彙整所有信號細節
    sym_info: dict[str, dict] = {}
    for s in signals_3star:
        sym = s["symbol"]
        if sym not in sym_info:
            sym_info[sym] = {
                "name":        s["name"],
                "entry_price": s["entry_price"],
                "details":     [],
            }
        type_tag = "📊指標" if s["strategy_type"] == "technical" else "💹籌碼"
        sym_info[sym]["details"].append(
            f"[{type_tag}｜{s['strategy_label']}] {s['detail']}"
        )

    n = len(sym_info)
    lines = [
        f"⚡ **⭐⭐⭐ 即時警報** — {today}",
        f"以下 **{n}** 支獲指標派 + 籌碼派雙重確認，建議優先關注",
        "",
    ]
    for sym, info in sym_info.items():
        lines.append(f"**{sym} {info['name']}**  進場 ${info['entry_price']:.1f}")
        for d in info["details"]:
            lines.append(f"  • {d}")
        lines.append("")
    lines.append("⚠️ 量化信號，非投資建議，請自行評估風險")

    send_discord("\n".join(lines))
    print(f"  ⚡ 即時警報已發送：{n} 支 ⭐⭐⭐ 雙確認")


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

    # ── 1. 初始化 DB（指標 + 籌碼）─────────────────────────────────
    init_db()
    init_chip_db()

    # ── 2. 更新歷史信號結算 ───────────────────────────────────────
    print("\n[步驟 2] 更新歷史信號...")
    update_pending_results()

    # ── 3. 取得 ETF 宇宙 ─────────────────────────────────────────
    print("\n[步驟 3] 取得 ETF 選股宇宙...")
    universe = get_universe()
    print(f"  → 宇宙：{len(universe)} 支股票")

    # ── 3b. 市場狀態感知（ADX Regime Filter）─────────────────────
    print("\n[步驟 3b] 偵測大盤市場狀態（ADX）...")
    from strategy_agent.strategies import (
        get_market_regime, TREND_STRATEGIES, MEAN_REVERSION_STRATEGIES
    )
    twii_ohlcv = fetch_ohlcv("^TWII", months=3)
    regime = get_market_regime(twii_ohlcv)
    if regime == "trend":
        active_strats: frozenset | None = TREND_STRATEGIES | frozenset(CHIP_STRATEGIES.keys())
        print(f"  📈 趨勢市（ADX≥25）→ 啟用動量策略 + 籌碼策略")
    elif regime == "range":
        active_strats = MEAN_REVERSION_STRATEGIES | frozenset(CHIP_STRATEGIES.keys())
        print(f"  📉 震盪盤（ADX<25）→ 啟用均值回歸策略 + 籌碼策略")
    else:
        active_strats = None  # unknown → 全部策略均啟用
        print(f"  ❓ 狀態未知（大盤資料不足）→ 啟用全部策略")

    # ── 4. 並行抓取 OHLCV + 籌碼資料 ────────────────────────────
    print(f"\n[步驟 4] 抓取日線資料 + TWSE 籌碼資料...")
    ohlcv_map: dict[str, list[dict]] = {}

    def _fetch_one(sym: str) -> tuple[str, list[dict]]:
        data = fetch_ohlcv(sym)
        time.sleep(0.05)
        return sym, data

    # OHLCV 與籌碼並行抓取
    with ThreadPoolExecutor(max_workers=6) as ex:
        ohlcv_futs = {ex.submit(_fetch_one, sym): sym for sym in universe}
        chip_fut   = ex.submit(update_chip_data, today)   # 籌碼並行

        done = 0
        for fut in as_completed(ohlcv_futs):
            sym, data = fut.result()
            ohlcv_map[sym] = data
            done += 1
            if done % 10 == 0:
                print(f"    OHLCV 進度：{done}/{len(universe)}")

        chip_stored = chip_fut.result()

    ok_count = sum(1 for v in ohlcv_map.values() if v)
    print(f"  ✅ OHLCV：{ok_count}/{len(universe)} 支")
    print(f"  ✅ 籌碼資料：{chip_stored} 支")

    # 取出籌碼歷史（最近 5 天）
    chip_map = get_chip_map(days=5)
    chip_covered = sum(1 for sym in universe if sym in chip_map)
    print(f"  📊 有籌碼歷史的股票：{chip_covered}/{len(universe)} 支")

    # ── 5. 策略掃描（指標 + 籌碼）───────────────────────────────
    n_tech = len(ALL_STRATEGIES) if active_strats is None else len(active_strats & set(ALL_STRATEGIES))
    n_chip = len(CHIP_STRATEGIES)
    regime_label = {"trend": "趨勢市", "range": "震盪盤", "unknown": "未知"}.get(regime, regime)
    print(f"\n[步驟 5] 執行策略掃描（{len(universe)} 支 × {n_tech} 指標 + {n_chip} 籌碼 | {regime_label}）...")
    new_signals = run_screener(universe, ohlcv_map, today, chip_map, active_strats)
    stars3 = sum(1 for s in new_signals if s.get("confidence_stars") == 3)
    stars2 = sum(1 for s in new_signals if s.get("confidence_stars") == 2)
    stars1 = sum(1 for s in new_signals if s.get("confidence_stars") == 1)
    print(f"  📡 今日觸發：{len(new_signals)} 筆（⭐⭐⭐{stars3} ⭐⭐{stars2} ⭐{stars1}）")

    # ── 6. 寫入新信號 ─────────────────────────────────────────────
    print("\n[步驟 6] 寫入信號資料庫...")
    added = 0
    for s in new_signals:
        ok = add_signal(
            date             = today,
            symbol           = s["symbol"],
            name             = s["name"],
            etf_source       = s["etf_source"],
            strategy         = s["strategy"],
            entry_price      = s["entry_price"],
            confidence_stars = s.get("confidence_stars", 1),
            strategy_type    = s.get("strategy_type", "technical"),
        )
        if ok:
            added += 1

    # 更新信心星級（同股票可能有多個信號，需同步更新 DB）
    from collections import defaultdict
    sym_stars: dict[str, int] = {}
    for s in new_signals:
        sym = s["symbol"]
        stars = s.get("confidence_stars", 1)
        sym_stars[sym] = max(sym_stars.get(sym, 1), stars)
    for sym, stars in sym_stars.items():
        if stars > 1:
            update_confidence_stars(today, sym, stars)
    print(f"  ✅ 新增 {added} 筆（含重複跳過）")

    # ── 6b. ⭐⭐⭐ 即時警報（雙確認立即通知，不等 17:00 匯總）────
    stars3_new = [s for s in new_signals if s.get("confidence_stars") == 3]
    if stars3_new and not dry_run:
        print(f"\n[步驟 6b] ⭐⭐⭐ 即時警報（{len(stars3_new)} 筆雙確認信號）...")
        _fire_immediate_alert(stars3_new, today)

    # ── 7. 計算勝率（指標 + 籌碼）───────────────────────────────
    print("\n[步驟 7] 計算各策略勝率...")
    strategy_keys = list(ALL_STRATEGIES_COMBINED.keys())
    strategy_stats = get_all_strategy_stats(strategy_keys)
    for key, (wr, cnt, avg_r) in strategy_stats.items():
        label = ALL_STRATEGIES_COMBINED.get(key, (key,))[0]
        if wr is not None:
            print(f"  {label}：{wr:.0f}%（{cnt} 筆，均報酬 {avg_r:+.1f}%）")
        else:
            print(f"  {label}：無歷史資料")

    summary = get_summary_stats()

    # ── 8. Claude 生成報告 ────────────────────────────────────────
    print("\n[步驟 8] 生成 Discord 明牌報告...")
    report = build_claude_report(today, new_signals, strategy_stats, summary, regime)

    if not report:
        print("  ℹ️  今日無信號，略過 Discord 發送")
        return {
            "date":         today,
            "signals":      new_signals,
            "stats":        strategy_stats,
            "discord_sent": False,
            "report":       "",
            "regime":       regime,
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
        "regime":       regime,
    }


# ── 純文字版勝率摘要（供 /明牌 Discord 指令用）──────────────────

def get_winrate_summary() -> str:
    """回傳各策略勝率的 Discord 格式摘要（指標派 + 籌碼派分組）"""
    all_keys  = list(ALL_STRATEGIES_COMBINED.keys())
    all_stats = get_all_strategy_stats(all_keys)

    lines = ["📊 **策略勝率統計**（近 60 筆有效信號）\n"]

    lines.append("**📈 指標派**")
    for key, (label, _) in ALL_STRATEGIES.items():
        wr, cnt, avg_r = all_stats.get(key, (None, 0, 0.0))
        if wr is not None:
            bar = "█" * int(wr / 10) + "░" * (10 - int(wr / 10))
            lines.append(f"{label}\n  [{bar}] {wr:.0f}%  {cnt}筆  均報酬{avg_r:+.1f}%")
        else:
            lines.append(f"{label}\n  [──────────] 積累中")

    lines.append("\n**💹 籌碼派**")
    for key, (label, _) in CHIP_STRATEGIES.items():
        wr, cnt, avg_r = all_stats.get(key, (None, 0, 0.0))
        if wr is not None:
            bar = "█" * int(wr / 10) + "░" * (10 - int(wr / 10))
            lines.append(f"{label}\n  [{bar}] {wr:.0f}%  {cnt}筆  均報酬{avg_r:+.1f}%")
        else:
            lines.append(f"{label}\n  [──────────] 積累中（籌碼派初期）")

    summary = get_summary_stats()
    lines.append(
        f"\n總信號：{summary['total']} 筆"
        f"｜整體勝率：{summary.get('winrate') or '積累中'}%"
        f"｜勝 {summary['wins']} / 敗 {summary['losses']}"
        f"｜待結算 {summary['pending']}"
    )
    return "\n".join(lines)


def get_today_summary() -> str:
    """回傳今日信號摘要（供 /明牌 指令使用，依星級排序）"""
    today = datetime.date.today().isoformat()
    sigs  = get_today_signals(today)
    if not sigs:
        return f"📭 今日（{today}）尚無策略信號\n（策略 Agent 每日 17:00 盤後自動執行）"

    strategy_keys = list(ALL_STRATEGIES_COMBINED.keys())
    stats = get_all_strategy_stats(strategy_keys)

    star_emoji = {3: "⭐⭐⭐", 2: "⭐⭐", 1: "⭐"}
    # 依星級排序
    sorted_sigs = sorted(sigs, key=lambda s: -(s["confidence_stars"] or 1))

    lines = [f"📊 今日策略信號 {today}（{len(sigs)} 筆）\n"]
    prev_stars = None
    for s in sorted_sigs:
        stars = s["confidence_stars"] or 1
        key   = s["strategy"]
        label = ALL_STRATEGIES_COMBINED.get(key, (key,))[0]
        stype = s.get("strategy_type", "technical")
        type_tag = "📊" if stype == "technical" else "💹"
        wr, cnt, _ = stats.get(key, (None, 0, 0.0))
        wr_str = f"勝率{wr:.0f}%" if wr else "無歷史"

        if stars != prev_stars:
            prev_stars = stars
            lines.append(f"\n{star_emoji[stars]}")
        lines.append(
            f"• **{s['symbol']} {s['name']}** {type_tag}[{label}]（{wr_str}）"
            f"  @${s['entry_price']:.1f}"
        )
    lines.append("\n⚠️ 量化信號，非投資建議")
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
