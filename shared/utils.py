"""
shared/utils.py — 共用工具函式
時段判斷、格式化、Discord 傳送
"""

import datetime
import re
import time
import requests
from shared.config import (
    TAIPEI_TZ, TW_TZ, MAX_EMBED_FIELD, MAX_CONTENT
)


# ── 時間工具 ──────────────────────────────────────────────────────

def now_tw() -> datetime.datetime:
    return datetime.datetime.now(TAIPEI_TZ)

def now_str() -> str:
    return now_tw().strftime("%Y-%m-%d %H:%M")

def is_weekend() -> bool:
    return now_tw().weekday() >= 5  # 5=Saturday, 6=Sunday

def get_session_info() -> dict:
    """
    依 REPORT_SESSION 環境變數（morning/afternoon/evening）決定時段；
    未設定時才依台灣時間自動偵測。
    GitHub Actions cron 在 07:55 觸發，若靠時間自動偵測會誤判，
    因此正式 workflow 應明確傳入 REPORT_SESSION。
    """
    import os
    _sessions = {
        "morning": {
            "label": "盤前早報",
            "period": "22:00 ～ 08:00（隔夜）",
            "emoji": "🌅",
            "start_h": 22,
            "end_h": 8,
        },
        "afternoon": {
            "label": "盤中午報",
            "period": "08:00 ～ 14:00",
            "emoji": "☀️",
            "start_h": 8,
            "end_h": 14,
        },
        "evening": {
            "label": "盤後晚報",
            "period": "14:00 ～ 22:00",
            "emoji": "🌙",
            "start_h": 14,
            "end_h": 22,
        },
    }
    env_session = os.environ.get("REPORT_SESSION", "auto").lower()
    if env_session in _sessions:
        print(f"  ⏰ 時段（env）：{_sessions[env_session]['label']}")
        return _sessions[env_session]
    # 自動偵測（本機測試 / 手動觸發）
    h = now_tw().hour
    if 8 <= h < 14:
        return _sessions["morning"]
    elif 14 <= h < 22:
        return _sessions["afternoon"]
    else:
        return _sessions["evening"]


# ── 格式化工具 ────────────────────────────────────────────────────

def fmt_quote(q: dict) -> str:
    # 週末時僅用 footer 統一說明，不在每筆旁邊加 ⚠️
    stale_tag = " 📅" if q.get("stale") else ""
    return f"{q['emoji']} {q['price']} ({q['pct']}){stale_tag}"

def truncate(text: str, limit: int = MAX_EMBED_FIELD) -> str:
    if not text or not text.strip():
        return "暫無資料"
    text = text.strip()
    return text[:limit - 3] + "..." if len(text) > limit else text

def build_news_text(articles: list, limit: int = 12) -> str:
    lines = []
    for i, a in enumerate(articles[:limit], 1):
        title = a["title"]
        summary = a.get("summary", "")
        pub = a["pub_dt"].strftime("%m/%d %H:%M") if a.get("pub_dt") else ""
        line = f"{i}. [{pub}] {title}"
        if summary:
            line += f"\n   摘要：{summary[:100]}"
        lines.append(line)
    return "\n".join(lines) if lines else "（本時段暫無新聞）"

def build_news_links(articles: list, limit: int = 8) -> str:
    if not articles:
        return "暫無新聞"
    lines = []
    for i, a in enumerate(articles[:limit], 1):
        title = a["title"][:50]
        url = a.get("url", "#")
        pub = a["pub_dt"].strftime("%H:%M") if a.get("pub_dt") else ""
        time_tag = f"`{pub}` " if pub else ""
        lines.append(f"{i}. {time_tag}[{title}]({url})")
    return "\n".join(lines)

def build_market_table(market_data: dict) -> str:
    rows = []
    mapping = [
        ("twii",  "🇹🇼 台灣加權"),
        ("dji",   "🇺🇸 道瓊"),
        ("ixic",  "📊 納斯達克"),
        ("gspc",  "📈 S&P 500"),
        ("vix",   "😱 VIX"),
        ("us10y", "🏦 美債10Y"),
        ("dxy",   "💵 美元指數"),
        ("gold",  "🥇 黃金"),
        ("oil",   "🛢️ 原油(WTI)"),
    ]
    for key, label in mapping:
        q = market_data.get(key, {})
        if q and q.get("price") != "N/A":
            # 週末數據改在 footer 統一說明，個別項目不加 ⚠️ 避免誤解
            rows.append(f"{label}: {q['emoji']} **{q['price']}** ({q['pct']})")
    return "\n".join(rows) if rows else "暫無數據"


# ── Discord 傳送 ──────────────────────────────────────────────────

def send_discord_message(webhook_url: str, content: str) -> bool:
    if not content or not content.strip():
        return False
    # 分割長訊息：依換行符切割，單行超長時強制切
    chunks, current = [], ""
    for line in content.split("\n"):
        # 單行本身就超過限制 → 強制每 MAX_CONTENT 字切一刀
        while len(line) > MAX_CONTENT:
            piece = line[:MAX_CONTENT]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece)
            line = line[MAX_CONTENT:]
        # 正常行：累積到 current，超出時先存再重新開始
        if len(current) + len(line) + 1 > MAX_CONTENT:
            if current:
                chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)

    success = True
    for chunk in chunks:
        for attempt in range(3):   # 最多重試 3 次（處理 429 速率限制）
            try:
                r = requests.post(webhook_url, json={"content": chunk}, timeout=15)
                if r.status_code == 429:
                    wait = float(r.json().get("retry_after", 5))
                    print(f"⏳ Discord 速率限制，等待 {wait:.1f}s 後重試...")
                    time.sleep(wait + 0.5)
                    continue
                if r.status_code not in [200, 204]:
                    print(f"❌ Discord 傳送失敗：{r.status_code} {r.text[:200]}")
                    success = False
                else:
                    print(f"✅ Discord 傳送成功（{len(chunk)} 字）")
                break
            except Exception as e:
                print(f"❌ Discord 傳送錯誤：{e}")
                success = False
                break
        time.sleep(1.5)   # 每段間隔加大，降低觸發速率限制機率
    return success

def send_embed(webhook_url: str, embed: dict) -> bool:
    for field in embed.get("fields", []):
        field["value"] = truncate(field.get("value", ""), MAX_EMBED_FIELD)
    try:
        r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
        if r.status_code in [200, 204]:
            print(f"✅ Embed 發送成功：{embed.get('title', '')[:40]}")
            return True
        else:
            print(f"❌ Embed 發送失敗：{r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        print(f"❌ Embed 發送錯誤：{e}")
        return False
