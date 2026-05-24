# 🇹🇼 台股日報自動發送系統

每日自動抓取台股、美股、國際財經新聞，透過 Claude AI 分析後發送到 Discord。

## 📅 發送時段（台灣時間）

| 時段 | 時間 | 內容 |
|------|------|------|
| 🌅 盤前早報 | 08:00 | 前日 22:00 ～ 今日 08:00 新聞 |
| ☀️ 盤中午報 | 14:00 | 今日 08:00 ～ 14:00 新聞 |
| 🌙 盤後晚報 | 22:00 | 今日 14:00 ～ 22:00 新聞 |

> 僅週一至週五執行

## 🚀 功能

- 📊 **大盤數據**：台灣加權、道瓊、納斯達克、S&P500、VIX、美債10Y、美元指數、黃金、原油
- 🪙 **加密貨幣**：BTC、ETH、SOL（CoinGecko）
- 📰 **RSS 新聞**：Google News、鉅亨網、MoneyDJ、Yahoo Finance、Reuters、BBC
- 🤖 **Claude AI 分析**：台股、美股、國際三份深度分析報告
- 📤 **Discord 發送**：三個頻道（台股、美股、國際）

## ⚙️ 設定

### GitHub Actions 自動排程

1. Fork 或 Clone 此 Repo
2. 到 `Settings > Secrets and variables > Actions` 新增：
   - `ANTHROPIC_API_KEY`：你的 Claude API Key

### 本機執行

```bash
# 安裝套件
pip install -r requirements.txt

# 設定環境變數
$env:ANTHROPIC_API_KEY="your-api-key"

# 立即執行一次
python stock_daily.py

# 啟動排程模式
python stock_daily.py --schedule
```

## 📁 檔案說明

| 檔案 | 說明 |
|------|------|
| `stock_daily.py` | 主程式（Claude AI + RSS + Discord） |
| `run_daily.bat` | Windows 一鍵執行 |
| `run_schedule.bat` | Windows 排程模式 |
| `run_test.bat` | 功能測試 |
| `.github/workflows/stock-daily.yml` | GitHub Actions 排程設定 |
