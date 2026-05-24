"""
run_test.py - 測試腳本，用於驗證 stock_daily.py 的各個功能
"""
import sys
import io
import os

# 強制 UTF-8 輸出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 設定 API Key（從環境變數讀取）
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

print("=" * 60)
print("股市日報系統 - 功能測試")
print("=" * 60)

# 測試 1: 套件匯入
print("\n[1] 測試套件匯入...")
try:
    import requests
    import feedparser
    import schedule
    import anthropic
    print("  OK: requests, feedparser, schedule, anthropic 全部正常")
except ImportError as e:
    print(f"  FAIL: {e}")
    sys.exit(1)

# 測試 2: Claude API
print("\n[2] 測試 Claude API 連線...")
if not ANTHROPIC_API_KEY:
    print("  SKIP: 未設定 ANTHROPIC_API_KEY")
else:
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=50,
            messages=[{"role": "user", "content": "請用繁體中文回覆：連線測試成功"}],
        )
        print(f"  OK: {msg.content[0].text}")
    except Exception as e:
        print(f"  FAIL: {e}")

# 測試 3: Yahoo Finance
print("\n[3] 測試 Yahoo Finance 大盤數據...")
try:
    r = requests.get(
        "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?interval=1d&range=2d",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    if r.status_code == 200:
        data = r.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if closes:
            print(f"  OK: 台灣加權指數 = {closes[-1]:,.2f}")
        else:
            print("  WARN: 無收盤數據")
    else:
        print(f"  FAIL: HTTP {r.status_code}")
except Exception as e:
    print(f"  FAIL: {e}")

# 測試 4: RSS 新聞
print("\n[4] 測試 RSS 新聞抓取...")
try:
    feed = feedparser.parse(
        "https://news.google.com/rss/search?q=台股+股市&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        request_headers={"User-Agent": "Mozilla/5.0"},
    )
    count = len(feed.entries)
    if count > 0:
        print(f"  OK: 抓到 {count} 篇台股新聞")
        print(f"  最新: {feed.entries[0].title[:50]}")
    else:
        print("  WARN: 無新聞（可能被封鎖）")
except Exception as e:
    print(f"  FAIL: {e}")

# 測試 5: Discord Webhook
print("\n[5] 測試 Discord Webhook...")
DISCORD_TW = "https://discord.com/api/webhooks/1507952802662449152/8iumIv-Bs5PTRVlMpFXbE7wH_uzHJlLtmybTHaj1zUDxksQBZwRAOs7v69tvSOezmWnW"
try:
    r = requests.post(
        DISCORD_TW,
        json={"content": "🧪 **系統測試** | stock_daily.py 連線測試成功 ✅"},
        timeout=10,
    )
    if r.status_code in [200, 204]:
        print("  OK: Discord 台股頻道發送成功")
    else:
        print(f"  FAIL: HTTP {r.status_code} {r.text[:100]}")
except Exception as e:
    print(f"  FAIL: {e}")

# 測試 6: CoinGecko
print("\n[6] 測試 CoinGecko 加密貨幣...")
try:
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"},
        timeout=10,
    )
    if r.status_code == 200:
        btc = r.json().get("bitcoin", {})
        print(f"  OK: BTC = ${btc.get('usd', 'N/A'):,.2f} ({btc.get('usd_24h_change', 0):+.2f}%)")
    else:
        print(f"  FAIL: HTTP {r.status_code}")
except Exception as e:
    print(f"  FAIL: {e}")

print("\n" + "=" * 60)
print("測試完成！若以上全部 OK，可執行：")
print("  python stock_daily.py")
print("=" * 60)
