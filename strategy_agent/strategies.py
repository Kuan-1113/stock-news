"""
strategy_agent/strategies.py — 策略定義

每個策略函式：
  輸入：ohlcv list（由舊到新排列）
        每筆格式：{"date": "YYYY-MM-DD", "open": float, "high": float,
                   "low": float, "close": float, "volume": int}
  輸出：bool（今日是否觸發信號）

策略設計原則：
  - 只看今日（ohlcv[-1]）是否觸發
  - 需要足夠歷史資料作為計算基礎
  - 純技術面，不依賴外部 API
"""

from __future__ import annotations


# ── 輔助函式 ──────────────────────────────────────────────────────

def _ma(closes: list[float], n: int, offset: int = 0) -> float | None:
    """計算 MA(n)，offset=1 代表昨天"""
    end = len(closes) - offset
    if end < n:
        return None
    arr = closes[end - n: end]
    return sum(arr) / n


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    relevant = deltas[-(period):]
    gains  = [d for d in relevant if d > 0]
    losses = [-d for d in relevant if d < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _atr(ohlcv: list, period: int = 14) -> float:
    if len(ohlcv) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(ohlcv)):
        h, l, pc = ohlcv[i]["high"], ohlcv[i]["low"], ohlcv[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


# ── 策略 1：動量突破（創 20 日新高）────────────────────────────────

def strategy_momentum(ohlcv: list) -> tuple[bool, str]:
    """
    收盤價突破前 20 個交易日最高點。
    適合：趨勢啟動型，適合追強勢股。
    """
    if len(ohlcv) < 22:
        return False, ""
    today    = ohlcv[-1]
    high_20d = max(r["high"] for r in ohlcv[-22:-1])   # 前20日（不含今日）
    if today["close"] > high_20d:
        return True, f"收盤 {today['close']:.1f} 突破近20日高點 {high_20d:.1f}"
    return False, ""


# ── 策略 2：均線黃金交叉（MA5 穿越 MA20）───────────────────────────

def strategy_ma_crossover(ohlcv: list) -> tuple[bool, str]:
    """
    MA5 由下方穿越 MA20（黃金交叉）。
    昨天 MA5 < MA20，今天 MA5 >= MA20。
    """
    if len(ohlcv) < 22:
        return False, ""
    closes = [r["close"] for r in ohlcv]
    ma5_t  = _ma(closes, 5)
    ma20_t = _ma(closes, 20)
    ma5_y  = _ma(closes, 5, offset=1)
    ma20_y = _ma(closes, 20, offset=1)
    if None in (ma5_t, ma20_t, ma5_y, ma20_y):
        return False, ""
    if ma5_y < ma20_y and ma5_t >= ma20_t:
        return True, f"MA5({ma5_t:.1f}) 穿越 MA20({ma20_t:.1f}) 黃金交叉"
    return False, ""


# ── 策略 3：RSI 超賣反彈（RSI < 30 且今收 > 昨收）─────────────────

def strategy_rsi_oversold(ohlcv: list) -> tuple[bool, str]:
    """
    RSI(14) 低於 30（超賣區），且今日收盤 > 昨日收盤（開始反彈）。
    適合：均值回歸，低接反彈。
    """
    if len(ohlcv) < 17:
        return False, ""
    closes = [r["close"] for r in ohlcv]
    rsi    = _rsi(closes[-16:])
    if rsi is None:
        return False, ""
    today_up = ohlcv[-1]["close"] > ohlcv[-2]["close"]
    if rsi < 30 and today_up:
        return True, f"RSI(14)={rsi:.1f} 超賣區，今日開始反彈"
    return False, ""


# ── 策略 4：爆量收紅（量比 > 2.5x + 收紅 + 收盤在高點區）────────────

def strategy_volume_surge(ohlcv: list) -> tuple[bool, str]:
    """
    今日成交量 > 近20日均量的 2.5 倍，且收紅（今收 > 今開）。
    收盤位置在今日振幅的上半段（強勢特徵）。
    """
    if len(ohlcv) < 22:
        return False, ""
    today   = ohlcv[-1]
    vols    = [r["volume"] for r in ohlcv[-22:-1] if r.get("volume", 0) > 0]
    if not vols:
        return False, ""
    avg_vol   = sum(vols) / len(vols)
    vol_ratio = today.get("volume", 0) / avg_vol if avg_vol > 0 else 0
    is_up     = today["close"] > today["open"]
    rng       = today["high"] - today["low"]
    close_pct = (today["close"] - today["low"]) / rng if rng > 0 else 0
    if vol_ratio >= 2.5 and is_up and close_pct >= 0.6:
        return True, f"爆量 {vol_ratio:.1f}x，收盤在高檔區（{close_pct:.0%}）"
    return False, ""


# ── 策略 5：強勢回檔至 MA20 支撐反彈 ────────────────────────────────

def strategy_pullback_ma20(ohlcv: list) -> tuple[bool, str]:
    """
    近30日高點在現價的 80% 以上（強勢股），
    今日低點觸及 MA20（± 1.5%），且收盤 > 今開（反彈確認）。
    適合：強勢股拉回 MA20 低接。
    """
    if len(ohlcv) < 32:
        return False, ""
    closes  = [r["close"] for r in ohlcv]
    ma20    = _ma(closes, 20)
    if ma20 is None:
        return False, ""
    high30  = max(r["high"] for r in ohlcv[-31:-1])
    today   = ohlcv[-1]
    near_ma = abs(today["low"] - ma20) / ma20 <= 0.015  # 低點距 MA20 ≤ 1.5%
    strong  = today["close"] >= high30 * 0.80            # 離30日高不超過20%
    up_day  = today["close"] > today["open"]
    if near_ma and strong and up_day:
        return True, f"觸及 MA20({ma20:.1f}) 支撐，離30日高 {(today['close']/high30-1)*100:+.1f}%"
    return False, ""


# ── 策略 6：週線強勢（5 日累積漲幅 > 3% + 今日量縮收紅）─────────────

def strategy_weekly_momentum(ohlcv: list) -> tuple[bool, str]:
    """
    過去5日累積漲幅超過 3%（週線強勢），
    今日量縮（< 0.8x 均量）且收紅（量縮漲，機構惜售）。
    """
    if len(ohlcv) < 27:
        return False, ""
    price_5d_ago = ohlcv[-6]["close"]
    today        = ohlcv[-1]
    week_gain    = (today["close"] - price_5d_ago) / price_5d_ago * 100
    vols         = [r["volume"] for r in ohlcv[-22:-1] if r.get("volume", 0) > 0]
    avg_vol      = sum(vols) / len(vols) if vols else 0
    vol_ratio    = today.get("volume", 0) / avg_vol if avg_vol > 0 else 1.0
    is_up        = today["close"] > today["open"]
    if week_gain >= 3.0 and vol_ratio <= 0.8 and is_up:
        return True, f"週線強勢 +{week_gain:.1f}%，今日量縮({vol_ratio:.2f}x)收紅"
    return False, ""


# ── 策略字典（順序決定報告呈現順序）────────────────────────────────

ALL_STRATEGIES: dict[str, tuple[str, callable]] = {
    "momentum":         ("📈 動量突破（N日新高）",   strategy_momentum),
    "ma_crossover":     ("⚡ 均線黃金交叉",           strategy_ma_crossover),
    "rsi_oversold":     ("🔄 RSI超賣反彈",            strategy_rsi_oversold),
    "volume_surge":     ("💥 爆量強勢收紅",           strategy_volume_surge),
    "pullback_ma20":    ("💪 強勢回檔 MA20 支撐",     strategy_pullback_ma20),
    "weekly_momentum":  ("📊 週線強勢量縮收紅",       strategy_weekly_momentum),
}
