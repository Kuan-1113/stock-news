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


def _calc_macd(
    closes: list[float],
    fast: int = 12, slow: int = 26, sig: int = 9,
) -> tuple:
    """
    計算 MACD（指數平滑異同移動平均）。
    回傳 (dif_today, signal_today, dif_yesterday, signal_yesterday)
    任一項 None 代表資料不足。
    """
    if len(closes) < slow + sig + 1:
        return None, None, None, None

    k12 = 2 / (fast + 1)
    k26 = 2 / (slow + 1)
    k9  = 2 / (sig + 1)

    # Seed EMA from first bars
    ema12 = sum(closes[:fast]) / fast
    ema26 = sum(closes[:slow]) / slow

    # Build DIF series（每根 bar 都計算一個 DIF）
    dif_list: list[float] = []
    for price in closes[slow:]:
        ema12 = price * k12 + ema12 * (1 - k12)
        ema26 = price * k26 + ema26 * (1 - k26)
        dif_list.append(ema12 - ema26)

    if len(dif_list) < sig + 1:
        return None, None, None, None

    # Signal：昨日（用 dif_list[:-1] 算到底），今日再進一步延伸
    sig_y = sum(dif_list[:sig]) / sig
    for d in dif_list[sig:-1]:
        sig_y = d * k9 + sig_y * (1 - k9)

    sig_t = dif_list[-1] * k9 + sig_y * (1 - k9)

    return dif_list[-1], sig_t, dif_list[-2], sig_y


def _calc_kd(ohlcv: list, period: int = 9) -> tuple:
    """
    計算 KD 指標（隨機指標，Williams 公式）。
    回傳 (K_today, D_today, K_yesterday, D_yesterday)
    任一項 None 代表資料不足。
    """
    if len(ohlcv) < period + 1:
        return None, None, None, None

    K, D = 50.0, 50.0
    K_prev = D_prev = 50.0

    for i in range(period - 1, len(ohlcv)):
        window  = ohlcv[i - period + 1: i + 1]
        highest = max(r["high"] for r in window)
        lowest  = min(r["low"]  for r in window)
        close   = ohlcv[i]["close"]

        if highest == lowest:
            rsv = 50.0
        else:
            rsv = (close - lowest) / (highest - lowest) * 100

        K_prev, D_prev = K, D
        K = K * (2 / 3) + rsv * (1 / 3)
        D = D * (2 / 3) + K   * (1 / 3)

    return K, D, K_prev, D_prev


def _adx(ohlcv: list, period: int = 14) -> float:
    """
    計算 ADX（平均趨向指標，Wilder 平滑法）。
    ADX > 25 → 趨勢中；ADX ≤ 25 → 震盪盤。
    """
    if len(ohlcv) < period * 2 + 2:
        return 0.0

    plus_dms: list[float] = []
    minus_dms: list[float] = []
    trs: list[float] = []

    for i in range(1, len(ohlcv)):
        h_diff = ohlcv[i]["high"]  - ohlcv[i-1]["high"]
        l_diff = ohlcv[i-1]["low"] - ohlcv[i]["low"]
        plus_dms.append( h_diff if (h_diff > l_diff and h_diff > 0) else 0.0)
        minus_dms.append(l_diff if (l_diff > h_diff and l_diff > 0) else 0.0)
        trs.append(max(
            ohlcv[i]["high"] - ohlcv[i]["low"],
            abs(ohlcv[i]["high"] - ohlcv[i-1]["close"]),
            abs(ohlcv[i]["low"]  - ohlcv[i-1]["close"]),
        ))

    def _wilder(arr: list, n: int) -> list:
        """Wilder 指數平滑（α = 1/n）"""
        result = [sum(arr[:n])]
        for x in arr[n:]:
            result.append(result[-1] - result[-1] / n + x)
        return result

    sp = _wilder(plus_dms,  period)
    sm = _wilder(minus_dms, period)
    st = _wilder(trs,        period)

    dxs: list[float] = []
    for a, b, t in zip(sp, sm, st):
        if t == 0:
            continue
        di_p = 100 * a / t
        di_m = 100 * b / t
        s    = di_p + di_m
        if s == 0:
            continue
        dxs.append(100 * abs(di_p - di_m) / s)

    if len(dxs) < period:
        return 0.0
    return round(sum(dxs[-period:]) / period, 2)


