"""
agents/publisher_agent.py — Discord 發送 Agent
依序發送 TW / US / Global 三個頻道
（Discord 有 rate limit，保留 sleep 間隔）
"""

import time
from shared.config import (
    DISCORD_TW, DISCORD_US, DISCORD_GLOBAL
)
from shared.utils import (
    send_discord_message, send_embed, truncate,
    build_market_table, build_news_links, now_str, is_weekend
)


class PublisherAgent:
    """
    接收 market_data / news_data / analysis，依序發送到 Discord
    """

    def run(
        self,
        market_data: dict,
        news_data:   dict,
        analysis:    dict,
        session_info: dict,
    ) -> None:
        print("📤 [PublisherAgent] 發送到 Discord...")

        ts             = now_str()
        weekend_banner = "（週末版）" if is_weekend() else ""
        session_tag    = f"{session_info['emoji']} {session_info['label']} {weekend_banner}| {session_info['period']}"
        weekend_footer = "｜⚠️ 週末數據為上次交易日" if is_weekend() else ""

        # 共用資料
        market_table = build_market_table(market_data)
        crypto       = market_data.get("crypto", {})
        crypto_text  = "\n".join([
            f"{d['emoji']} **{sym}**：{d['price']} ({d['pct']})"
            for sym, d in crypto.items()
        ]) if crypto else "暫無數據"

        tw_links     = build_news_links(news_data.get("tw", []))
        us_links     = build_news_links(news_data.get("us", []))
        global_links = build_news_links(news_data.get("global", []))

        # ── 🇹🇼 台股頻道 ───────────────────────────────────────────
        send_embed(DISCORD_TW, {
            "title": f"🇹🇼 台股{session_info['label']} {weekend_banner}| {ts}",
            "description": f"**時段：** {session_info['period']}",
            "color": 0x2ECC71,
            "fields": [
                {"name": "📊 大盤指數", "value": truncate(market_table), "inline": False},
            ],
            "footer": {"text": f"資料來源：Yahoo Finance{weekend_footer}"},
        })
        time.sleep(1.2)

        send_discord_message(
            DISCORD_TW,
            f"## 🤖 Claude AI 台股分析 — {session_tag}\n\n{analysis['tw']}"
        )
        time.sleep(1.2)

        send_embed(DISCORD_TW, {
            "title": f"📰 台股新聞連結 | {ts}",
            "color": 0x27AE60,
            "fields": [
                {"name": "🔗 本時段重要新聞", "value": truncate(tw_links), "inline": False},
            ],
            "footer": {"text": "資料來源：Google News / 鉅亨網 / MoneyDJ"},
        })
        time.sleep(1.5)

        # ── 🇺🇸 美股頻道 ───────────────────────────────────────────
        dji  = market_data.get("dji",  {})
        ixic = market_data.get("ixic", {})
        gspc = market_data.get("gspc", {})

        def _stale(q: dict) -> str:
            return " ⚠️未更新" if q.get("stale") else ""

        us_market_text = "\n".join([
            f"{dji['emoji']}  **道瓊**：{dji['price']} ({dji['pct']}){_stale(dji)}",
            f"{ixic['emoji']} **納斯達克**：{ixic['price']} ({ixic['pct']}){_stale(ixic)}",
            f"{gspc['emoji']} **S&P 500**：{gspc['price']} ({gspc['pct']}){_stale(gspc)}",
        ])

        send_embed(DISCORD_US, {
            "title": f"🇺🇸 美股{session_info['label']} {weekend_banner}| {ts}",
            "description": f"**時段：** {session_info['period']}",
            "color": 0x3498DB,
            "fields": [
                {"name": "📊 三大指數", "value": truncate(us_market_text), "inline": False},
            ],
            "footer": {"text": f"資料來源：Yahoo Finance{weekend_footer}"},
        })
        time.sleep(1.2)

        send_discord_message(
            DISCORD_US,
            f"## 🤖 Claude AI 美股分析 — {session_tag}\n\n{analysis['us']}"
        )
        time.sleep(1.2)

        send_embed(DISCORD_US, {
            "title": f"📰 美股新聞連結 | {ts}",
            "color": 0x2980B9,
            "fields": [
                {"name": "🔗 本時段重要新聞", "value": truncate(us_links), "inline": False},
            ],
            "footer": {"text": "資料來源：Yahoo Finance / MarketWatch / Google News"},
        })
        time.sleep(1.5)

        # ── 🌍 國際頻道 ────────────────────────────────────────────
        vix   = market_data.get("vix",   {})
        us10y = market_data.get("us10y", {})
        dxy   = market_data.get("dxy",   {})
        gold  = market_data.get("gold",  {})
        oil   = market_data.get("oil",   {})

        global_market_text = "\n".join([
            f"{vix['emoji']}   **VIX**：{vix['price']} ({vix['pct']}){_stale(vix)}",
            f"{us10y['emoji']} **美債10Y**：{us10y['price']} ({us10y['pct']}){_stale(us10y)}",
            f"{dxy['emoji']}   **美元指數**：{dxy['price']} ({dxy['pct']}){_stale(dxy)}",
            f"{gold['emoji']}  **黃金**：{gold['price']} ({gold['pct']}){_stale(gold)}",
            f"{oil['emoji']}   **原油(WTI)**：{oil['price']} ({oil['pct']}){_stale(oil)}",
        ])

        send_embed(DISCORD_GLOBAL, {
            "title": f"🌍 國際市場{session_info['label']} {weekend_banner}| {ts}",
            "description": f"**時段：** {session_info['period']}",
            "color": 0x9B59B6,
            "fields": [
                {"name": "📉 總體指標",  "value": truncate(global_market_text), "inline": True},
                {"name": "🪙 加密貨幣",  "value": truncate(crypto_text),        "inline": True},
            ],
            "footer": {"text": f"資料來源：Yahoo Finance / CoinGecko{weekend_footer}"},
        })
        time.sleep(1.2)

        send_discord_message(
            DISCORD_GLOBAL,
            f"## 🤖 Claude AI 國際分析 — {session_tag}\n\n{analysis['global']}"
        )
        time.sleep(1.2)

        send_embed(DISCORD_GLOBAL, {
            "title": f"📰 國際新聞連結 | {ts}",
            "color": 0x8E44AD,
            "fields": [
                {"name": "🔗 本時段重要新聞", "value": truncate(global_links), "inline": False},
            ],
            "footer": {"text": "資料來源：Reuters / BBC / Google News"},
        })

        print("📤 [PublisherAgent] 完成")
