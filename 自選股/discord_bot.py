"""
discord_bot.py（薄 Bot 版本）
接收 Discord /查股 指令 → 觸發 GitHub Actions → 結果透過 Webhook 傳回 Discord
Bot 本身不跑任何分析，極輕量，免費雲端即可運作。

環境變數：
  DISCORD_BOT_TOKEN  - Discord Bot Token
  GITHUB_PAT         - GitHub Personal Access Token（需 workflow 權限）
  GITHUB_REPO        - 選填，預設 Kuan-1113/stock-news
"""

import os
import asyncio
import discord
from discord import app_commands
import requests

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GITHUB_PAT        = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "Kuan-1113/stock-news")

# ─────────────────────────────────────────────────────────────
# GitHub Actions 觸發（同步，在 executor 中執行避免阻塞 event loop）
# ─────────────────────────────────────────────────────────────

def _trigger_sync(workflow_file: str, inputs: dict) -> tuple[bool, str]:
    """同步呼叫 GitHub API，回傳 (成功與否, 錯誤訊息)"""
    if not GITHUB_PAT:
        return False, "未設定 GITHUB_PAT"
    try:
        r = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{workflow_file}/dispatches",
            headers={
                "Authorization": f"token {GITHUB_PAT}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"ref": "main", "inputs": inputs},
            timeout=10,
        )
        if r.status_code == 204:
            print(f"  ✅ GitHub Actions [{workflow_file}] 觸發成功")
            return True, ""
        else:
            msg = f"HTTP {r.status_code}: {r.text[:100]}"
            print(f"  ❌ GitHub Actions [{workflow_file}] 失敗：{msg}")
            return False, msg
    except Exception as e:
        print(f"  ❌ GitHub API 例外：{e}")
        return False, str(e)[:80]

async def trigger_workflow(workflow_file: str, inputs: dict) -> tuple[bool, str]:
    """在 executor 中執行同步 HTTP 請求，不阻塞 Discord event loop"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _trigger_sync, workflow_file, inputs)

# ─────────────────────────────────────────────────────────────
# Discord Bot
# ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

@tree.command(name="查股", description="查詢股票行情與 AI 深度分析（約 1 分鐘後結果出現）")
@app_commands.describe(
    symbol="股票代碼（台股：2330.TW｜美股：NVDA｜指數：^TWII）",
    name="股票名稱（選填，如：台積電）",
)
async def cmd_查股(interaction: discord.Interaction, symbol: str, name: str = ""):
    try:
        await interaction.response.defer()
        sym       = symbol.strip().upper()
        ok, errmsg = await trigger_workflow(
            "query_stock.yml", {"symbol": sym, "name": name.strip()}
        )
        if ok:
            await interaction.followup.send(
                f"⏳ **{sym}** 分析啟動！約 **1 分鐘**後報告會出現在自選股頻道。"
            )
        else:
            await interaction.followup.send(
                f"❌ 觸發失敗：`{errmsg}`\n"
                f"請確認 GitHub Secret `GITHUB_PAT` 是否設定（需要 `workflow` 權限）。"
            )
    except Exception as e:
        print(f"❌ /查股 例外：{e}")
        try:
            await interaction.followup.send(f"❌ 發生錯誤：{str(e)[:100]}")
        except Exception:
            pass

@tree.command(name="自選股", description="立即執行完整自選股分析（約 2 分鐘後結果出現）")
async def cmd_自選股(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        ok, errmsg = await trigger_workflow("query_watchlist.yml", {})
        if ok:
            await interaction.followup.send(
                "⏳ 自選股分析啟動！約 **2 分鐘**後報告會出現在自選股頻道。"
            )
        else:
            await interaction.followup.send(f"❌ 觸發失敗：`{errmsg}`")
    except Exception as e:
        print(f"❌ /自選股 例外：{e}")
        try:
            await interaction.followup.send(f"❌ 發生錯誤：{str(e)[:100]}")
        except Exception:
            pass

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot 啟動：{client.user}（ID: {client.user.id}）")
    print(f"   已同步指令：{[c.name for c in tree.get_commands()]}")
    if not GITHUB_PAT:
        print("⚠️  警告：未設定 GITHUB_PAT，/查股 和 /自選股 將無法觸發分析")

if not DISCORD_BOT_TOKEN:
    print("❌ 未設定 DISCORD_BOT_TOKEN，Bot 無法啟動")
else:
    client.run(DISCORD_BOT_TOKEN)