def get_market_regime(ohlcv: list, threshold: float = 25.0) -> str:
    """
    使用大盤（TWII）OHLCV 判斷市場狀態。

    ADX >= threshold → "trend"   趨勢市，適合動量策略
    ADX <  threshold → "range"   震盪盤，適合均值回歸策略
    資料不足          → "unknown" 兩類策略均啟用（保守策略）
    """
    if len(ohlcv) < 30:
        return "unknown"
    window = ohlcv[-60:] if len(ohlcv) >= 60 else ohlcv
    adx = _adx(window)
    regime = "trend" if adx >= threshold else "range"
    print(f"  📐 大盤 ADX({len(window)}根) = {adx:.1f} → {'趨勢市 📈' if regime == 'trend' else '震盪盤 📉'}")
    return regime


# ── 市場狀態 × 策略對應表 ──────────────────────────────────────────
# 趨勢市（ADX≥25）適合追動能，震盪盤（ADX<25）適合低接均值回歸。
# 籌碼派策略不受市場狀態限制（外資/投信連買在任何市況均有效）。

TREND_STRATEGIES: frozenset = frozenset({
    # 起漲初期（趨勢市 + 震盪盤皆適用，但放在趨勢市優先）
    "volume_buildup",       # 底部量縮後放量突破
    "ma5_cross_fresh",      # MA5 剛突破 MA20
    "flat_base_breakout",   # 低檔平台突破
    # 動量確認（趨勢市）
    "momentum",             # 動量突破（創 N 日新高）
    "ma_crossover",         # 均線黃金交叉
    "volume_surge",         # 爆量強勢收紅
    "weekly_momentum",      # 週線強勢量縮收紅
    "bollinger_breakout",   # 布林通道突破（放量）
    "macd_golden",          # MACD 金叉
    "52w_high",             # 52 週新高突破
})

MEAN_REVERSION_STRATEGIES: frozenset = frozenset({
    "rsi_oversold",   # RSI 超賣反彈
    "pullback_ma20",  # 強勢股回檔至 MA20 支撐
    "kd_oversold",    # KD 超賣區金叉
})


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


# ── 策略 7：布林通道突破（Bollinger Band Breakout）──────────────────

def strategy_bollinger_breakout(ohlcv: list) -> tuple[bool, str]:
    """
    收盤突破布林上軌（MA20 + 2σ），且成交量 ≥ 1.5x 20 日均量。
    ⚠️ 加入「已漲過濾」：近 20 日漲幅 > 20% 的跳過（避免追高）。
    """
    if len(ohlcv) < 22:
        return False, ""

    closes = [r["close"] for r in ohlcv]
    ma20   = _ma(closes, 20)
    if ma20 is None:
        return False, ""

    # 已漲過濾：近 20 天從最低點漲超過 20% → 已飆股，跳過
    low_20  = min(r["low"] for r in ohlcv[-20:])
    recent_gain = (closes[-1] - low_20) / low_20 if low_20 > 0 else 0
    if recent_gain > 0.20:
        return False, ""

    # 標準差（用近 20 日）
    window_c = closes[-20:]
    std20 = (sum((c - ma20) ** 2 for c in window_c) / 20) ** 0.5
    upper = ma20 + 2 * std20

    # 成交量
    vols    = [r["volume"] for r in ohlcv[-21:-1] if r.get("volume", 0) > 0]
    avg_vol = sum(vols) / len(vols) if vols else 0

    today     = ohlcv[-1]
    vol_ratio = today.get("volume", 0) / avg_vol if avg_vol > 0 else 0.0

    if today["close"] > upper and vol_ratio >= 1.5:
        return True, (
            f"收盤 {today['close']:.1f} 突破布林上軌 {upper:.1f}"
            f"（MA20={ma20:.1f} ±{std20:.1f}），放量 {vol_ratio:.1f}x，近20日漲{recent_gain:.1%}"
        )
    return False, ""


