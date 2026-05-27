"""
warrant_bs.py — Black-Scholes 權證定價與風險參數計算

純 Python 實作（不依賴 scipy），適用台灣認購 / 認售權證。

主要函式：
  calc_iv(S, K, T, r, w_price_per_share, is_call)  → float | None
  calc_greeks(S, K, T, r, sigma, is_call)           → dict
  full_calc(S, K, days, w_price, ratio_f, is_call)  → dict
"""

import math
import os

# 台灣無風險利率（預設 2%，可用環境變數 TW_RISK_FREE 覆寫）
_DEFAULT_R = float(os.environ.get("TW_RISK_FREE", "0.02"))


# ── 標準常態分布 ──────────────────────────────────────────────────

def _ncdf(x: float) -> float:
    """標準常態累積分布函數（使用 math.erf）"""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _npdf(x: float) -> float:
    """標準常態密度函數"""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ── Black-Scholes 定價 ────────────────────────────────────────────

def bs_price(S: float, K: float, T: float, r: float,
             sigma: float, is_call: bool = True) -> float:
    """
    BS 理論價格（European）。
    S     : 標的現價
    K     : 履約價
    T     : 到期年數
    r     : 無風險利率（年化）
    sigma : 隱含波動率（年化）
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(S - K, 0) if is_call else max(K - S, 0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    else:
        return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


# ── 隱含波動率（二分搜尋）────────────────────────────────────────

def calc_iv(S: float, K: float, T: float, r: float,
            w_price_per_share: float, is_call: bool = True) -> float | None:
    """
    以二分法反推隱含波動率。
    w_price_per_share = 權證市價 / 行使比例 / 1000
                      = 每股標的對應的權證成本（元）
    回傳值為年化 IV（小數），例如 0.35 表示 35%。
    """
    if w_price_per_share <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None

    # 內在價值下限檢查
    intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if w_price_per_share < intrinsic * 0.95:
        return None   # 市價低於內在價值，資料可能有誤

    lo, hi = 0.001, 8.0
    for _ in range(120):
        mid = (lo + hi) * 0.5
        p   = bs_price(S, K, T, r, mid, is_call)
        if p > w_price_per_share:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-5:
            break

    iv = (lo + hi) * 0.5
    return iv if 0.005 < iv < 7.99 else None


# ── 希臘字母 ──────────────────────────────────────────────────────

def calc_greeks(S: float, K: float, T: float, r: float,
                sigma: float, is_call: bool = True) -> dict:
    """
    計算 Delta / Gamma / Theta / Vega。
    Theta 單位：每日損耗（元/每標的股）
    Vega  單位：IV 上升 1% 時的價格變化（元/每標的股）
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {}
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        pdf_d1 = _npdf(d1)

        delta = _ncdf(d1) if is_call else _ncdf(d1) - 1.0
        gamma = pdf_d1 / (S * sigma * math.sqrt(T))

        if is_call:
            theta = (-(S * pdf_d1 * sigma) / (2.0 * math.sqrt(T))
                     - r * K * math.exp(-r * T) * _ncdf(d2)) / 365.0
        else:
            theta = (-(S * pdf_d1 * sigma) / (2.0 * math.sqrt(T))
                     + r * K * math.exp(-r * T) * _ncdf(-d2)) / 365.0

        vega = S * pdf_d1 * math.sqrt(T) / 100.0   # per 1% IV change

        return {
            "delta": round(delta, 4),
            "gamma": round(gamma, 6),
            "theta": round(theta, 4),
            "vega":  round(vega, 4),
        }
    except Exception:
        return {}


# ── 一站式完整計算 ────────────────────────────────────────────────

def full_calc(S: float, K: float, days: int, w_price: float,
              ratio_f: float, is_call: bool = True,
              r: float = _DEFAULT_R) -> dict:
    """
    輸入現實數值，回傳完整計算結果。

    S       : 標的現價（元）
    K       : 履約價（元）
    days    : 剩餘天數
    w_price : 權證現價（元/千單位）
    ratio_f : 行使比例（每千單位可換幾股，例如 0.1）
    is_call : True = 認購，False = 認售

    回傳 dict：
        iv         : 隱含波動率（%，字串）
        delta      : Delta（浮點）
        gamma      : Gamma
        theta      : Theta / 日
        vega       : Vega / 1% IV
        theoretical: 理論價格（元/千單位）
        premium_pct: 溢價率（%）
        leverage   : 有效槓桿（倍）
        moneyness  : 價內外程度（%，正 = 價內）
        status     : anomaly flag (str)
    """
    result: dict = {}
    if S <= 0 or K <= 0 or days <= 0 or ratio_f <= 0 or w_price <= 0:
        return result

    T = days / 365.0
    w_per_share = w_price / ratio_f   # 每股對應的權證成本

    # ── 隱含波動率 ──────────────────────────────────────────────
    iv = calc_iv(S, K, T, r, w_per_share, is_call)
    if iv is not None:
        result["iv"] = round(iv * 100, 2)   # convert to %
        g = calc_greeks(S, K, T, r, iv, is_call)
        result.update(g)

        # 理論價格（元/千單位 = per w_price unit）
        theo_per_share = bs_price(S, K, T, r, iv, is_call)
        result["theoretical"] = round(theo_per_share * ratio_f, 4)

        # 有效槓桿 = |Delta| × S / w_price × ratio_f
        if result.get("delta") and w_price > 0:
            eff_lev = abs(result["delta"]) * S / w_per_share
            result["leverage"] = round(eff_lev, 2)

    # ── 溢價率（不依賴 IV，直接計算）────────────────────────────
    prem = (w_per_share + K - S) / S * 100 if is_call else (w_per_share + S - K) / S * 100
    result["premium_pct"] = round(prem, 2)

    # ── 價內外程度 ───────────────────────────────────────────────
    moneyness = (S - K) / K * 100 if is_call else (K - S) / K * 100
    result["moneyness"] = round(moneyness, 2)

    # ── 異常標記 ─────────────────────────────────────────────────
    flags = []
    if prem > 30:
        flags.append(f"溢價過高({prem:.0f}%)")
    if prem < -5:
        flags.append(f"折價異常({prem:.0f}%)")
    if iv and iv > 2.0:
        flags.append(f"IV異常({iv*100:.0f}%)")
    if days <= 30:
        flags.append(f"即將到期({days}天)")
    result["anomaly"] = "、".join(flags) if flags else ""

    return result
