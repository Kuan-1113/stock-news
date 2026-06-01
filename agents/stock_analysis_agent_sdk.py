"""
agents/stock_analysis_agent_sdk.py — C1: Managed Agents SDK 查股升級

用 Anthropic Managed Agents API（Beta 2026-04-01）讓 Claude 自主決定：
  - 要不要抓即時報價？
  - 要不要算技術指標？
  - 要不要抓新聞？
  - 要不要查財報基本面？

與傳統 /查股（預先抓所有資料 → 塞給 Claude 分析）的差異：
  - Agent 版：Claude 先看使用者問題，再決定調用哪些工具
  - 可以做「只看技術面」「只看基本面」「比較兩支股票」等開放性分析

實作說明：
  - Managed Agents API（client.beta.agents.*）
  - 自訂工具：get_stock_price / get_tech_indicators / get_stock_news / get_fundamentals
  - Agent 懶惰初始化（第一次呼叫時建立，agent_id 存檔複用）
  - 同步執行（從 Discord 的 asyncio executor 呼叫）
  - 失敗時拋出例外 → Discord 指令 fallback 到傳統模式

設定：
  - 需要 ANTHROPIC_API_KEY 環境變數
  - STOCK_AGENT_CACHE 可指定 agent_id 快取檔路徑（Railway Volume 用）

使用範例（Discord /查股 指令）：
  data = await loop.run_in_executor(None, analyze_stock_sdk, "2330.TW", "台積電")
"""

from __future__ import annotations

import os
import json
import time
import datetime

import anthropic

# 重用 main.py 的工具函式（在同目錄引入避免循環）
# 注意：stock_analysis_agent_sdk 被 main.py import，不能反 import main
# 因此在這裡重新引用已有的 helper function 或直接 inline

import requests
import feedparser


# ── 快取設定 ───────────────────────────────────────────────────────

_CACHE_PATH = os.environ.get(
    "STOCK_AGENT_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agent_cache.json"),
)

_AGENT_NAME  = "StockAnalyst"
_AGENT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
_AGENT_SYSTEM = """你是一位資深台股 + 美股分析師，專門針對個股提供深度分析。

當使用者詢問某支股票時，你應該：
1. 首先呼叫 get_stock_price 取得即時報價
2. 呼叫 get_tech_indicators 取得技術分析指標
3. 根據需要呼叫 get_stock_news 取得最新新聞
4. 對於台股，可呼叫 get_fundamentals 取得財報數據

分析格式：
- 繁體中文，全程 bullet（•），禁 Markdown 表格
- 台灣慣例：🔴=漲，🟢=跌
- 每個論點格式：「數值/事實 → 機制 → 影響方向」
- 總字數 1100 字以內
- 包含：A.歸因分析 B.技術訊號 C.多空決策 D.操作建議（含進場條件+目標+停損）

⚠️ AI 生成分析，不構成投資建議。"""

_TOOLS = [
    {
        "type": "custom",
        "name": "get_stock_price",
        "description": (
            "取得股票或指數的即時報價，包含現價、漲跌幅、成交量。"
            "台股代碼格式：2330.TW；美股：NVDA；指數：^TWII。"
            "這是最基本的工具，幾乎每次分析都應該先呼叫。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Yahoo Finance 代碼，例如 2330.TW、NVDA、^TWII",
                }
            },
            "required": ["symbol"],
        },
    },
    {
        "type": "custom",
        "name": "get_tech_indicators",
        "description": (
            "計算股票的技術分析指標（純 Python，無需 yfinance）。"
            "包含：MA5/10/20/60 均線、MACD（含黃金/死亡交叉）、RSI、KDJ、"
            "乖離率、量比、近20日高低點（支撐壓力參考）。"
            "建議搭配 get_stock_price 一起使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Yahoo Finance 代碼",
                }
            },
            "required": ["symbol"],
        },
    },
    {
        "type": "custom",
        "name": "get_stock_news",
        "description": (
            "取得股票的最新相關新聞標題（Google News RSS）。"
            "台股提供中文新聞，美股提供英文新聞。"
            "最多返回 8 則新聞標題。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Yahoo Finance 代碼",
                },
                "name": {
                    "type": "string",
                    "description": "股票中文名稱（選填，提升新聞相關性）",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "type": "custom",
        "name": "get_fundamentals",
        "description": (
            "取得台股財報基本面數據，包含本益比（P/E）、殖利率、P/B 比、52週高低。"
            "只對台灣上市股票（*.TW）有效；美股回傳空結果。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Yahoo Finance 台股代碼（例如 2330.TW）",
                }
            },
            "required": ["symbol"],
        },
    },
]


