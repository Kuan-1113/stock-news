"""
shared/config.py — 所有設定集中管理
從環境變數讀取敏感資料，其餘設定直接定義於此
"""

import os
import pytz

# ── 時區 ────────────────────────────────────────────────────────
TAIPEI_TZ = pytz.timezone("Asia/Taipei")
TW_TZ = TAIPEI_TZ  # 向下相容舊名稱

# ── API Keys ─────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Discord Webhook URLs（從環境變數讀取，不寫死在程式碼裡）────────
# 本機測試請在 PowerShell 設定：$env:DISCORD_TW = "你的webhook"
# GitHub Actions 請在 Secrets 設定：DISCORD_TW / DISCORD_US / DISCORD_GLOBAL / DISCORD_WATCHLIST
DISCORD_TW        = os.environ.get("DISCORD_TW",        "")
DISCORD_US        = os.environ.get("DISCORD_US",        "")
DISCORD_GLOBAL    = os.environ.get("DISCORD_GLOBAL",    "")
DISCORD_WATCHLIST = os.environ.get("DISCORD_WATCHLIST", "")

# 啟動時檢查
_missing = [k for k, v in {
    "DISCORD_TW": DISCORD_TW,
    "DISCORD_US": DISCORD_US,
    "DISCORD_GLOBAL": DISCORD_GLOBAL,
    "DISCORD_WATCHLIST": DISCORD_WATCHLIST,
}.items() if not v]
if _missing:
    import warnings
    warnings.warn(f"⚠️ 未設定 Discord Webhook：{', '.join(_missing)}")

# ── 自選股清單 ────────────────────────────────────────────────────
WATCHLIST = [
    {"symbol": "2330.TW", "name": "台積電"},
    {"symbol": "2454.TW", "name": "聯發科"},
    {"symbol": "NVDA",    "name": "輝達"},
    {"symbol": "AAPL",    "name": "蘋果"},
    {"symbol": "TSLA",    "name": "特斯拉"},
    {"symbol": "^TWII",   "name": "加權指數"},
]

# ── 大盤指標（key → (Yahoo symbol, 顯示名稱)）────────────────────
MARKET_SYMBOLS = {
    "twii":  ("^TWII",    "台灣加權"),
    "dji":   ("^DJI",     "道瓊"),
    "ixic":  ("^IXIC",    "納斯達克"),
    "gspc":  ("^GSPC",    "S&P 500"),
    "vix":   ("^VIX",     "VIX"),
    "us10y": ("^TNX",     "美債10Y"),
    "dxy":   ("DX-Y.NYB", "美元指數"),
    "gold":  ("GC=F",     "黃金"),
    "oil":   ("CL=F",     "原油"),
}

# ── RSS 新聞來源 ──────────────────────────────────────────────────
RSS_FEEDS = {
    "tw": [
        "https://news.cnyes.com/rss/cat/tw_stock",
        "https://tw.stock.yahoo.com/rss",
        "https://news.google.com/rss/search?q=台股+股市&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "https://news.google.com/rss/search?q=台灣+財經+科技股&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "https://www.moneydj.com/rss/news.aspx?svc=NW&cat=MB",
    ],
    "us": [
        "https://news.google.com/rss/search?q=US+stock+market+Wall+Street&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=NASDAQ+S%26P500+Fed+earnings&hl=en-US&gl=US&ceid=US:en",
        "https://finance.yahoo.com/rss/topstories",
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    ],
    "global": [
        "https://news.google.com/rss/search?q=global+economy+Fed+inflation+oil&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=geopolitics+trade+war+OPEC&hl=en-US&gl=US&ceid=US:en",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
    ],
}

# ── Discord 訊息限制 ──────────────────────────────────────────────
MAX_EMBED_FIELD = 1024
MAX_CONTENT     = 2000