# ── 策略 8：MACD 金叉 ──────────────────────────────────────────────

def strategy_macd_golden(ohlcv: list) -> tuple[bool, str]:
    """
    DIF 由下方穿越 Signal Line（MACD 金叉）。
    昨日 DIF < Signal，今日 DIF >= Signal。
    趨勢轉強的經典訊號。
    """
    if len(ohlcv) < 36:   # slow(26) + sig(9) + 1
        return False, ""

    closes = [r["close"] for r in ohlcv]
    dif_t, sig_t, dif_y, sig_y = _calc_macd(closes)

    if any(v is None for v in (dif_t, sig_t, dif_y, sig_y)):
        return False, ""

    if dif_y < sig_y and dif_t >= sig_t:
        return True, (
            f"MACD 金叉：DIF {dif_t:+.4f} 穿越 Signal {sig_t:+.4f}"
            f"（昨 DIF={dif_y:+.4f}）"
        )
    return False, ""


# ── 策略 9：KD 超賣區金叉 ──────────────────────────────────────────

def strategy_kd_oversold(ohlcv: list) -> tuple[bool, str]:
    """
    K 值從低於 D 值的超賣區（K < 30）向上穿越 D 值（KD 金叉）。
    昨日 K < D，今日 K >= D，且今日 K < 30（確認超賣區）。
    震盪市的低接反彈信號。
    """
    if len(ohlcv) < 12:
        return False, ""

    K_t, D_t, K_y, D_y = _calc_kd(ohlcv)

    if any(v is None for v in (K_t, D_t, K_y, D_y)):
        return False, ""

    if K_y < D_y and K_t >= D_t and K_t < 30:
        return True, (
            f"KD 超賣金叉：K={K_t:.1f} 穿越 D={D_t:.1f}"
            f"（昨 K={K_y:.1f}，超賣區確認）"
        )
    return False, ""


# ── 策略 10：52 週新高突破 ─────────────────────────────────────────

def strategy_52w_high(ohlcv: list) -> tuple[bool, str]:
    """
    收盤價突破過去 252 個交易日（約 1 年）的最高點。
    52 週新高是最強的動能信號之一，機構追高意願強。
    資料不足 253 根時，退而使用全部可用資料（至少 22 根）。
    """
    if len(ohlcv) < 22:
        return False, ""

    today  = ohlcv[-1]
    # 使用最多 252 根歷史（不含今日）
    lookback = ohlcv[-253:-1] if len(ohlcv) >= 253 else ohlcv[:-1]
    high_52w = max(r["high"] for r in lookback)
    days_used = len(lookback)

    if today["close"] > high_52w:
        label = "52週" if days_used >= 200 else f"{days_used}日"
        return True, (
            f"收盤 {today['close']:.1f} 突破{label}新高 {high_52w:.1f}"
            f"（{days_used} 個交易日）"
        )
    return False, ""


# ── 策略 11：底部量縮整理後放量突破（起漲初期）────────────────────

