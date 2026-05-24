"""
test_discord.py - 快速測試三個 Discord Webhook 連線
"""
import requests
import time

DISCORD_TW     = "https://discord.com/api/webhooks/1507952802662449152/8iumIv-Bs5PTRVlMpFXbE7wH_uzHJlLtmybTHaj1zUDxksQBZwRAOs7v69tvSOezmWnW"
DISCORD_US     = "https://discord.com/api/webhooks/1507952945130635307/3Rd1BBhGElvH4N7RZeQaOHNp5FqiCGBO4d9UZwK29dY1wksN70CWYh4MJ19tRfUuSOVX"
DISCORD_GLOBAL = "https://discord.com/api/webhooks/1507953174512668902/QsKOUt5afzwQYfbQQeGi8Tza2-gkLKUJaP-B03lWEyX9C5ops59NuGHLJCK7a8UC9N5-"

webhooks = {
    "台股": DISCORD_TW,
    "美股": DISCORD_US,
    "國際": DISCORD_GLOBAL,
}

for name, url in webhooks.items():
    try:
        r = requests.post(url, json={"content": f"[測試] {name} Discord Webhook 連線正常 ✅"}, timeout=10)
        status = "✅ 成功" if r.status_code in [200, 204] else f"❌ 失敗 ({r.status_code}: {r.text[:100]})"
        print(f"{name}: {status}")
    except Exception as e:
        print(f"{name}: ❌ 錯誤 {e}")
    time.sleep(1)
