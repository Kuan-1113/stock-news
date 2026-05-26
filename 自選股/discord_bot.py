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
import discord
from discord import app_commands
import requests

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GITHUB_PAT        = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "Kuan-1113/stock-news")

# ─────────────────────────────────────────────────────────────
# GitHub Actions 觸發
# ─────────────────────────────────────────────────────────────

def trigger_workflow(workflow_file: str, inputs: dict) -> bool:
    if not GITHUB_PAT:
        print("❌ 未設定 GITHUB_PAT")
        return False
    r = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{workflow_file}/dispatches",
        headers={
            "Authorization": f"token {GITHUB_PAT}",
            "Accept": "application/vnd.github.v3+json",
        },
        json={"ref": "main", "inputs": inputs},
        timeout=10,
    )
    ok = r.status_code == 204
    print(f"  GitHub Actions [{workflow_file}]: {'✅ 觸發成功' if ok else f'❌ {r.status_code} {r.text[:100]}'}")
    return ok

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
    await interaction.response.defer()
    sym = symbol.strip().upper()
    ok  = trigger_workflow("query_stock.yml", {"symbol": sym, "name": name.strip()})
    if ok:
        await interaction.followup.send(
            f"⏳ **{sym}** 分析啟動！約 **1 分鐘**後報告會出現在自選股頻道。"
        )
    else:
        await interaction.followup.send(
            f"❌ 觸發失敗，請稍後再試。\n（如持續失敗請確認 GitHub Secret `GITHUB_PAT` 是否設定正確）"
        )

@tree.command(name="自選股", description="立即執行完整自選股分析（約 2 分鐘後結果出現）")
async def cmd_自選股(interaction: discord.Interaction):
    await interaction.response.defer()
    ok = trigger_workflow("query_watchlist.yml", {})
    if ok:
        await interaction.followup.send(
            "⏳ 自選股分析啟動！約 **2 分鐘**後報告會出現在自選股頻道。"
        )
    else:
        await interaction.followup.send(
            "❌ 觸發失敗，請稍後再試。"
        )

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot 啟動：{client.user}（ID: {client.user.id}）")
    print(f"   已同步 {len(tree.get_commands())} 個指令")

if not DISCORD_BOT_TOKEN:
    print("❌ 未設定 DISCORD_BOT_TOKEN，Bot 無法啟動")
else:
    client.run(DISCORD_BOT_TOKEN)
