import requests
import time

urls = [
    ("台股", "https://discord.com/api/webhooks/1507952802662449152/8iumIv-Bs5PTRVlMpFXbE7wH_uzHJlLtmybTHaj1zUDxksQBZwRAOs7v69tvSOezmWnW"),
    ("美股", "https://discord.com/api/webhooks/1507952945130635307/3Rd1BBhGElvH4N7RZeQaOHNp5FqiCGBO4d9UZwK29dY1wksN70CWYh4MJ19tRfUuSOVX"),
    ("國際", "https://discord.com/api/webhooks/1507953174512668902/QsKOUt5afzwQYfbQQeGi8Tza2-gkLKUJaP-B03lWEyX9C5ops59NuGHLJCK7a8UC9N5-"),
]

for name, url in urls:
    r = requests.post(url, json={"content": "[測試] " + name + " OK"}, timeout=10)
    print(name, r.status_code)
    time.sleep(2)