def strategy_volume_buildup(ohlcv: list) -> tuple[bool, str]:
    """
    量縮整理後放量突破 — 捕捉「起漲第一天」。

    邏輯：股票先縮量整理（賣盤消化），今日突然放量且收紅突破 MA5。
    關鍵過濾：
      1. 近 5 天量能低於 10 日均量（確認有整理縮量）
      2. 今日量 ≥ 10 日均量 1.5x（放量突破）
      3. 今日收盤 > MA5（多方站穩）
      4. RSI 在 35-60 之間（未超買，說明漲幅還很小）
      5. 近 15 天最大漲幅 < 12%（未飆過）
    """
    if len(ohlcv) < 22:
        return False, ""

    closes  = [r["close"] for r in ohlcv]
    vols    = [r.get("volume", 0) for r in ohlcv]
    today   = ohlcv[-1]

    # 近 10 日均量（不含今日）
    avg_vol10 = sum(vols[-11:-1]) / 10 if sum(vols[-11:-1]) > 0 else 0
    if avg_vol10 == 0:
        return False, ""

    # 近 5 日均量（不含今日）→ 是否呈縮量
    avg_vol5 = sum(vols[-6:-1]) / 5
    vol_contracting = avg_vol5 < avg_vol10 * 0.9   # 近 5 日量比前 10 日少 10%

    # 今日放量
    vol_today   = vols[-1]
    vol_ratio   = vol_today / avg_vol10 if avg_vol10 > 0 else 0

    # MA5
    ma5 = _ma(closes, 5)
    if ma5 is None:
        return False, ""

    # RSI
    rsi = _rsi(closes)
    if rsi is None:
        return False, ""

    # 近 15 天最大漲幅（從最低點計算）
    low_15      = min(r["low"] for r in ohlcv[-15:])
    recent_gain = (closes[-1] - low_15) / low_15 if low_15 > 0 else 1

    # 今日收紅
    price_up = today["close"] > today["open"]

    if (vol_contracting
            and vol_ratio >= 1.5
            and today["close"] > ma5
            and 35 <= rsi <= 62
            and recent_gain < 0.12
            and price_up):
        return True, (
            f"底部縮量後放量突破 MA5（量比 {vol_ratio:.1f}x，RSI={rsi:.0f}，"
            f"近15日漲幅僅 {recent_gain:.1%}）"
        )
    return False, ""


# ── 策略 12：MA5 剛突破 MA20（均線多頭排列剛形成）──────────────────

def strategy_ma5_cross_ma20_fresh(ohlcv: list) -> tuple[bool, str]:
    """
    MA5 剛穿越 MA20（今日站上，昨日在下）— 趨勢剛翻多的第一天。

    與 strategy_ma_crossover 的差別：
      - ma_crossover 用 MA5/MA20（今日金叉即觸發，包含已漲一段後的金叉）
      - 本策略額外要求：
        1. 近 20 天漲幅 < 15%（剛起漲，不是金叉後已漲很多）
        2. 成交量有放大（確認突破有效）
        3. MA20 本身也是上升趨勢（確保方向正確）
    """
    if len(ohlcv) < 25:
        return False, ""

    closes = [r["close"] for r in ohlcv]
    vols   = [r.get("volume", 0) for r in ohlcv]

    ma5_t  = _ma(closes, 5)          # 今日 MA5
    ma5_y  = _ma(closes, 5, offset=1) # 昨日 MA5
    ma20_t = _ma(closes, 20)
    ma20_y = _ma(closes, 20, offset=1)

    if any(v is None for v in (ma5_t, ma5_y, ma20_t, ma20_y)):
        return False, ""

    # 今日 MA5 剛穿越 MA20（昨日在下，今日在上）
    fresh_cross = ma5_y <= ma20_y and ma5_t > ma20_t
    if not fresh_cross:
        return False, ""

    # MA20 本身呈上升趨勢（今日 MA20 > 5 天前 MA20）
    ma20_5d_ago = _ma(closes, 20, offset=5)
    ma20_rising = ma20_5d_ago is None or ma20_t >= ma20_5d_ago

    # 成交量放大（今日 > 10 日均量的 1.1x）
    avg_vol10 = sum(vols[-11:-1]) / 10 if sum(vols[-11:-1]) > 0 else 0
    vol_ok    = avg_vol10 > 0 and vols[-1] >= avg_vol10 * 1.1

    # 近 20 天漲幅限制（防止金叉後已大漲）
    low_20      = min(r["low"] for r in ohlcv[-20:])
    recent_gain = (closes[-1] - low_20) / low_20 if low_20 > 0 else 1

    if ma20_rising and vol_ok and recent_gain < 0.15:
        return True, (
            f"MA5（{ma5_t:.1f}）剛突破 MA20（{ma20_t:.1f}）均線金叉初期，"
            f"近20日漲幅 {recent_gain:.1%}（尚未追高）"
        )
    return False, ""


