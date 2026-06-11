"""
notion_daily_sync.py — 股市日報 → Notion 同步模組

架構：📰 日報 → 🇹🇼/🇺🇸/🌍 市場 → 📅 日期 → ☀️/🌤️/🌙 早午晚報

在 stock_daily.py 中，Discord 發送完後呼叫：
    sync_report("tw",     session_key, tw_analysis)
    sync_report("us",     session_key, us_analysis)
    sync_report("global", session_key, global_analysis)

session_key 傳 "morning" / "afternoon" / "evening"
（或傳 None 讓本模組依台灣時間自動判斷）
"""

import os
import requests
from datetime import datetime
import pytz

NOTION_TOKEN  = os.getenv("NOTION_TOKEN", "")
NOTION_VER    = "2022-06-28"
BASE_URL      = "https://api.notion.com/v1"

# ── 已建立的 Notion 市場父頁面 ID ──────────────────────────────
MARKET_PARENTS = {
    "tw":     "37c613f1f3f08199a0e3d73b2aaeff46",   # 🇹🇼 台股
    "us":     "37c613f1f3f0816d9407deadbbe54d60",   # 🇺🇸 美股
    "global": "37c613f1f3f08166927bed1017e50bd1",   # 🌍 國際
}

SESSION_LABELS = {
    "morning":   "☀️ 早報",
    "afternoon": "🌤️ 午報",
    "evening":   "🌙 晚報",
}

# ── 內部工具函數 ───────────────────────────────────────────────

def _h():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type":  "application/json",
        "Notion-Version": NOTION_VER,
    }

def _taipei_date():
    return datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y/%m/%d")

def _detect_session():
    hour = datetime.now(pytz.timezone("Asia/Taipei")).hour
    if 6 <= hour < 12:
        return "morning"
    elif 12 <= hour < 19:
        return "afternoon"
    return "evening"

def _find_child(parent_id: str, title: str):
    """在父頁面的子頁面中找符合 title 的頁面 ID，找不到回傳 None"""
    url = f"{BASE_URL}/blocks/{parent_id}/children?page_size=100"
    r = requests.get(url, headers=_h())
    if r.status_code != 200:
        return None
    for block in r.json().get("results", []):
        if block.get("type") == "child_page":
            if block["child_page"]["title"] == title:
                return block["id"]
    return None

def _create_page(parent_id: str, title: str, icon: str = None) -> str:
    payload = {
        "parent": {"page_id": parent_id},
        "properties": {
            "title": {"title": [{"text": {"content": title}}]}
        },
    }
    if icon:
        payload["icon"] = {"emoji": icon}
    r = requests.post(f"{BASE_URL}/pages", json=payload, headers=_h())
    r.raise_for_status()
    return r.json()["id"]

def _find_or_create(parent_id: str, title: str, icon: str = None) -> str:
    return _find_child(parent_id, title) or _create_page(parent_id, title, icon)

def _clear_page(page_id: str):
    """刪除頁面內所有現有 block（覆蓋前清空）"""
    url = f"{BASE_URL}/blocks/{page_id}/children?page_size=100"
    r = requests.get(url, headers=_h())
    if r.status_code != 200:
        return
    for block in r.json().get("results", []):
        requests.delete(f"{BASE_URL}/blocks/{block['id']}", headers=_h())

def _to_blocks(text: str) -> list:
    """將 Markdown 文字轉為 Notion blocks（最多 95 個）"""
    blocks = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("## "):
            blocks.append({"type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": s[3:]}}]}})
        elif s.startswith("### "):
            blocks.append({"type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": s[4:]}}]}})
        elif s.startswith(("• ", "- ", "* ")):
            blocks.append({"type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": s[2:]}}]}})
        else:
            blocks.append({"type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": s.replace("**", "")}}]}})
        if len(blocks) >= 95:
            break
    return blocks

def _append_blocks(page_id: str, blocks: list):
    if not blocks:
        return
    r = requests.patch(f"{BASE_URL}/blocks/{page_id}/children",
                       json={"children": blocks}, headers=_h())
    r.raise_for_status()

# ── 主要呼叫函數 ───────────────────────────────────────────────

def sync_report(market: str, session: str, content: str):
    """
    stock_daily.py 呼叫此函數同步到 Notion。

    Args:
        market  : "tw" / "us" / "global"
        session : "morning" / "afternoon" / "evening"（或 None 自動偵測）
        content : Claude 分析的完整文字
    """
    if not NOTION_TOKEN:
        print("[Notion] ⚠️  NOTION_TOKEN 未設定，跳過")
        return
    if not content or not content.strip():
        print(f"[Notion] ⚠️  {market} 內容為空，跳過")
        return

    try:
        session       = session or _detect_session()
        session_label = SESSION_LABELS.get(session, "☀️ 早報")
        parent_id     = MARKET_PARENTS[market]
        date_str      = _taipei_date()

        date_page_id    = _find_or_create(parent_id,      date_str,      "📅")
        session_page_id = _find_or_create(date_page_id,   session_label)

        _clear_page(session_page_id)
        _append_blocks(session_page_id, _to_blocks(content))

        print(f"[Notion] ✅ {date_str} {session_label} ({market}) 同步完成")

    except Exception as e:
        print(f"[Notion] ❌ 同步失敗 [{market}/{session}]: {e}")
