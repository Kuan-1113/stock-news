"""
agents/scheduler_agent.py — 排程守衛 Agent

職責：
  1. 集中定義所有排程時間（Single Source of Truth）
  2. 主迴圈：按時觸發日報 / 策略 / 週回顧 / Podcast
  3. 開機補跑：在合理窗口內自動補發遺漏任務
  4. 健康監控：偵測連續遺漏 → 送 Discord 告警
  5. 雙層防重複：in-memory + trigger_log.json 跨重啟保護

排程時間表（修改時間只需改這裡，不用動 main.py）：
  07:57 TW → 早報（08:00 到達）
  14:57 TW → 午報（15:00 到達）
  17:00 TW → 策略選股（週一～五）
  17:30 TW → 週回顧報告（週五）
  21:57 TW → 晚報（22:00 到達）
"""

from __future__ import annotations

import os
import json
import time
import datetime
import threading
from typing import Callable

import pytz

TW_TZ = pytz.timezone("Asia/Taipei")

# ── 排程配置（Single Source of Truth）──────────────────────────────
#
# 欄位說明：
#   name          : trigger_log.json 的 key
#   trigger_hour  : 觸發小時（台灣時區）
#   trigger_min   : 觸發分鐘
#   label         : 顯示名稱
#   session       : 傳給 _run_daily_report() 的參數（None = 不是日報）
#   recovery_min  : 補跑窗口（分鐘）— 觸發點前 2 分 ~ 後 N 分
#   weekdays_only : True = 只在週一～五執行
#   weekday       : 指定星期幾（0=週一, 4=週五，None=每天）
#
SCHEDULES: list[dict] = [
    {
        "name":         "daily_morning",
        "trigger_hour": 7,
        "trigger_min":  57,
        "label":        "早報",
        "session":      "morning",
        "recovery_min": 15,
        "weekdays_only": False,
        "weekday":      None,
    },
    {
        "name":         "daily_afternoon",
        "trigger_hour": 14,
        "trigger_min":  57,
        "label":        "午報",
        "session":      "afternoon",
        "recovery_min": 15,
        "weekdays_only": False,
        "weekday":      None,
    },
    {
        "name":         "daily_evening",
        "trigger_hour": 21,
        "trigger_min":  57,
        "label":        "晚報",
        "session":      "evening",
        "recovery_min": 15,
        "weekdays_only": False,
        "weekday":      None,
    },
    {
        "name":         "strategy",
        "trigger_hour": 17,
        "trigger_min":  0,
        "label":        "策略選股",
        "session":      None,
        "recovery_min": 15,
        "weekdays_only": True,
        "weekday":      None,
    },
    {
        "name":         "weekly",
        "trigger_hour": 17,
        "trigger_min":  30,
        "label":        "週回顧報告",
        "session":      None,
        "recovery_min": 15,
        "weekdays_only": True,
        "weekday":      4,   # 週五
    },
]

# Podcast（用 GitHub Actions workflow_dispatch 觸發，非直接執行）
PODCAST_HOURS: set[int] = {8, 20}   # 08:57 / 20:57 TW 觸發

# ── trigger_log 路徑 ───────────────────────────────────────────────
_TRIGGER_LOG_PATH: str = os.environ.get(
    "TRIGGER_LOG_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trigger_log.json"),
)


# ── trigger_log 讀寫 ───────────────────────────────────────────────