# ── 策略 13：低檔整理平台突破（缺口或平台上緣突破）────────────────

def strategy_flat_base_breakout(ohlcv: list) -> tuple[bool, str]:
    """
    低檔橫盤整理後突破平台上緣 — 機構在悄悄累積，突破才露臉。

    判斷邏輯：
      1. 近 15 天高低差 < 8%（平台整理，沒有大波動）
      2. 今日突破 15 天高點
      3. 成交量放大（突破有效）
      4. 整理前沒有大漲（近 30 天漲幅 < 20%）
      5. 股價仍在合理位置（RSI < 65）
    """
    if len(ohlcv) < 32:
        return False, ""

    closes = [r["close"] for r in ohlcv]
    vols   = [r.get("volume", 0) for r in ohlcv]

    # 近 15 天（不含今日）的整理範圍
    base_window = ohlcv[-16:-1]
    base_high   = max(r["high"]  for r in base_window)
    base_low    = min(r["low"]   for r in base_window)
    base_range  = (base_high - base_low) / base_low if base_low > 0 else 1

    # 必須是平台整理（高低差 < 8%）
    if base_range >= 0.08:
        return False, ""

    today = ohlcv[-1]

    # 今日突破平台高點
    if today["close"] <= base_high:
        return False, ""

    # 成交量放大
    avg_vol10 = sum(vols[-11:-1]) / 10 if sum(vols[-11:-1]) > 0 else 0
    vol_ratio = vols[-1] / avg_vol10 if avg_vol10 > 0 else 0
    if vol_ratio < 1.3:
        return False, ""

    # 近 30 天漲幅限制
    low_30      = min(r["low"] for r in ohlcv[-30:])
    recent_gain = (closes[-1] - low_30) / low_30 if low_30 > 0 else 1
    if recent_gain > 0.20:
        return False, ""

    # RSI 不過熱
    rsi = _rsi(closes)
    if rsi and rsi > 65:
        return False, ""

    return True, (
        f"低檔平台（{base_range:.1%}震幅）突破上緣 {base_high:.1f}，"
        f"放量 {vol_ratio:.1f}x，近30日漲幅 {recent_gain:.1%}"
    )


# ── 策略字典（順序決定報告呈現順序）────────────────────────────────

ALL_STRATEGIES: dict[str, tuple[str, callable]] = {
    # ── 起漲初期策略（新增，捕捉尚未大漲的股票）─────────────────
    "volume_buildup":       ("🌱 底部放量突破（起漲初期）", strategy_volume_buildup),
    "ma5_cross_fresh":      ("🚦 MA5剛突破MA20（趨勢翻多）", strategy_ma5_cross_ma20_fresh),
    "flat_base_breakout":   ("📦 低檔平台整理突破",         strategy_flat_base_breakout),
    # ── 動量確認策略（已有漲勢，確認趨勢延續）───────────────────
    "momentum":             ("📈 動量突破（N日新高）",       strategy_momentum),
    "ma_crossover":         ("⚡ 均線黃金交叉",               strategy_ma_crossover),
    "rsi_oversold":         ("🔄 RSI超賣反彈",               strategy_rsi_oversold),
    "volume_surge":         ("💥 爆量強勢收紅",               strategy_volume_surge),
    "pullback_ma20":        ("💪 強勢回檔 MA20 支撐",         strategy_pullback_ma20),
    "weekly_momentum":      ("📊 週線強勢量縮收紅",           strategy_weekly_momentum),
    "bollinger_breakout":   ("🎯 布林突破（已漲濾除）",       strategy_bollinger_breakout),
    "macd_golden":          ("📉 MACD 金叉",                  strategy_macd_golden),
    "kd_oversold":          ("🌀 KD 超賣金叉",                strategy_kd_oversold),
    "52w_high":             ("🏔️ 52週新高突破",               strategy_52w_high),
}