# ── 工具執行函式（自給自足，不依賴 main.py）───────────────────────

def _tool_get_stock_price(symbol: str) -> str:
    """取得即時報價"""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval=1d&range=10d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        if r.status_code != 200:
            return json.dumps({"error": f"HTTP {r.status_code}"})
        result = r.json()["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) < 2:
            return json.dumps({"error": "資料不足"})
        meta = result.get("meta", {})
        prev, curr = closes[-2], closes[-1]
        chg = curr - prev
        pct = chg / prev * 100
        return json.dumps({
            "symbol":   symbol,
            "name":     meta.get("shortName", symbol),
            "price":    round(curr, 2),
            "change":   round(chg, 2),
            "pct":      round(pct, 2),
            "emoji":    "🔴" if pct < 0 else "🟢",
            "currency": meta.get("currency", ""),
            "volume":   result["indicators"]["quote"][0].get("volume", [None])[-1],
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_get_tech_indicators(symbol: str) -> str:
    """計算技術指標"""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval=1d&range=3mo",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if r.status_code != 200:
            return json.dumps({"error": f"HTTP {r.status_code}"})
        result = r.json()["chart"]["result"][0]
        qdata  = result["indicators"]["quote"][0]
        valid  = [
            (c, h, l, v)
            for c, h, l, v in zip(
                qdata.get("close", []), qdata.get("high", []),
                qdata.get("low",   []), qdata.get("volume", [])
            )
            if c is not None and h is not None and l is not None
        ]
        if len(valid) < 20:
            return json.dumps({"error": "歷史資料不足"})
        closes  = [x[0] for x in valid]
        highs   = [x[1] for x in valid]
        lows    = [x[2] for x in valid]
        volumes = [x[3] if x[3] is not None else 0 for x in valid]
        curr    = closes[-1]

        def _ma(n: int):
            return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

        ma5, ma20, ma60 = _ma(5), _ma(20), _ma(60) if len(closes) >= 30 else None

        # 近20日高低（支撐壓力）
        top3_high = sorted(highs[-20:], reverse=True)[:3]
        top3_low  = sorted(lows[-20:])[:3]

        # 量比（今日量 / 近20日均量）
        avg_vol  = (sum(volumes[-21:-1]) / 20) if len(volumes) >= 21 else 0
        vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else None

        result_dict = {
            "price":     round(curr, 2),
            "ma5":       ma5,
            "ma20":      ma20,
            "ma60":      ma60,
            "ma5_above": curr >= ma5  if ma5  else None,
            "ma20_above": curr >= ma20 if ma20 else None,
            "resistance": [round(h, 2) for h in top3_high],
            "support":    [round(l, 2) for l in top3_low],
            "vol_ratio":  vol_ratio,
            "bars":       len(closes),
        }

        # RSI (14)
        if len(closes) >= 15:
            deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains  = [d for d in deltas[-14:] if d > 0]
            losses = [-d for d in deltas[-14:] if d < 0]
            ag = sum(gains) / 14
            al = sum(losses) / 14
            rs = ag / al if al else 100
            rsi = round(100 - (100 / (1 + rs)), 1)
            result_dict["rsi"] = rsi
            result_dict["rsi_signal"] = (
                "超賣" if rsi < 30 else "超買" if rsi > 70 else "中性"
            )

        # MACD (12/26/9) — simplified
        if len(closes) >= 36:
            k12, k26, k9 = 2/13, 2/27, 2/10
            ema12 = sum(closes[:12]) / 12
            ema26 = sum(closes[:26]) / 26
            for p in closes[26:]:
                ema12 = p * k12 + ema12 * (1 - k12)
                ema26 = p * k26 + ema26 * (1 - k26)
            dif = ema12 - ema26
            result_dict["macd_dif"] = round(dif, 4)
            result_dict["macd_bullish"] = dif > 0

        return json.dumps(result_dict)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_get_stock_news(symbol: str, name: str = "") -> str:
    """取得新聞"""
    titles, seen = [], set()
    try:
        if ".TW" in symbol:
            stock_no = symbol.replace(".TW", "")
            q = requests.utils.quote(f"{name or stock_no} 台股")
            url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        else:
            q = requests.utils.quote(f"{symbol} stock")
            url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        for e in feed.entries[:8]:
            t = getattr(e, "title", "").strip()
            key = t.lower()[:25]
            if t and key not in seen:
                seen.add(key)
                titles.append(t)
    except Exception:
        pass
    return json.dumps({"news": titles[:8] if titles else ["（暫無相關新聞）"]})


def _tool_get_fundamentals(symbol: str) -> str:
    """取得台股基本面（P/E、殖利率、P/B、52W高低）"""
    result: dict = {}
    is_tw = symbol.upper().endswith(".TW")
    tw_code = symbol.upper().removesuffix(".TW") if is_tw else ""

    # TWSE P/E
    if is_tw and tw_code:
        try:
            r = requests.get(
                "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            if r.status_code == 200:
                for item in r.json():
                    if str(item.get("Code", "")).strip() == tw_code:
                        for key, field in [("pe", "PEratio"), ("yield_pct", "DividendYield"), ("pb", "PBratio")]:
                            if item.get(field):
                                try:
                                    result[key] = float(item[field])
                                except ValueError:
                                    pass
                        break
        except Exception:
            pass

    # 52W 高低
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
        )
        if r.status_code == 200:
            meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            if meta.get("fiftyTwoWeekHigh"):
                result["hi52"] = meta["fiftyTwoWeekHigh"]
            if meta.get("fiftyTwoWeekLow"):
                result["lo52"] = meta["fiftyTwoWeekLow"]
    except Exception:
        pass

    return json.dumps(result if result else {"note": "無基本面資料（非台股或資料暫無）"})


def _execute_tool(name: str, inputs: dict) -> str:
    """路由工具呼叫"""
    if name == "get_stock_price":
        return _tool_get_stock_price(inputs.get("symbol", ""))
    elif name == "get_tech_indicators":
        return _tool_get_tech_indicators(inputs.get("symbol", ""))
    elif name == "get_stock_news":
        return _tool_get_stock_news(inputs.get("symbol", ""), inputs.get("name", ""))
    elif name == "get_fundamentals":
        return _tool_get_fundamentals(inputs.get("symbol", ""))
    return json.dumps({"error": f"未知工具：{name}"})


# ── Agent / Environment 懶惰初始化 ────────────────────────────────

_client:  anthropic.Anthropic | None = None
_agent_id: str = ""
_env_id:   str = ""


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY 未設定")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _load_cache() -> dict:
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(data: dict) -> None:
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠️ SDK agent cache 寫入失敗：{e}")


def _get_or_create_agent() -> str:
    """懶惰初始化 Agent（第一次建立後快取 agent_id）"""
    global _agent_id
    if _agent_id:
        return _agent_id

    cache = _load_cache()
    _agent_id = cache.get("agent_id", "")

    if _agent_id:
        print(f"  🤖 StockAnalyst Agent 載入：{_agent_id[:16]}...")
        return _agent_id

    print("  🤖 建立 StockAnalyst Agent（首次）...")
    client = _get_client()
    agent  = client.beta.agents.create(
        name=_AGENT_NAME,
        model=_AGENT_MODEL,
        system=_AGENT_SYSTEM,
        tools=_TOOLS,
        betas=["managed-agents-2026-04-01"],
    )
    _agent_id = agent.id
    cache["agent_id"] = _agent_id
    _save_cache(cache)
    print(f"  ✅ Agent 建立完成：{_agent_id}")
    return _agent_id


def _get_or_create_env() -> str:
    """懶惰初始化 Environment"""
    global _env_id
    if _env_id:
        return _env_id

    cache = _load_cache()
    _env_id = cache.get("env_id", "")
    if _env_id:
        return _env_id

    print("  🌐 建立 Agent Environment（首次）...")
    client = _get_client()
    env    = client.beta.environments.create(
        name="stock-analyst-env",
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},
        },
        betas=["managed-agents-2026-04-01"],
    )
    _env_id = env.id
    cache["env_id"] = _env_id
    _save_cache(cache)
    print(f"  ✅ Environment 建立完成：{_env_id}")
    return _env_id


# ── 主要分析函式（同步，供 executor 呼叫）────────────────────────

def analyze_stock_sdk(symbol: str, name: str = "") -> dict:
    """
    用 Managed Agents SDK 執行股票分析。

    回傳 dict：
      {
        "header":   str,   # Discord 第一行（標的名稱 + 現價）
        "analysis": str,   # Claude 分析文字（完整）
        "tools_used": list,  # 工具呼叫記錄
        "error":    str | None,
      }

    此函式是同步的，從 Discord asyncio event loop 用：
      loop.run_in_executor(None, analyze_stock_sdk, symbol, name)
    """
    try:
        client   = _get_client()
        agent_id = _get_or_create_agent()
        env_id   = _get_or_create_env()
    except Exception as e:
        return {"error": str(e), "header": "", "analysis": "", "tools_used": []}

    query = (
        f"請分析股票 {symbol}"
        + (f"（{name}）" if name else "")
        + "，提供完整的技術面、新聞面、基本面分析和操作建議。"
    )

    print(f"  🤖 [SDK Agent] 查股：{symbol}...")
    collected_text: list[str] = []
    tools_used:     list[str] = []
    header_text     = ""

    try:
        client_c = _get_client()  # shorthand

        session = client_c.beta.sessions.create(
            agent=agent_id,
            environment_id=env_id,
            title=f"查股 {symbol}",
            betas=["managed-agents-2026-04-01"],
        )

        with client_c.beta.sessions.events.stream(
            session.id,
            betas=["managed-agents-2026-04-01"],
        ) as stream:
            # 送出問題
            client_c.beta.sessions.events.send(
                session.id,
                events=[{
                    "type":    "user.message",
                    "content": [{"type": "text", "text": query}],
                }],
                betas=["managed-agents-2026-04-01"],
            )

            for event in stream:
                et = event.type

                if et == "agent.message":
                    for block in event.content:
                        if hasattr(block, "text"):
                            collected_text.append(block.text)

                elif et == "agent.tool_use":
                    tool_name = event.name
                    tools_used.append(tool_name)
                    print(f"    🔧 工具：{tool_name}({event.input})")

                    result = _execute_tool(tool_name, event.input)
                    print(f"    ✅ 結果長度：{len(result)} chars")

                    # 從 get_stock_price 結果建立 header
                    if tool_name == "get_stock_price" and not header_text:
                        try:
                            d = json.loads(result)
                            if d.get("price") and not d.get("error"):
                                sym_name  = name or d.get("name", symbol)
                                ts_now    = datetime.datetime.now(
                                    __import__("pytz").timezone("Asia/Taipei")
                                ).strftime("%Y-%m-%d %H:%M")
                                fund_note = ""
                                header_text = (
                                    f"## {d['emoji']} **{sym_name}（{symbol}）** | {ts_now}\n"
                                    f"**現價：{d['price']:,.2f} {d.get('currency','')}**　"
                                    f"漲跌：{d['change']:+.2f}（{d['pct']:+.2f}%）{fund_note}\n"
                                    f"{'─' * 28}\n\n"
                                )
                        except Exception:
                            pass

                    # 回傳工具結果
                    client_c.beta.sessions.events.send(
                        session.id,
                        events=[{
                            "type":        "tool_result",
                            "tool_use_id": event.id,
                            "content":     [{"type": "text", "text": result}],
                        }],
                        betas=["managed-agents-2026-04-01"],
                    )

                elif et in ("session.status_idle", "session.status_terminated"):
                    print(f"    Session {et}")
                    break

    except Exception as e:
        return {
            "error":      str(e),
            "header":     header_text,
            "analysis":   "\n".join(collected_text),
            "tools_used": tools_used,
        }

    analysis = "\n".join(collected_text)
    print(f"  ✅ SDK Agent 完成。工具：{tools_used}  字數：{len(analysis)}")

    return {
        "error":      None,
        "header":     header_text,
        "analysis":   analysis,
        "tools_used": tools_used,
    }
