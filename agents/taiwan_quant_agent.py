"""
agents/taiwan_quant_agent.py — 台股量化 Agent

整合台股量化選股全流程，重點聚焦「起漲初期」信號識別。

功能：
  1. 執行 13 個指標策略 + 3 個籌碼策略完整掃描
  2. 信號分層：起漲初期（🌱）/ 動量確認（📈）/ 籌碼支撐（💹）
  3. Claude AI 量化深度分析（嵌入量化研究知識）
  4. 起漲初期信號優先展示，並給出觀察重點

量化知識基礎（基於研究摘要）：
  - 布林帶突破+量能：歷史勝率 +812%（特定參數）
  - RSI 策略：單純 RSI 勝率 57.7%，搭配 ADX 可提升至 65-70%
  - 量縮整理後放量：為最可靠的起漲信號（量先價行）
  - MA5 突破 MA20：趨勢翻多初期，勝率顯著高於金叉後追進
  - 平台整理突破：機構累積完成的特徵，突破有效性高

排程：隨 StrategyAgent 在 17:00 TW 後執行（每日盤後）
頻道：DISCORD_STRATEGY（與策略明牌同頻道）
"""

from __future__ import annotations

import os
import time
import datetime
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

import os as _os
from shared.config import ANTHROPIC_API_KEY
from shared.utils  import send_discord_message, now_str

DISCORD_STRATEGY: str = _os.environ.get("DISCORD_STRATEGY", "")

# 起漲初期策略名稱（優先展示）
EARLY_BREAKOUT_STRATEGIES = frozenset({
    "volume_buildup",       # 底部量縮整理後放量突破
    "ma5_cross_fresh",      # MA5 剛突破 MA20
    "flat_base_breakout",   # 低檔平台整理突破
})

# 量化研究知識庫（內嵌關鍵發現）
_QUANT_KNOWLEDGE = """
【量化選股研究摘要 — 台股適用版】

■ 起漲初期信號（本系統新增，重點關注）
  • 底部量縮放量：縮量整理 ≥5天 → 今日放量 1.5x + MA5 站穩 + RSI 35-62 + 近15日漲幅<12%
    → 量先價行，最可靠的起漲信號；機構吸籌完畢才放量突破
  • MA5 剛突破 MA20：趨勢翻多「第一天」（昨MA5<MA20，今MA5>MA20）+ 量放大 + 近20日漲幅<15%
    → 趨勢剛翻多初期進場，優於追金叉後已漲過一段
  • 低檔平台整理突破：近15天高低差<8% → 今日突破上緣 + 放量 1.3x
    → 平台整理=機構累積特徵，突破=機構開始拉抬

■ 動量策略現有問題與對策
  • 布林上軌突破：信號滯後，已加入「近20日漲幅>20%跳過」過濾
  • 52週新高：機構追高邏輯，已漲幅大，風險/報酬比下降
  • RSI + ADX 組合：文獻勝率 65-70%（優於純 RSI 的 57.7%）
  • 量比 >1.5x：確認突破有效性的關鍵指標

■ 信號篩選原則
  • RSI 進場最佳區間：35-62（離開超賣但未超買）
  • 近期漲幅過濾：>15% 的信號謹慎，>20% 建議跳過
  • 籌碼確認：外資/投信連買 3 天 = 最強確認
  • 量能配合：突破日量比 ≥1.5x 才算有效突破

■ 風控要點
  • 停損設於 MA20 下方（均線破位即出）
  • 起漲初期信號停損可設 ATR 1.2x（比動量信號更緊）
  • 單日最大持股不超過 3 支（分散風險）
"""


def _claude_quant_analysis(signals: list[dict], early_signals: list[dict],
                            regime: str, today: str) -> str:
    """
    呼叫 Claude 進行量化深度分析。
    嵌入量化知識庫，重點分析起漲初期信號。
    """
    if not ANTHROPIC_API_KEY:
        return ""

    from agents.analyst_agent import claude_call

    # 整理信號摘要
    early_lines = []
    for s in early_signals[:5]:
        early_lines.append(
            f"• {s['symbol']} {s['name']} [{s['strategy_label']}]"
            f" 進場${s['entry_price']:.1f}  {s['detail']}"
        )

    other_lines = []
    for s in [x for x in signals if x not in early_signals][:5]:
        other_lines.append(
            f"• {s['symbol']} {s['name']} [{s['strategy_label']}] {s['detail']}"
        )

    early_text = "\n".join(early_lines) if early_lines else "（今日無起漲初期信號）"
    other_text = "\n".join(other_lines) if other_lines else "（無）"
    regime_zh  = {"trend": "趨勢市（ADX≥25）", "range": "震盪盤（ADX<25）"}.get(regime, regime)

    prompt = f"""{_QUANT_KNOWLEDGE}

---
今日（{today}）台股量化掃描結果：
大盤狀態：{regime_zh}
信號總數：{len(signals)} 支（起漲初期 {len(early_signals)} 支 + 動量確認 {len(signals)-len(early_signals)} 支）

【🌱 起漲初期信號（重點）】
{early_text}

【📈 動量確認信號】
{other_text}

請以台股量化分析師身份，提供以下分析（繁體中文，500字以內）：

1. **今日市場量化觀察**（大盤狀態對選股的影響，2句）
2. **起漲初期信號解讀**（哪支最值得關注？量能/位置/勝率判斷，3句）
3. **操作建議**
   • 優先關注：（列股票代碼 + 一句理由）
   • 進場條件：（量能/價格確認條件）
   • 風控要點：（停損位、持股數限制）
4. **一句話定調**（今日整體量化環境評估）

⚠️ AI 量化分析，不構成投資建議。"""

    try:
        return claude_call(prompt, max_tokens=700)
    except Exception as e:
        print(f"  ⚠️ [TaiwanQuantAgent] Claude 分析失敗：{e}", flush=True)
        return ""


