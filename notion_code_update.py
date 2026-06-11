"""
notion_code_update.py — GitHub 程式碼變更 → Notion 同步
每次 push 後由 GitHub Actions 執行，更新 Notion 程式碼總覽頁面
"""

import os
import sys
import requests
from datetime import datetime, timezone, timedelta

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_VER   = "2022-06-28"
BASE_URL     = "https://api.notion.com/v1"

# 主總覽頁 ID
OVERVIEW_PAGE_ID = "37c613f1f3f08106ae21f30e7acab130"

# 各模組頁面 ID（對應到 GitHub 路徑關鍵字）
MODULE_PAGES = {
    "agents":           "37c613f1f3f081b899acede15563f116",
    "shared":           "37c613f1f3f081fa8ba5f53eeaf5285c",
    "strategy_agent":   "37c613f1f3f0814d8229f57d482055fd",
    "股市日報":          "37c613f1f3f0818b9fc1d279afe3cf00",
    "自選股":            "37c613f1f3f08188bb74cd64e1f541df",
    "Podcast":           "37c613f1f3f08152bbc5e848f15bac35",
    "warrant":           "37c613f1f3f0818ea2f7cc85a293b653",
    "main.py":           "37c613f1f3f081738f1af0f65cae50e5",
    "orchestrator.py":   "37c613f1f3f081738f1af0f65cae50e5",
    "technical_ind":     "37c613f1f3f081738f1af0f65cae50e5",
    "weekly_review":     "37c613f1f3f081738f1af0f65cae50e5",
    "watchlist_db":      "37c613f1f3f081738f1af0f65cae50e5",
}

TW = timezone(timedelta(hours=8))

def h():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type":  "application/json",
        "Notion-Version": NOTION_VER,
    }

def now_tw():
    return datetime.now(TW).strftime("%Y/%m/%d %H:%M")

def append_block(page_id: str, block: dict):
    requests.patch(f"{BASE_URL}/blocks/{page_id}/children",
                   json={"children": [block]}, headers=h())

def divider(page_id: str):
    append_block(page_id, {"type": "divider", "divider": {}})

def paragraph(page_id: str, text: str, bold: bool = False):
    append_block(page_id, {
        "type": "paragraph",
        "paragraph": {"rich_text": [{
            "type": "text",
            "text": {"content": text},
            "annotations": {"bold": bold}
        }]}
    })

def callout(page_id: str, text: str, emoji: str = "📝"):
    append_block(page_id, {
        "type": "callout",
        "callout": {
            "icon": {"emoji": emoji},
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }
    })

def bullet(page_id: str, text: str):
    append_block(page_id, {
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text}}]}
    })

def detect_module(filepath: str) -> str:
    for key in MODULE_PAGES:
        if key in filepath:
            return key
    return ""

def main():
    if not NOTION_TOKEN:
        print("❌ NOTION_TOKEN 未設定")
        sys.exit(1)

    # 從環境變數取得 GitHub Actions 傳入的資訊
    commit_sha     = os.getenv("GITHUB_SHA", "unknown")[:7]
    commit_message = os.getenv("COMMIT_MESSAGE", "（無說明）").strip()
    changed_files  = os.getenv("CHANGED_FILES", "").strip()
    repo           = os.getenv("GITHUB_REPOSITORY", "Kuan-1113/stock-news")
    actor          = os.getenv("GITHUB_ACTOR", "unknown")
    ts             = now_tw()

    files_list = [f for f in changed_files.split("\n") if f.strip()] if changed_files else []
    print(f"[Notion] 同步 commit {commit_sha}，共 {len(files_list)} 個檔案異動")

    # ── 1. 在主總覽頁加入更新記錄 ────────────────────────────────
    divider(OVERVIEW_PAGE_ID)
    callout(OVERVIEW_PAGE_ID,
            f"🔄 最後更新：{ts}  |  {actor}  |  {commit_sha}  |  {commit_message}",
            "🔄")
    if files_list:
        for f in files_list[:15]:
            bullet(OVERVIEW_PAGE_ID, f)
        if len(files_list) > 15:
            bullet(OVERVIEW_PAGE_ID, f"... 共 {len(files_list)} 個檔案")

    # ── 2. 在對應模組頁加入「最近修改」標記 ───────────────────────
    updated_modules = set()
    for filepath in files_list:
        module = detect_module(filepath)
        if module and module not in updated_modules:
            updated_modules.add(module)
            page_id = MODULE_PAGES[module]
            callout(page_id,
                    f"📝 {ts}  |  {commit_sha}  |  {commit_message}",
                    "📝")
            for f in files_list:
                if detect_module(f) == module:
                    bullet(page_id, f"修改：{f}")

    print(f"[Notion] ✅ 同步完成，更新模組：{updated_modules or '（主頁）'}")

if __name__ == "__main__":
    main()
