"""
agents/news_agent.py — 新聞收集 Agent
TW / US / Global 三類別共 13 個 RSS feed 並行抓取
原本順序執行 ~3 分鐘 → 並行後 ~20 秒
"""

import datetime
import re
import requests
import feedparser
from concurrent.futures import ThreadPoolExecutor, as_completed

from shared.config import RSS_FEEDS, TAIPEI_TZ
from shared.utils import now_tw


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
    """依時段過濾新聞，保留該時段內的文章"""
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


# ── Agent ─────────────────────────────────────────────────────────

class NewsAgent:
    """
    並行抓取 TW / US / Global 三類別 RSS
    回傳 {"tw": [...], "us": [...], "global": [...]}
    """

    def run(self, session_info: dict) -> dict:
        print("📰 [NewsAgent] 並行抓取新聞...")
        news_data: dict = {}

        # 同時送出全部 13 個 RSS 請求，並追蹤每個屬於哪個類別
        with ThreadPoolExecutor(max_workers=15) as executor:
            future_map: dict = {}  # future → (category, url)
            for category, feeds in RSS_FEEDS.items():
                for url in feeds:
                    f = executor.submit(fetch_rss, url, 15)
                    future_map[f] = category

            # 按類別彙整結果
            category_articles: dict = {"tw": [], "us": [], "global": []}
            category_seen:     dict = {"tw": set(), "us": set(), "global": set()}

            for future in as_completed(future_map):
                category = future_map[future]
                try:
                    articles = future.result()
                    for a in articles:
                        key = re.sub(r"\s+", "", a["title"].lower())[:30]
                        if key not in category_seen[category]:
                            category_seen[category].add(key)
                            category_articles[category].append(a)
                except Exception as e:
                    print(f"  RSS 彙整錯誤：{e}")

        # 過濾時段並整理
        for category, articles in category_articles.items():
            news_data[category] = _filter_by_session(articles, session_info)
            print(f"  ✅ {category.upper()}：{len(news_data[category])} 篇")

        print(f"📰 [NewsAgent] 完成")
        return news_data