def load_trigger_log() -> dict:
    try:
        with open(_TRIGGER_LOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_trigger_log(task: str, status: str = "ok") -> None:
    """記錄任務觸發（台灣時區日期，14 天滾動保留）"""
    today = datetime.datetime.now(TW_TZ).date().isoformat()
    log   = load_trigger_log()
    log.setdefault(today, {})[task] = {
        "time":   datetime.datetime.now(TW_TZ).isoformat(),
        "status": status,
    }
    cutoff = (datetime.datetime.now(TW_TZ).date() - datetime.timedelta(days=14)).isoformat()
    log = {d: v for d, v in log.items() if d >= cutoff}
    try:
        with open(_TRIGGER_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ [SchedulerAgent] trigger_log 寫入失敗：{e}", flush=True)


def was_triggered_today(task: str) -> bool:
    """檢查今日（台灣時區）是否已成功觸發"""
    today = datetime.datetime.now(TW_TZ).date().isoformat()
    entry = load_trigger_log().get(today, {}).get(task, {})
    return isinstance(entry, dict) and entry.get("status") == "ok"


# ── 健康狀態查詢 ───────────────────────────────────────────────────

def get_schedule_status() -> list[dict]:
    """
    回傳今日所有排程的執行狀態，供 Discord /診斷 指令使用。
    格式：[{"name": "早報", "done": True, "time": "08:02", "next": "明日 07:57"}]
    """
    now   = datetime.datetime.now(TW_TZ)
    today = now.date().isoformat()
    log   = load_trigger_log().get(today, {})
    result = []

    for s in SCHEDULES:
        entry  = log.get(s["name"], {})
        done   = isinstance(entry, dict) and entry.get("status") == "ok"
        run_at = entry.get("time", "")[:16].replace("T", " ") if done else None

        # 計算下次觸發時間
        trigger = now.replace(hour=s["trigger_hour"], minute=s["trigger_min"], second=0, microsecond=0)
        if trigger <= now:
            trigger += datetime.timedelta(days=1)
        next_str = trigger.strftime("%m/%d %H:%M")

        result.append({
            "name":    s["label"],
            "done":    done,
            "time":    run_at,
            "next":    next_str,
            "session": s.get("session"),
        })

    return result


def format_status_for_discord() -> str:
    """格式化排程健康狀態給 Discord 顯示"""
    now    = datetime.datetime.now(TW_TZ)
    status = get_schedule_status()
    lines  = [f"📅 **排程健康狀態** — {now.strftime('%Y-%m-%d %H:%M')} TW\n"]
    for s in status:
        icon  = "✅" if s["done"] else "⏳"
        time_ = f"（{s['time']}）" if s["time"] else ""
        lines.append(f"{icon} **{s['name']}** {time_} → 下次：{s['next']}")
    return "\n".join(lines)


# ── 核心 Agent 類別 ────────────────────────────────────────────────

class SchedulerAgent:
    """
    排程守衛 Agent。
    透過 callbacks 執行任務（不直接 import main.py，避免循環依賴）。
    """

    def __init__(
        self,
        run_daily_report:  Callable[[str], None],
        run_strategy:      Callable[[], None],
        run_weekly:        Callable[[], None],
        trigger_podcast:   Callable[[], None] | None = None,
        notify_discord:    Callable[[str], None] | None = None,
    ) -> None:
        """
        Args:
            run_daily_report : 執行日報，傳入 session（morning/afternoon/evening）
            run_strategy     : 執行策略選股
            run_weekly       : 執行週回顧報告
            trigger_podcast  : 觸發 Podcast（可選）
            notify_discord   : 發送 Discord 告警訊息（可選）
        """
        self._run_daily   = run_daily_report
        self._run_strategy = run_strategy
        self._run_weekly  = run_weekly
        self._podcast     = trigger_podcast
        self._notify      = notify_discord

        self._triggered:  set[str]             = set()   # Layer 1 in-memory
        self._trigger_ts: dict[str, datetime.datetime] = {}

    # ── 補跑（開機時呼叫）────────────────────────────────────────

    def startup_recovery(self) -> None:
        """
        開機時掃描：只在觸發點「前 2 分 ~ 後 recovery_min 分」內補跑。
        Railway 重啟若落在 18:xx → 窗口早過，完全不觸發。
        """
        now = datetime.datetime.now(TW_TZ)
        print(f"🛡️ [SchedulerAgent] 開機補跑掃描 {now.strftime('%H:%M')} TW...", flush=True)

        for s in SCHEDULES:
            if not self._is_applicable_today(s, now):
                continue
            if was_triggered_today(s["name"]):
                continue
            if self._in_recovery_window(s, now):
                label = s["label"]
                ts    = now.strftime("%H:%M")
                print(f"⚠️ [SchedulerAgent] 補跑：{label}（重啟後偵測，現在 {ts}）", flush=True)
                self._notify_recovery(label, ts)
                self._dispatch(s)

    # ── 主排程迴圈 ────────────────────────────────────────────────

    def run_scheduler_loop(self) -> None:
        """
        主排程迴圈（15 秒輪詢）。
        在獨立 thread 執行，不阻塞主程式。
        """
        import random
        time.sleep(random.uniform(0, 5))   # 啟動保護

        print("⏰ [SchedulerAgent] 排程主迴圈啟動（15 秒輪詢）", flush=True)

        while True:
            try:
                self._tick()
            except Exception as e:
                print(f"⚠️ [SchedulerAgent] 排程例外：{e}", flush=True)
            time.sleep(15)

    def start(self) -> None:
        """在背景 thread 啟動排程主迴圈"""
        threading.Thread(target=self.startup_recovery, daemon=True).start()
        threading.Thread(target=self.run_scheduler_loop, daemon=True).start()

    # ── 內部邏輯 ─────────────────────────────────────────────────

    def _tick(self) -> None:
        now     = datetime.datetime.now(TW_TZ)
        date_h  = f"{now.strftime('%Y-%m-%d')}-{now.hour}"

        # 每天 00:00 清空 in-memory
        if now.hour == 0 and now.minute < 1 and self._triggered:
            self._triggered.clear()
            self._trigger_ts.clear()
            print("⏰ [SchedulerAgent] 每日 00:00 清空 in-memory 觸發記錄", flush=True)

        for s in SCHEDULES:
            if not self._is_applicable_today(s, now):
                continue

            key = f"{s['name']}-{now.strftime('%Y-%m-%d')}"

            # 是否在觸發分鐘（:trigger_min ~ :trigger_min+2）
            in_trigger = (
                now.hour   == s["trigger_hour"] and
                s["trigger_min"] <= now.minute <= s["trigger_min"] + 2
            )
            if not in_trigger:
                continue
            if key in self._triggered:
                continue
            # Layer 2：今日是否已完成
            if was_triggered_today(s["name"]):
                print(f"⏰ [SchedulerAgent] {s['label']} 今日已完成，跳過", flush=True)
                self._triggered.add(key)
                continue
            # 10 分鐘防重複
            last_ts = self._trigger_ts.get(key)
            if last_ts and (now - last_ts).total_seconds() < 600:
                self._triggered.add(key)
                continue

            self._triggered.add(key)
            self._trigger_ts[key] = now
            print(f"⏰ [SchedulerAgent] 啟動 {s['label']}（{now.strftime('%H:%M')} TW）", flush=True)
            self._dispatch(s)

        # Podcast（整點前 3 分鐘觸發，:57～:59）
        if self._podcast and 57 <= now.minute:
            pod_key = f"podcast-{now.strftime('%Y-%m-%d')}-{now.hour}"
            if now.hour in PODCAST_HOURS and pod_key not in self._triggered:
                self._triggered.add(pod_key)
                print(f"⏰ [SchedulerAgent] 觸發 Podcast（{now.strftime('%H:%M')} TW）", flush=True)
                threading.Thread(target=self._podcast, daemon=True).start()

    def _dispatch(self, s: dict) -> None:
        """根據排程類型派發任務（在背景 thread 執行）"""
        session = s.get("session")
        name    = s["name"]

        def _run():
            try:
                if session:                         # 日報
                    self._run_daily(session)
                    save_trigger_log(name, "ok")
                elif name == "strategy":            # 策略選股
                    self._run_strategy()
                    save_trigger_log(name, "ok")
                elif name == "weekly":              # 週回顧
                    self._run_weekly()
                    save_trigger_log(name, "ok")
            except Exception as e:
                save_trigger_log(name, f"failed:{e}")
                print(f"❌ [SchedulerAgent] {s['label']} 執行失敗：{e}", flush=True)
                if self._notify:
                    self._notify(f"❌ **{s['label']}** 執行失敗：{str(e)[:100]}")

        threading.Thread(target=_run, daemon=True).start()

    def _is_applicable_today(self, s: dict, now: datetime.datetime) -> bool:
        """檢查今天是否應該執行此排程"""
        weekday = now.weekday()
        if s.get("weekdays_only") and weekday >= 5:
            return False
        if s.get("weekday") is not None and weekday != s["weekday"]:
            return False
        return True

    def _in_recovery_window(self, s: dict, now: datetime.datetime) -> bool:
        """檢查是否在補跑窗口內（觸發點前 2 分 ~ 後 recovery_min 分）"""
        trigger = now.replace(
            hour=s["trigger_hour"], minute=s["trigger_min"],
            second=0, microsecond=0
        )
        start = trigger - datetime.timedelta(minutes=2)
        end   = trigger + datetime.timedelta(minutes=s.get("recovery_min", 15))
        return start <= now < end

    def _notify_recovery(self, label: str, ts: str) -> None:
        if self._notify:
            try:
                self._notify(f"⚠️ **[排程守衛]** `{label}` 遺漏偵測，{ts} 自動補跑")
            except Exception:
                pass
