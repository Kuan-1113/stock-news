"""
agents/news_agent.py — 新聞收集 Agent
TW / US / Global RSS + 金十數據(Jin10) + AI新聞 + VIP新聞 並行抓取

新增（對應 stock_daily.py 功能）：
  - Jin10 MCP：即時快訊（list_flash）+ 財經行事曆（list_calendar）
  - AI 全球動態：RSS_AI_FEEDS × 4 個來源
  - VIP 新聞：馬斯克 / 川普 / 黃仁勳 最新動態
"""

import os
import json
import datetime
import re
import requests
import feedparser
from concurrent.futures import ThreadPoolExecutor, as_completed

from shared.config import RSS_FEEDS, TAIPEI_TZ
from shared.utils import now_tw

# ── Jin10 設定 ────────────────────────────────────────────────────
JIN10_TOKEN = os.environ.get("JIN10_TOKEN", "")
_jin10_session_id: str = ""

# ── AI 新聞 RSS ───────────────────────────────────────────────────
RSS_AI_FEEDS = [
    "https://news.google.com/rss/search?q=AI+artificial+intelligence+AGI+breakthrough&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=OpenAI+Anthropic+Google+AI+agent&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=AI+robot+LLM+invention+2025&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.arstechnica.com/arstechnica/index",
]

# ── VIP 人物新聞 ──────────────────────────────────────────────────
RSS_VIP_FEEDS = {
    "musk":   "https://news.google.com/rss/search?q=Elon+Musk+Tesla+SpaceX+X&hl=en-US&gl=US&ceid=US:en",
    "trump":  "https://news.google.com/rss/search?q=Donald+Trump+tariff+economy+market&hl=en-US&gl=US&ceid=US:en",
    "jensen": "https://news.google.com/rss/search?q=Jensen+Huang+NVIDIA+AI+chip&hl=en-US&gl=US&ceid=US:en",
}


# ── RSS 工具 ──────────────────────────────────────────────────────

def parse_rss_date(entry) -> datetime.datetime | None:
    for attr in ["published_parsed", "updated_parsed"]:
        t = getattr(entry, attr, None)
        if t:
            try:
                dt = datetime.datetime(*t[:6], tzinfo=datetime.timezone.utc)
                return dt.astimezone(TAIPEI_TZ)
            except Exception:
                pass
    return None

