"""
technical_indicators.py
技術指標計算模組

提供：MA5/10/20/60、MACD、RSI、KDJ、乖離率、成交量分析
      融資券（TWSE API）、三大法人買賣超（TWSE API）

使用方式：
    from technical_indicators import get_full_indicators, format_indicators_for_prompt, format_indicators_for_discord
    ind = get_full_indicators("2330.TW")
"""

import datetime
import time
import warnings
import requests
import pytz

warnings.filterwarnings("ignore")

TW_TZ = pytz.timezone("Asia/Taipei")


# ─────────────────────────────────────────────────────────────
# 基礎計算工具
# ─────────────────────────────────────────────────────────────

def _calc_ema(prices: list, period: int) -> list:
    k = 2 / (period + 1)
    emas = [prices[0]]
    for p in prices[1:]:
        emas.append(p * k + emas[-1] * (1 - k))
    return emas


def calc_macd(closes: list) -> dict:
    """MACD (12, 26, 9)"""
    if len(closes) < 27:
        return {"macd": None, "signal": None, "hist": None, "trend": "資料不足", "golden_cross": False, "dead_cross": False}

    ema12 = _calc_ema(closes, 12)
    ema26 = _calc_ema(closes, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal_line = _calc_ema(macd_line[25:], 9)
    hist = [m - s for m, s in zip(macd_line[25:], signal_line)]

    curr_hist = hist[-1]
    prev_hist = hist[-2] if len(hist) >= 2 else 0
    curr_macd = macd_line[-1]
    curr_sig  = signal_line[-1]
    prev_macd = macd_line[-2] if len(macd_line) >= 2 else curr_macd
    prev_sig  = signal_line[-2] if len(signal_line) >= 2 else curr_sig

    if curr_hist > 0 and curr_hist > prev_hist:
        trend = "多頭擴張 📈"
    elif curr_hist > 0:
        trend = "多頭收斂 ⚠️"
    elif curr_hist < 0 and curr_hist < prev_hist:
        trend = "空頭擴張 📉"
    else:
        trend = "空頭收斂 🔄"

    return {
        "macd":         round(curr_macd, 4),
        "signal":       round(curr_sig,  4),
        "hist":         round(curr_hist, 4),
        "trend":        trend,
        "golden_cross": (curr_macd > curr_sig and prev_macd <= prev_sig),
        "dead_cross":   (curr_macd < curr_sig and prev_macd >= prev_sig),
    }


def calc_rsi(closes: list, period: int = 14) -> dict:
    """RSI"""
    if len(closes) < period + 2:
        return {"rsi": None, "signal": "資料不足"}

    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        rsi = 100.0
    else:
        rsi = 100 - 100 / (1 + avg_gain / avg_loss)

    if rsi >= 80:
        signal = "嚴重超買 🔴"
    elif rsi >= 70:
        signal = "超買區 ⚠️"
    elif rsi <= 20:
        signal = "嚴重超賣 🟢"
    elif rsi <= 30:
        signal = "超賣區 🔄"
    else:
        signal = "中性區間"

    return {"rsi": round(rsi, 2), "signal": signal}


def calc_kdj(highs: list, lows: list, closes: list, period: int = 9) -> dict:
    """KDJ (9日)"""
    if len(closes) < period:
        return {"k": None, "d": None, "j": None, "signal": "資料不足"}

    rsv_list = []
    for i in range(period - 1, len(closes)):
        ph = highs[i - period + 1: i + 1]
        pl = lows[i  - period + 1: i + 1]
        hi, lo = max(ph), min(pl)
        rsv_list.append((closes[i] - lo) / (hi - lo) * 100 if hi != lo else 50.0)

    k_vals, d_vals = [50.0], [50.0]
    for rsv in rsv_list:
        k = k_vals[-1] * 2 / 3 + rsv * 1 / 3
        d = d_vals[-1] * 2 / 3 + k   * 1 / 3
        k_vals.append(k)
        d_vals.append(d)

    k_vals, d_vals = k_vals[1:], d_vals[1:]
    j_vals = [3 * k - 2 * d for k, d in zip(k_vals, d_vals)]

    ck, cd, cj = k_vals[-1], d_vals[-1], j_vals[-1]
    pk, pd     = (k_vals[-2], d_vals[-2]) if len(k_vals) >= 2 else (ck, cd)

    golden = ck > cd and pk <= pd
    dead   = ck < cd and pk >= pd

    if cj > 100:
        signal = "J 超買區"
    elif cj < 0:
        signal = "J 超賣區"
    elif golden:
        signal = "KD 黃金交叉 📈"
    elif dead:
        signal = "KD 死亡交叉 📉"
    elif ck > 80:
        signal = "超買區"
    elif ck < 20:
        signal = "超賣區"
    else:
        signal = "中性"

    return {"k": round(ck, 2), "d": round(cd, 2), "j": round(cj, 2), "signal": signal}


def calc_bias(closes: list) -> dict:
    """乖離率"""
    result = {}
    for p in [5, 10, 20, 60]:
        if len(closes) >= p:
            ma = sum(closes[-p:]) / p
            result[f"bias{p}"] = round((closes[-1] - ma) / ma * 100, 2)
        else:
            result[f"bias{p}"] = None
    return result


def calc_volume(volumes: list) -> dict:
    """成交量分析"""
    clean = [v for v in volumes if v is not None and v > 0]
    if len(clean) < 2:
        return {"curr_vol": None, "vol_ma5": None, "vol_ratio": None, "vol_trend": "資料不足"}

    curr  = clean[-1]
    ma5   = sum(clean[-5:]) / min(5, len(clean))
    ratio = round(curr / ma5, 2) if ma5 > 0 else 1.0

    if ratio >= 2.0:
        trend = "爆量 🔥"
    elif ratio >= 1.5:
        trend = "量增 ⬆️"
    elif ratio <= 0.5:
        trend = "極度縮量 ⬇️"
    elif ratio <= 0.7:
        trend = "縮量"
    else:
        trend = "量持平"

    return {
        "curr_vol":  int(curr),
        "vol_ma5":   int(ma5),
        "vol_ratio": ratio,
        "vol_trend": trend,
    }


# ─────────────────────────────────────────────────────────────
# 基本面（yfinance info）
# ─────────────────────────────────────────────────────────────

def fetch_stock_fundamentals(symbol: str) -> dict:
    """從 yfinance 取得基本面數據（PE/PB/EPS/ROE/殖利率）"""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        result = {}
        for key, src in [
            ("pe",         "trailingPE"),
            ("forward_pe", "forwardPE"),
            ("pb",         "priceToBook"),
            ("eps",        "trailingEps"),
            ("roe",        "returnOnEquity"),
            ("div_yield",  "dividendYield"),
            ("profit_margin", "profitMargins"),
            ("revenue_growth","revenueGrowth"),
        ]:
            v = info.get(src)
            if v is not None:
                result[key] = round(float(v), 4)
        return result
    except Exception as e:
        print(f"基本面 API 錯誤 {symbol}: {e}")
        return {}


# ─────────────────────────────────────────────────────────────
# 台指期（TAIFEX Open API）
# ─────────────────────────────────────────────────────────────

def fetch_taifex_txf_5d() -> list:
    """台指期近5個交易日：收盤價、現貨（^TWII）、正逆價差、未平倉量
    資料來源：FinMind（TaiwanFuturesDaily TX）+ yfinance ^TWII
    """
    # 1. 取 ^TWII spot 近期收盤
    spot_map: dict[str, float] = {}
    try:
        import yfinance as yf
        twii = yf.Ticker("^TWII").history(period="20d")
        for ts, row in twii.iterrows():
            spot_map[ts.strftime("%Y-%m-%d")] = round(float(row["Close"]), 2)
    except Exception as e:
        print(f"^TWII 取得失敗: {e}")

    # 2. FinMind TaiwanFuturesDaily（TX 台指期，免費公開 API）
    try:
        start = (datetime.datetime.now(TW_TZ) - datetime.timedelta(days=20)).strftime("%Y-%m-%d")
        end   = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
        r = requests.get(
            "https://api.finmindtrade.com/api/v4/data"
            f"?dataset=TaiwanFuturesDaily&data_id=TX&start_date={start}&end_date={end}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
        )
        if r.status_code != 200:
            print(f"FinMind API 失敗: {r.status_code}")
            return []
        raw = r.json().get("data", [])
    except Exception as e:
        print(f"FinMind 取得失敗: {e}")
        return []

    # 3. 每日取成交量最大的近月合約
    from collections import defaultdict
    by_date: dict[str, list] = defaultdict(list)
    for rec in raw:
        if rec.get("volume", 0) > 0 and rec.get("close", 0) > 0:
            by_date[rec["date"]].append(rec)

    results = []
    for d in sorted(by_date.keys(), reverse=True):
        if len(results) >= 5:
            break
        front = max(by_date[d], key=lambda x: x.get("volume", 0))
        futures_close = round(float(front["close"]), 2)
        oi            = int(front.get("open_interest", 0))
        spot          = spot_map.get(d, 0)
        basis         = round(futures_close - spot, 2) if spot else None
        results.append({
            "date":          d,
            "futures_close": futures_close,
            "spot_close":    spot,
            "basis":         basis,
            "basis_type":    ("正價差" if basis and basis > 0
                              else ("逆價差" if basis and basis < 0 else "平價")),
            "oi":            f"{oi:,}",
        })

    return results  # newest first


# ─────────────────────────────────────────────────────────────
# TWSE 融資券 + 三大法人
# ─────────────────────────────────────────────────────────────

def _last_trading_dates(n: int = 6) -> list:
    """回傳最近 n 個可能的交易日（字串 YYYYMMDD，不保證真為交易日）"""
    today = datetime.datetime.now(TW_TZ)
    dates = []
    d = today
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y%m%d"))
        d -= datetime.timedelta(days=1)
    return dates


def fetch_twse_margin(stock_no: str) -> dict:
    """融資券餘額（TWSE API）"""
    for date_str in _last_trading_dates():
        try:
            url = (
                f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
                f"?date={date_str}&stockNo={stock_no}&response=json"
            )
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            if data.get("stat") != "OK" or not data.get("data"):
                continue
            row = data["data"][-1]
            # 欄位：日期,融資買進,融資賣出,融資現金還款,融資餘額,融資限額,融券賣出,融券買進,融券現券還款,融券餘額,融券限額,資券互抵
            return {
                "date":            row[0],
                "margin_buy":      row[1],
                "margin_sell":     row[2],
                "margin_balance":  row[4],
                "short_sell":      row[6],
                "short_buy":       row[7],
                "short_balance":   row[9],
            }
        except Exception as e:
            print(f"融資券 API 錯誤 {stock_no} {date_str}: {e}")
    return {}


def fetch_twse_institutional(stock_no: str) -> dict:
    """三大法人買賣超（TWSE API）"""
    for date_str in _last_trading_dates():
        try:
            url = (
                f"https://www.twse.com.tw/rwd/zh/fund/T86"
                f"?date={date_str}&selectType=ALLBUT0999&response=json"
            )
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if r.status_code != 200:
                continue
            data = r.json()
            if data.get("stat") != "OK" or not data.get("data"):
                continue
            for row in data["data"]:
                if str(row[0]).strip() == stock_no:
                    return {
                        "date":        date_str,
                        "foreign_net": row[4],
                        "trust_net":   row[10],
                        "dealer_net":  row[13],
                        "total_net":   row[16],
                    }
        except Exception as e:
            print(f"三大法人 API 錯誤 {stock_no} {date_str}: {e}")
    return {}


def fetch_twse_institutional_5d(stock_no: str) -> list:
    """三大法人近5個交易日買賣超。優先用 TWT38U 單股月報，失敗再逐日用 T86。"""
    # ── 嘗試 TWT38U（單股月度報表，一次取回整月資料）────────────
    try:
        today = datetime.datetime.now(TW_TZ)
        results: list = []
        for month_delta in range(2):   # 本月 + 上月（月初時可能當月資料不足）
            if len(results) >= 5:
                break
            if month_delta == 0:
                qdate = today.strftime("%Y%m%d")
            else:
                first = today.replace(day=1)
                last_m = first - datetime.timedelta(days=1)
                qdate = last_m.strftime("%Y%m%d")
            r = requests.get(
                f"https://www.twse.com.tw/rwd/zh/fund/TWT38U"
                f"?date={qdate}&stockNo={stock_no}&response=json",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            if data.get("stat") != "OK" or not data.get("data"):
                continue
            # 從最新日往前取 5 筆
            for row in reversed(data["data"]):
                if len(results) >= 5:
                    break
                if len(row) < 10:
                    continue
                try:
                    results.append({
                        "date":        row[0],
                        "foreign_net": row[3],   # 外資買賣超股數（千股）
                        "trust_net":   row[6],   # 投信買賣超股數（千股）
                        "dealer_net":  row[9],   # 自營商買賣超股數（千股）
                    })
                except (IndexError, ValueError):
                    continue
        if results:
            return results
    except Exception as e:
        print(f"TWT38U 失敗 {stock_no}，改用 T86 逐日查詢：{e}")

    # ── Fallback：T86 逐日查詢 ─────────────────────────────────
    results = []
    for date_str in _last_trading_dates(10):
        if len(results) >= 5:
            break
        try:
            r = requests.get(
                f"https://www.twse.com.tw/rwd/zh/fund/T86"
                f"?date={date_str}&selectType=ALLBUT0999&response=json",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            if data.get("stat") != "OK" or not data.get("data"):
                continue
            for row in data["data"]:
                if str(row[0]).strip() == stock_no:
                    results.append({
                        "date":        date_str,
                        "foreign_net": row[4],
                        "trust_net":   row[10],
                        "dealer_net":  row[13],
                    })
                    break
        except Exception as e:
            print(f"T86 錯誤 {stock_no} {date_str}: {e}")
        time.sleep(0.3)
    return results


# ─────────────────────────────────────────────────────────────
# 主函數
# ─────────────────────────────────────────────────────────────

def get_full_indicators(symbol: str) -> dict:
    """
    取得完整技術指標。
    回傳 dict，available=True 表示資料足夠。
    台股（.TW）會額外呼叫 TWSE API 取得融資券與三大法人。
    """
    result = {"symbol": symbol, "available": False}

    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="3mo")

        if hist.empty or len(hist) < 20:
            print(f"⚠️ {symbol} 歷史資料不足（{len(hist)} 筆）")
            return result

        closes  = [float(c) for c in hist["Close"].tolist()]
        highs   = [float(h) for h in hist["High"].tolist()]
        lows    = [float(l) for l in hist["Low"].tolist()]
        volumes = [float(v) for v in hist["Volume"].tolist()]
        curr    = closes[-1]

        # 均線
        def _ma(n):
            return round(sum(closes[-n:]) / min(n, len(closes)), 2) if len(closes) >= 5 else None

        ma5, ma10, ma20 = _ma(5), _ma(10), _ma(20)
        ma60 = round(sum(closes[-60:]) / min(60, len(closes)), 2) if len(closes) >= 30 else None

        result.update({
            "available": True,
            "price":     round(curr, 2),
            "ma5":       ma5,
            "ma10":      ma10,
            "ma20":      ma20,
            "ma60":      ma60,
            "ma5_vs":    "多頭" if ma5  and curr >= ma5  else "空頭",
            "ma20_vs":   "多頭" if ma20 and curr >= ma20 else ("空頭" if ma20 else "N/A"),
            "ma60_vs":   "多頭" if ma60 and curr >= ma60 else ("空頭" if ma60 else "N/A"),
            "macd":      calc_macd(closes),
            "rsi":       calc_rsi(closes),
            "kdj":       calc_kdj(highs, lows, closes),
            "bias":      calc_bias(closes),
            "volume":    calc_volume(volumes),
        })

        # 台股才抓 TWSE 資料
        is_tw = ".TW" in symbol.upper() and "^" not in symbol
        if is_tw:
            stock_no = symbol.upper().replace(".TW", "").replace(".TWO", "")
            time.sleep(0.3)
            result["margin"]           = fetch_twse_margin(stock_no)
            time.sleep(0.3)
            result["institutional"]    = fetch_twse_institutional(stock_no)
            time.sleep(0.3)
            result["institutional_5d"] = fetch_twse_institutional_5d(stock_no)
            time.sleep(0.3)
            result["fundamentals"]     = fetch_stock_fundamentals(symbol)
            time.sleep(0.3)
            result["taifex_txf"]       = fetch_taifex_txf_5d()

        return result

    except Exception as e:
        print(f"❌ 技術指標計算失敗 {symbol}: {e}")
        return result


# ─────────────────────────────────────────────────────────────
# 資料品質報告（Phase 1 資訊收斂輔助）
# ─────────────────────────────────────────────────────────────

def get_data_quality_report(ind: dict) -> str:
    """
    生成資料來源與品質摘要，供 Phase 1 資訊收斂使用。
    讓 Claude 知道哪些來源有效、哪些缺失，避免對缺失資料過度推論。
    """
    lines = []
    symbol = ind.get("symbol", "")
    is_tw  = ".TW" in symbol.upper() and "^" not in symbol

    # 技術指標
    if ind.get("available"):
        macd_ok  = ind.get("macd", {}).get("macd") is not None
        rsi_ok   = ind.get("rsi",  {}).get("rsi")  is not None
        kdj_ok   = ind.get("kdj",  {}).get("k")    is not None
        vol_ok   = ind.get("volume", {}).get("vol_ratio") is not None
        lines.append(
            f"✅ [yfinance] 技術指標：MA/MACD{'✓' if macd_ok else '✗'}"
            f" RSI{'✓' if rsi_ok else '✗'} KDJ{'✓' if kdj_ok else '✗'}"
            f" 量能{'✓' if vol_ok else '✗'}"
        )
    else:
        lines.append("❌ [yfinance] 技術指標：資料不足（歷史 < 20 筆）")

    if is_tw:
        # 籌碼面
        mg  = ind.get("margin", {})
        ins = ind.get("institutional", {})
        i5d = ind.get("institutional_5d", [])
        lines.append(
            f"{'✅' if mg else '⚠️'} [TWSE] 融資券：{'有' if mg else '無法取得'}"
            f" │ 三大法人（單日）：{'有' if ins else '無法取得'}"
            f" │ 5日明細：{'有 '+str(len(i5d))+'筆' if i5d else '無法取得'}"
        )

        # 基本面
        fund = ind.get("fundamentals", {})
        have = [k for k in ["pe","pb","eps","roe","div_yield"] if fund.get(k) is not None]
        lines.append(
            f"{'✅' if len(have)>=3 else ('⚠️' if have else '❌')} [yfinance] 基本面："
            f" {'/'.join(have) if have else '無法取得（部分台股 yfinance 基本面不完整）'}"
        )

        # 台指期
        txf = ind.get("taifex_txf", [])
        lines.append(
            f"{'✅' if len(txf)>=3 else ('⚠️' if txf else '❌')} [FinMind] 台指期："
            f" {'近'+str(len(txf))+'日正逆價差+OI' if txf else '無法取得'}"
        )
    else:
        lines.append("ℹ️ 非台股：TWSE/FinMind 籌碼與期貨資料不適用")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 格式化輸出
# ─────────────────────────────────────────────────────────────

def format_indicators_for_prompt(ind: dict) -> str:
    """格式化為 Claude prompt 用的純文字"""
    if not ind.get("available"):
        return "（技術指標資料不足，請以新聞和基本面為主要分析依據）"

    lines = []
    curr = ind.get("price", 0)

    # 均線
    ma_parts = []
    for p, key in [(5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60")]:
        v = ind.get(key)
        if v:
            pos = "上方" if curr >= v else "下方"
            ma_parts.append(f"MA{p}={v}（現價{pos}）")
    if ma_parts:
        lines.append(f"均線：{' | '.join(ma_parts)}")
        lines.append(f"短均趨勢：{ind.get('ma5_vs','N/A')} | 中均趨勢：{ind.get('ma20_vs','N/A')} | 長均趨勢：{ind.get('ma60_vs','N/A')}")

    # MACD
    m = ind.get("macd", {})
    if m.get("macd") is not None:
        cross = " ★黃金交叉" if m.get("golden_cross") else (" ★死亡交叉" if m.get("dead_cross") else "")
        lines.append(f"MACD={m['macd']} Signal={m['signal']} 柱狀={m['hist']} 趨勢：{m['trend']}{cross}")

    # RSI
    r = ind.get("rsi", {})
    if r.get("rsi") is not None:
        lines.append(f"RSI(14)={r['rsi']} — {r['signal']}")

    # KDJ
    k = ind.get("kdj", {})
    if k.get("k") is not None:
        lines.append(f"KDJ：K={k['k']} D={k['d']} J={k['j']} — {k['signal']}")

    # 乖離率
    b = ind.get("bias", {})
    bp = [f"{p}日={b[f'bias{p}']:+.2f}%" for p in [5, 10, 20] if b.get(f"bias{p}") is not None]
    if bp:
        lines.append(f"乖離率：{' | '.join(bp)}")

    # 成交量
    v = ind.get("volume", {})
    if v.get("vol_ratio") is not None:
        lines.append(f"成交量比（量比）={v['vol_ratio']}x — {v['vol_trend']}")

    # 融資券
    mg = ind.get("margin", {})
    if mg:
        lines.append(
            f"融資餘額={mg.get('margin_balance','N/A')} 張 | "
            f"融資買進={mg.get('margin_buy','N/A')} 融資賣出={mg.get('margin_sell','N/A')} | "
            f"融券餘額={mg.get('short_balance','N/A')} 張 | "
            f"融券賣出={mg.get('short_sell','N/A')} 融券買進={mg.get('short_buy','N/A')}"
        )

    # 三大法人（單日）
    inst = ind.get("institutional", {})
    if inst:
        lines.append(
            f"三大法人買賣超（{inst.get('date','')}）："
            f"外資={inst.get('foreign_net','N/A')} | "
            f"投信={inst.get('trust_net','N/A')} | "
            f"自營商={inst.get('dealer_net','N/A')} | "
            f"合計={inst.get('total_net','N/A')}"
        )

    # 籌碼面：5日三大法人
    inst5 = ind.get("institutional_5d", [])
    if inst5:
        lines.append("5日法人籌碼（外資買賣超/投信買賣超/自營商，千股）：")
        for r in inst5[:5]:
            lines.append(f"  {r.get('date','')}  外資={r.get('foreign_net','?')}  投信={r.get('trust_net','?')}  自營={r.get('dealer_net','?')}")

    # 基本面
    fund = ind.get("fundamentals", {})
    if fund:
        parts = []
        if fund.get("pe")            is not None: parts.append(f"本益比(PE)={fund['pe']:.1f}")
        if fund.get("forward_pe")    is not None: parts.append(f"預估PE={fund['forward_pe']:.1f}")
        if fund.get("pb")            is not None: parts.append(f"股價淨值比(PB)={fund['pb']:.2f}")
        if fund.get("eps")           is not None: parts.append(f"EPS={fund['eps']:.2f}")
        if fund.get("roe")           is not None: parts.append(f"ROE={fund['roe']*100:.1f}%")
        if fund.get("div_yield")     is not None: parts.append(f"殖利率={fund['div_yield']*100:.2f}%")
        if fund.get("profit_margin") is not None: parts.append(f"淨利率={fund['profit_margin']*100:.1f}%")
        if parts:
            lines.append("基本面：" + " | ".join(parts))

    # 台指期5日
    txf = ind.get("taifex_txf", [])
    if txf:
        lines.append("台指期近5日（期貨收盤/現貨/正逆價差/未平倉量）：")
        for r in txf[:5]:
            basis = r.get("basis")
            bstr  = f"{basis:+.0f}（{r.get('basis_type','')}）" if basis is not None else "N/A"
            lines.append(f"  {r.get('date','')}  期={r.get('futures_close','?')}  現={r.get('spot_close','?')}  價差={bstr}  OI={r.get('oi','?')}")

    return "\n".join(lines) if lines else "（無法取得技術指標）"


def format_indicators_for_discord(ind: dict) -> str:
    """格式化為 Discord 顯示的 Markdown 文字"""
    if not ind.get("available"):
        return "⚠️ 技術指標資料不足（歷史資料 < 20 筆）"

    sections = []
    curr = ind.get("price", 0)

    # 均線
    ma_parts = []
    for p, key in [(5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60")]:
        v = ind.get(key)
        if v:
            icon = "🟢" if curr >= v else "🔴"
            ma_parts.append(f"{icon} MA{p}:`{v}`")
    if ma_parts:
        sections.append("**📐 均線**\n" + "  ".join(ma_parts))

    # 動能指標
    momentum = []
    m = ind.get("macd", {})
    if m.get("macd") is not None:
        cross = " 🔔**黃金交叉!**" if m.get("golden_cross") else (" 💀**死亡交叉!**" if m.get("dead_cross") else "")
        momentum.append(f"MACD `{m['macd']}` | Signal `{m['signal']}` | 柱 `{m['hist']}`{cross}\n　{m['trend']}")
    r = ind.get("rsi", {})
    if r.get("rsi") is not None:
        momentum.append(f"RSI(14) `{r['rsi']}` — {r['signal']}")
    k = ind.get("kdj", {})
    if k.get("k") is not None:
        momentum.append(f"KDJ  K=`{k['k']}` D=`{k['d']}` J=`{k['j']}` — {k['signal']}")
    if momentum:
        sections.append("**📊 動能指標**\n" + "\n".join(momentum))

    # 乖離率
    b = ind.get("bias", {})
    bp = []
    for p in [5, 10, 20]:
        v = b.get(f"bias{p}")
        if v is not None:
            icon = "🔴" if v > 5 else ("🟢" if v < -5 else "⚪")
            bp.append(f"{icon} {p}日:`{v:+.2f}%`")
    if bp:
        sections.append("**📏 乖離率**\n" + "  ".join(bp))

    # 成交量
    v = ind.get("volume", {})
    if v.get("vol_ratio") is not None:
        sections.append(
            f"**📦 成交量**\n"
            f"量比 `{v['vol_ratio']}x` — {v['vol_trend']}"
        )

    # 融資券
    mg = ind.get("margin", {})
    if mg:
        sections.append(
            f"**💳 融資券**\n"
            f"融資餘額 `{mg.get('margin_balance','N/A')}` 張　"
            f"融券餘額 `{mg.get('short_balance','N/A')}` 張\n"
            f"融資買/賣 `{mg.get('margin_buy','N/A')}` / `{mg.get('margin_sell','N/A')}`　"
            f"融券賣/買 `{mg.get('short_sell','N/A')}` / `{mg.get('short_buy','N/A')}`"
        )

    # 三大法人（單日）
    inst = ind.get("institutional", {})
    if inst:
        sections.append(
            f"**🏦 三大法人買賣超**（{inst.get('date','')}）\n"
            f"外資 `{inst.get('foreign_net','N/A')}`　"
            f"投信 `{inst.get('trust_net','N/A')}`　"
            f"自營商 `{inst.get('dealer_net','N/A')}`　"
            f"合計 `{inst.get('total_net','N/A')}`"
        )

    # 籌碼面：5日三大法人
    inst5 = ind.get("institutional_5d", [])
    if inst5:
        rows = []
        for rec in inst5[:5]:
            rows.append(
                f"`{rec.get('date','')}` "
                f"外資`{rec.get('foreign_net','?')}` "
                f"投信`{rec.get('trust_net','?')}` "
                f"自營`{rec.get('dealer_net','?')}`"
            )
        sections.append("**📊 籌碼面（5日法人，千股）**\n" + "\n".join(rows))

    # 基本面
    fund = ind.get("fundamentals", {})
    fund_parts = []
    if fund.get("pe")            is not None: fund_parts.append(f"PE `{fund['pe']:.1f}`")
    if fund.get("forward_pe")    is not None: fund_parts.append(f"預估PE `{fund['forward_pe']:.1f}`")
    if fund.get("pb")            is not None: fund_parts.append(f"PB `{fund['pb']:.2f}`")
    if fund.get("eps")           is not None: fund_parts.append(f"EPS `{fund['eps']:.2f}`")
    if fund.get("roe")           is not None: fund_parts.append(f"ROE `{fund['roe']*100:.1f}%`")
    if fund.get("div_yield")     is not None: fund_parts.append(f"殖利率 `{fund['div_yield']*100:.2f}%`")
    if fund.get("profit_margin") is not None: fund_parts.append(f"淨利率 `{fund['profit_margin']*100:.1f}%`")
    if fund_parts:
        sections.append("**📋 基本面**\n" + "  ".join(fund_parts))

    # 台指期5日
    txf = ind.get("taifex_txf", [])
    if txf:
        txf_rows = []
        for rec in txf[:5]:
            basis = rec.get("basis")
            if basis is not None:
                bicon = "🔴" if basis >= 0 else "🟢"
                bstr  = f"{bicon}`{basis:+.0f}`（{rec.get('basis_type','')}）"
            else:
                bstr = "N/A"
            txf_rows.append(
                f"`{rec.get('date','')}` "
                f"期`{rec.get('futures_close','?')}` 現`{rec.get('spot_close','?')}` "
                f"價差{bstr} OI`{rec.get('oi','?')}`"
            )
        sections.append("**📐 台指期（5日正逆價差／未平倉）**\n" + "\n".join(txf_rows))

    return "\n\n".join(sections) if sections else "⚠️ 無法取得技術指標"