def _format_signal_block(signals: list[dict], title: str, emoji: str) -> str:
    """格式化信號區塊"""
    if not signals:
        return ""
    lines = [f"\n{emoji} **{title}**（{len(signals)} 支）"]
    for s in signals[:8]:  # 最多顯示 8 支
        lines.append(
            f"  • **{s['symbol']} {s['name']}**"
            f"  進場 `${s['entry_price']:.1f}`"
            f"  {s['detail'][:60]}"
        )
    return "\n".join(lines)


class TaiwanQuantAgent:
    """
    台股量化 Agent — 整合起漲初期策略 + Claude 量化分析。
    在 StrategyAgent 掃描完成後執行，提供更深度的量化視角。
    """

    def run(self, signals: list[dict] | None = None,
            regime: str = "unknown",
            dry_run: bool = False) -> dict:
        """
        Args:
            signals  : 由 strategy_agent.runner.run() 回傳的信號列表
                       None = 自行執行掃描
            regime   : 大盤狀態 ("trend"/"range"/"unknown")
            dry_run  : True = 不發 Discord
        """
        today = datetime.date.today().isoformat()
        print(f"\n🔬 [TaiwanQuantAgent] 台股量化分析啟動 {today}", flush=True)

        # 若沒有外部傳入信號，自行執行掃描
        if signals is None:
            print("  📊 執行完整量化掃描...", flush=True)
            from strategy_agent.runner import run as _strategy_run
            result  = _strategy_run(dry_run=True)
            signals = result.get("signals", [])
            regime  = result.get("regime", "unknown")

        if not signals:
            print("  ℹ️ 今日無量化信號，跳過", flush=True)
            return {"signals": [], "early": [], "report": ""}

        # ── 信號分層 ──────────────────────────────────────────────
        early_signals = [
            s for s in signals
            if s.get("strategy") in EARLY_BREAKOUT_STRATEGIES
        ]
        momentum_signals = [
            s for s in signals
            if s.get("strategy") not in EARLY_BREAKOUT_STRATEGIES
        ]

        print(f"  🌱 起漲初期信號：{len(early_signals)} 支", flush=True)
        print(f"  📈 動量確認信號：{len(momentum_signals)} 支", flush=True)

        # ── Claude 量化分析 ───────────────────────────────────────
        analysis = ""
        if ANTHROPIC_API_KEY:
            print("  🤖 Claude 量化深度分析中...", flush=True)
            analysis = _claude_quant_analysis(signals, early_signals, regime, today)

        # ── 組裝報告 ─────────────────────────────────────────────
        regime_zh = {"trend": "趨勢市 ADX≥25 📈", "range": "震盪盤 ADX<25 📉"}.get(
            regime, f"狀態未知 ({regime})"
        )
        header = (
            f"## 🔬 台股量化精選 | {today}\n"
            f"大盤：{regime_zh}　信號：{len(signals)} 支"
            f"（🌱起漲初期 {len(early_signals)} / 📈動量確認 {len(momentum_signals)}）"
        )

        early_block    = _format_signal_block(early_signals,    "起漲初期（重點關注）", "🌱")
        momentum_block = _format_signal_block(momentum_signals, "動量確認信號",         "📈")

        report_parts = [header]
        if early_block:
            report_parts.append(early_block)
        if momentum_block:
            report_parts.append(momentum_block)

        report_parts.append(
            "\n---\n"
            "📖 **信號說明**\n"
            "🌱 起漲初期 = 量縮整理後放量 / MA5剛穿MA20 / 平台突破（漲幅尚小）\n"
            "📈 動量確認 = 趨勢已成型，追漲需注意位置與已漲幅度"
        )

        if analysis:
            report_parts.append(f"\n---\n🤖 **Claude 量化觀點**\n{analysis}")

        report_parts.append("\n⚠️ 量化信號，非投資建議，請自行評估風險")
        report = "\n".join(report_parts)

        # ── 發送 Discord ─────────────────────────────────────────
        if not dry_run and DISCORD_STRATEGY:
            send_discord_message(DISCORD_STRATEGY, report)
            print(f"  ✅ [TaiwanQuantAgent] Discord 發送完成", flush=True)
        elif not DISCORD_STRATEGY:
            print("  ⚠️ DISCORD_STRATEGY 未設定，跳過發送", flush=True)

        return {
            "signals":  signals,
            "early":    early_signals,
            "momentum": momentum_signals,
            "report":   report,
        }