# ── 籌碼派策略（輸入：chip_history list，由舊到新）─────────────────
# chip_history 每筆格式：
#   {"date": str, "foreign_net": int, "trust_net": int,
#    "dealer_net": int, "total_net": int}
# 單位與 TWSE 資料相同（通常為千股，正 = 買超，負 = 賣超）

def strategy_chip_foreign(chip_hist: list) -> tuple[bool, str]:
    """
    外資連續淨買超 3 個交易日以上。
    台股最重要的籌碼訊號，外資連買通常代表有基本面或產業邏輯支撐。
    """
    if len(chip_hist) < 3:
        return False, ""

    # 計算從最近一天往回算連續買超天數
    consecutive = 0
    for rec in reversed(chip_hist):
        if rec["foreign_net"] > 0:
            consecutive += 1
        else:
            break

    if consecutive >= 3:
        recent3_total = sum(r["foreign_net"] for r in chip_hist[-3:])
        today_val     = chip_hist[-1]["foreign_net"]
        return True, (
            f"外資連買 {consecutive} 日"
            f"，今日 {today_val:+,}（近3日合計 {recent3_total:+,}）"
        )
    return False, ""


def strategy_chip_trust(chip_hist: list) -> tuple[bool, str]:
    """
    投信連續淨買超 3 個交易日以上。
    投信連買常出現在月底作帳或法人佈局早期，短線上漲概率高。
    """
    if len(chip_hist) < 3:
        return False, ""

    consecutive = 0
    for rec in reversed(chip_hist):
        if rec["trust_net"] > 0:
            consecutive += 1
        else:
            break

    if consecutive >= 3:
        recent3_total = sum(r["trust_net"] for r in chip_hist[-3:])
        today_val     = chip_hist[-1]["trust_net"]
        return True, (
            f"投信連買 {consecutive} 日"
            f"，今日 {today_val:+,}（近3日合計 {recent3_total:+,}）"
        )
    return False, ""


def strategy_chip_dual(chip_hist: list) -> tuple[bool, str]:
    """
    外資 + 投信今日同步淨買超（雙主力同向）。
    外資和投信同時買是非常罕見的強力信號，籌碼高度集中。
    """
    if not chip_hist:
        return False, ""

    today = chip_hist[-1]
    if today["foreign_net"] > 0 and today["trust_net"] > 0:
        return True, (
            f"外資+投信同買：外資 {today['foreign_net']:+,}"
            f" / 投信 {today['trust_net']:+,}"
            f" / 合計 {today['total_net']:+,}"
        )
    return False, ""


# ── 籌碼策略字典 ──────────────────────────────────────────────────

CHIP_STRATEGIES: dict[str, tuple[str, callable]] = {
    "chip_foreign": ("🏛️ 外資連買3日+",     strategy_chip_foreign),
    "chip_trust":   ("📦 投信連買3日+",     strategy_chip_trust),
    "chip_dual":    ("🔗 外資+投信同買",    strategy_chip_dual),
}

# 所有策略合集（指標 + 籌碼），供統計用
ALL_STRATEGIES_COMBINED = {**ALL_STRATEGIES, **CHIP_STRATEGIES}

# ── 公開 API ──────────────────────────────────────────────────────
__all__ = [
    "ALL_STRATEGIES", "CHIP_STRATEGIES", "ALL_STRATEGIES_COMBINED",
    "TREND_STRATEGIES", "MEAN_REVERSION_STRATEGIES",
    "get_market_regime",
]