def fetch_rss(url: str, limit: int = 15) -> list:
    """抓取單一 RSS feed"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        }
        feed = feedparser.parse(url, request_headers=headers)
        articles = []
        for entry in feed.entries[:limit]:
            title   = getattr(entry, "title", "").strip()
            link    = getattr(entry, "link", "#").strip()
            summary = getattr(entry, "summary", "").strip()
            summary = re.sub(r"<[^>]+>", "", summary)[:200]
            pub_dt  = parse_rss_date(entry)
            if title and len(title) > 5:
                articles.append({
                    "title":   title,
                    "url":     link,
                    "summary": summary,
                    "pub_dt":  pub_dt,
                })
        return articles
    except Exception as e:
        print(f"  RSS 錯誤 {url[:60]}：{e}")
        return []

def _filter_by_session(articles: list, session_info: dict) -> list:
    """依時段過濾新聞"""
    now = now_tw()
    start_h = session_info["start_h"]
    end_h   = session_info["end_h"]

    if start_h < end_h:
        start_dt = now.replace(hour=start_h, minute=0, second=0, microsecond=0)
        end_dt   = now.replace(hour=end_h,   minute=0, second=0, microsecond=0)
    else:
        yesterday = now - datetime.timedelta(days=1)
        start_dt  = yesterday.replace(hour=start_h, minute=0, second=0, microsecond=0)
        end_dt    = now.replace(hour=end_h, minute=0, second=0, microsecond=0)

    filtered, no_time = [], []
    for a in articles:
        if a["pub_dt"] is None:
            no_time.append(a)
        elif start_dt <= a["pub_dt"] <= end_dt:
            filtered.append(a)

    result = filtered + no_time
    result.sort(
        key=lambda x: x["pub_dt"] or datetime.datetime.min.replace(tzinfo=TAIPEI_TZ),
        reverse=True,
    )
    return result[:15]


# ── Jin10 MCP API ─────────────────────────────────────────────────

def _jin10_post(payload: dict, session_id: str = "") -> tuple:
    """返回 (result_dict, new_session_id)"""
    headers = {"Content-Type": "application/json"}
    if JIN10_TOKEN:
        headers["Authorization"] = f"Bearer {JIN10_TOKEN}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    try:
        r = requests.post(
            "https://mcp.jin10.com/mcp",
            headers=headers,
            json=payload,
            timeout=20,
        )
        new_sid = r.headers.get("Mcp-Session-Id", session_id)
        # 解析 SSE 格式（data: {...} 行）
        for line in r.content.decode("utf-8").splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                    if "result" in obj:
                        return obj["result"], new_sid
                except Exception:
                    pass
        try:
            return r.json().get("result", {}), new_sid
        except Exception:
            return {}, new_sid
    except Exception as e:
        print(f"  金十 API 錯誤：{e}")
        return {}, session_id


def jin10_initialize() -> str:
    global _jin10_session_id
    if not JIN10_TOKEN:
        return ""
    _, new_sid = _jin10_post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "clientInfo": {"name": "stock-bot", "version": "1.0"},
        }
    })
    _jin10_session_id = new_sid
    if _jin10_session_id:
        print(f"  金十 Session：{_jin10_session_id[:16]}...")
    else:
        print("  金十 Session：未取得")
    return _jin10_session_id


def jin10_call(tool: str, args: dict) -> list:
    global _jin10_session_id
    if not JIN10_TOKEN:
        return []
    result, _ = _jin10_post({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": args}
    }, session_id=_jin10_session_id)
    try:
        content_list = result.get("content", [])
        if result.get("isError") and content_list:
            print(f"  金十工具錯誤（{tool}）：{content_list[0].get('text','')[:120]}")
            return []
        text   = content_list[0].get("text", "") if content_list else ""
        parsed = json.loads(text)
        data   = parsed.get("data", [])
        if isinstance(data, dict):
            return data.get("items", [])
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        print(f"  金十解析錯誤（{tool}）：{e}")
        return []


def fetch_jin10_flash() -> list:
    """取得近24小時金十國際即時快訊"""
    if not JIN10_TOKEN:
        return []
    try:
        items  = jin10_call("list_flash", {})
        if not items:
            print("  金十快訊：無資料")
            return []
        now    = now_tw()
        cutoff = now - datetime.timedelta(hours=24)
        result = []
        for item in items:
            content  = item.get("content") or ""
            time_raw = item.get("time") or ""
            if not content:
                continue
            try:
                pub_dt = datetime.datetime.fromisoformat(
                    time_raw.replace("Z", "+00:00")
                ).astimezone(TAIPEI_TZ)
            except Exception:
                pub_dt = None
            if pub_dt and pub_dt < cutoff:
                continue
            result.append({
                "time":   pub_dt.strftime("%m/%d %H:%M") if pub_dt else "",
                "title":  content[:150],
                "pub_dt": pub_dt,
            })
        result.sort(
            key=lambda x: x.get("pub_dt") or datetime.datetime.min.replace(tzinfo=TAIPEI_TZ),
            reverse=True,
        )
        clean = [{k: v for k, v in r.items() if k != "pub_dt"} for r in result[:25]]
        print(f"  金十快訊：{len(clean)} 則（近24h）")
        return clean
    except Exception as e:
        print(f"  金十快訊錯誤：{e}")
        return []


def fetch_jin10_calendar() -> list:
    """取得今日重要財經事件行事曆（★★以上）"""
    if not JIN10_TOKEN:
        return []
    try:
        items  = jin10_call("list_calendar", {})
        result = []
        for item in items:
            star = int(item.get("star", 0) or 0)
            if star < 2:
                continue
            event    = item.get("title") or ""
            time_s   = item.get("pub_time") or item.get("time") or ""
            actual   = item.get("actual") or ""
            forecast = item.get("consensus") or ""
            affect   = item.get("affect_txt") or ""
            if event:
                result.append({
                    "event":    event[:60],
                    "time":     time_s,
                    "actual":   actual,
                    "forecast": forecast,
                    "affect":   affect,
                    "star":     star,
                })
        print(f"  金十行事曆：{len(result)} 筆（★★以上）")
        return result[:10]
    except Exception as e:
        print(f"  金十行事曆錯誤：{e}")
        return []


def build_jin10_text(flash: list, calendar: list) -> str:
    """格式化金十數據為 Claude prompt 文字"""
    lines = []
    if flash:
        lines.append("【金十數據 即時快訊】")
        for f in flash:
            t = f"[{f['time']}] " if f["time"] else ""
            lines.append(f"• {t}{f['title']}")
    if calendar:
        lines.append("\n【今日重要財經事件】")
        for c in calendar:
            stars        = "★" * min(c["star"], 3)
            actual_tag   = f"公布={c['actual']}" if c["actual"] else ""
            forecast_tag = f"預期={c['forecast']}" if c["forecast"] else ""
            affect_tag   = c.get("affect", "")
            vals = "　".join(t for t in [actual_tag, forecast_tag, affect_tag] if t)
            lines.append(
                f"{stars} [{c['time']}] {c['event']}"
                + (f"　{vals}" if vals else "")
            )
    return "\n".join(lines)


def build_jin10_discord_message(flash: list, calendar: list) -> str:
    """格式化金十數據為獨立 Discord 訊息"""
    parts = []
    if flash:
        lines = ["## 📡 金十數據 即時快訊"]
        for f in flash[:15]:   # 最多 15 則避免太長
            t = f"`{f['time']}` " if f["time"] else ""
            lines.append(f"• {t}{f['title']}")
        parts.append("\n".join(lines))
    if calendar:
        lines = ["## 📅 今日重要財經事件"]
        for c in calendar:
            stars = "⭐" * min(c["star"], 3)
            vals = "  ".join(v for v in [
                (f"公布 `{c['actual']}`"   if c["actual"]   else ""),
                (f"預期 `{c['forecast']}`" if c["forecast"] else ""),
                (c.get("affect", "")),
            ] if v)
            lines.append(
                f"{stars} **`{c['time']}`** {c['event']}"
                + (f" — {vals}" if vals else "")
            )
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else ""


# ── AI 新聞 ────────────────────────────────────────────────────────

def fetch_ai_news() -> list:
    """抓取 AI & AI Agent 全球最新動態（並行）"""
    all_articles, seen_titles = [], set()

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_rss, url, 10) for url in RSS_AI_FEEDS]
        for f in as_completed(futures):
            for a in (f.result() or []):
                key = re.sub(r"\s+", "", a["title"].lower())[:30]
                if key not in seen_titles:
                    seen_titles.add(key)
                    all_articles.append(a)

    ai_keywords = [
        "ai", "artificial intelligence", "llm", "gpt", "claude", "gemini", "agent",
        "robot", "openai", "deepmind", "anthropic", "chatgpt", "copilot",
        "inference", "foundation model", "generative", "nvidia", "automation",
    ]
    relevant = [a for a in all_articles if any(kw in a["title"].lower() for kw in ai_keywords)]
    other    = [a for a in all_articles if a not in relevant]
    result   = relevant + other
    result.sort(
        key=lambda x: x["pub_dt"] or datetime.datetime.min.replace(tzinfo=TAIPEI_TZ),
        reverse=True,
    )
    print(f"  AI新聞：{len(result)} 篇（相關 {len(relevant)} 篇）")
    return result[:12]


# ── VIP 人物新聞 ───────────────────────────────────────────────────

def fetch_vip_news() -> dict:
    """並行抓取馬斯克 / 川普 / 黃仁勳 最新動態"""
    result: dict = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fetch_rss, url, 5): key
                   for key, url in RSS_VIP_FEEDS.items()}
        for f in as_completed(futures):
            key = futures[f]
            result[key] = (f.result() or [])[:3]
    print(f"  VIP新聞：Musk {len(result.get('musk',[]))} / Trump {len(result.get('trump',[]))} / Jensen {len(result.get('jensen',[]))}")
    return result


# ── Agent ─────────────────────────────────────────────────────────

class NewsAgent:
    """
    並行抓取所有新聞來源，回傳完整 news_data
    回傳格式：
    {
      "tw": [...],             ← 台股 RSS
      "us": [...],             ← 美股 RSS
      "global": [...],         ← 國際 RSS
      "ai": [...],             ← AI 全球動態
      "vip": {...},            ← 馬斯克/川普/黃仁勳
      "jin10_flash": [...],    ← 金十即時快訊
      "jin10_calendar": [...], ← 金十財經行事曆
    }
    """

    def run(self, session_info: dict) -> dict:
        print("📰 [NewsAgent] 並行抓取新聞...")

        # Jin10 需要先取得 Session ID 才能呼叫工具
        # （initialize 很快，不影響整體速度）
        if JIN10_TOKEN:
            jin10_initialize()

        # ── 全部並行送出 ─────────────────────────────────────────────
        with ThreadPoolExecutor(max_workers=25) as executor:
            # RSS feeds（主新聞）
            rss_futures: dict = {}
            for category, feeds in RSS_FEEDS.items():
                for url in feeds:
                    f = executor.submit(fetch_rss, url, 15)
                    rss_futures[f] = category

            # Jin10（flash + calendar 分開，互不阻塞）
            jin10_flash_fut    = executor.submit(fetch_jin10_flash)
            jin10_cal_fut      = executor.submit(fetch_jin10_calendar)

            # AI 新聞
            ai_fut  = executor.submit(fetch_ai_news)

            # VIP 人物新聞
            vip_fut = executor.submit(fetch_vip_news)

            # ── 彙整 RSS ──────────────────────────────────────────────
            category_articles: dict = {"tw": [], "us": [], "global": []}
            category_seen:     dict = {"tw": set(), "us": set(), "global": set()}

            for future in as_completed(rss_futures):
                cat = rss_futures[future]
                try:
                    for a in (future.result() or []):
                        key = re.sub(r"\s+", "", a["title"].lower())[:30]
                        if key not in category_seen[cat]:
                            category_seen[cat].add(key)
                            category_articles[cat].append(a)
                except Exception as e:
                    print(f"  RSS 彙整錯誤：{e}")

            jin10_flash    = jin10_flash_fut.result()
            jin10_calendar = jin10_cal_fut.result()
            ai_news        = ai_fut.result()
            vip_news       = vip_fut.result()

        # ── 時段過濾 ──────────────────────────────────────────────────
        news_data: dict = {}
        for cat, articles in category_articles.items():
            news_data[cat] = _filter_by_session(articles, session_info)
            print(f"  ✅ {cat.upper()}：{len(news_data[cat])} 篇")

        news_data["ai"]             = ai_news
        news_data["vip"]            = vip_news
        news_data["jin10_flash"]    = jin10_flash
        news_data["jin10_calendar"] = jin10_calendar

        has_jin10 = bool(jin10_flash or jin10_calendar)
        print(f"📰 [NewsAgent] 完成 | 金十{'✅' if has_jin10 else '❌（無 Token）'} | AI {len(ai_news)} 篇")
        return news_data
