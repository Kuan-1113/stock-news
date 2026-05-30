"""
podcast_tracker.py
Podcast 追蹤器：RSS 偵測新集數 → faster-whisper 轉逐字稿 → Claude 分析 → Discord 名人堂

追蹤：韭菜畢業班、股癌（Gooaye）、科技浪
無需 Spotify API，直接使用公開 RSS feed

環境變數（必要）：
  ANTHROPIC_API_KEY - Claude API Key

環境變數（選填）：
  DISCORD_PODCAST   - 名人堂 Webhook（有預設值）
"""

import os, sys, json, re, time, datetime, tempfile, warnings, requests, feedparser, pytz
warnings.filterwarnings("ignore")

# ── 讓 shared/ 套件可被 import（Podcast/ 子目錄執行時需向上一層加路徑）────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get_tw_standard_time() -> datetime.datetime:
    """向台灣標準時間 NTP 伺服器對時（失敗時降級備用）"""
    TW = pytz.timezone("Asia/Taipei")
    try:
        import ntplib as _ntplib
        c    = _ntplib.NTPClient()
        resp = c.request('time.stdtime.gov.tw', version=3, timeout=5)
        t    = datetime.datetime.fromtimestamp(resp.tx_time, tz=TW)
        print(f"⏰ 台灣標準時間（NTP stdtime.gov.tw）：{t.strftime('%Y-%m-%d %H:%M:%S')}")
        return t
    except Exception as e:
        print(f"⚠️  NTP 失敗（{e}），使用系統時間")
    return datetime.datetime.now(TW)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
DISCORD_PODCAST   = os.environ.get("DISCORD_PODCAST",
    "https://discord.com/api/webhooks/1508482881864077312/Vd42Ytajzz80KYyHZHwX_EzjfctiW5ZBWq5CccuLHRnElqET5U_qli_1uis31nuvyYVQ")

TW_TZ            = pytz.timezone("Asia/Taipei")
STATE_FILE        = "podcast_state.json"
MAX_NEW_PER_SHOW  = 1      # 每次只處理最新一集（避免一次處理太多）
MAX_AUDIO_MB      = 80     # 音訊大小上限（約 50 分鐘 128kbps）

SHOWS = [
    {
        "name": "韭菜畢業班",
        "rss":  "https://feeds.soundon.fm/podcasts/70907bd6-d0ae-4b64-bc38-2bf48ae4fc36.xml",
    },
    {
        "name": "股癌（Gooaye）",
        "rss":  "https://feeds.soundon.fm/podcasts/954689a5-3096-43a4-a80b-7810b219cef3.xml",
    },
    {
        "name": "科技浪",
        "rss":  "https://feed.firstory.me/rss/user/cm3o5681s06e801v3fxpjehwb",
    },
]

# ─────────────────────────────────────────────────────────────
# 狀態管理
# ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────────────────────────
# RSS 解析
# ─────────────────────────────────────────────────────────────

def parse_entry_date(entry) -> datetime.datetime:
    for attr in ["published_parsed", "updated_parsed"]:
        t = getattr(entry, attr, None)
        if t:
            try:
                dt = datetime.datetime(*t[:6], tzinfo=datetime.timezone.utc)
                return dt.astimezone(TW_TZ)
            except Exception:
                pass
    return datetime.datetime(2000, 1, 1, tzinfo=TW_TZ)

def get_audio_url(entry) -> str:
    """從 RSS entry 取得音訊 URL"""
    for enc in getattr(entry, "enclosures", []):
        mime = enc.get("type", "")
        url  = enc.get("href") or enc.get("url", "")
        if url and (mime.startswith("audio") or url.endswith(".mp3")):
            return url
    # fallback：links 裡找 audio
    for link in getattr(entry, "links", []):
        if link.get("type", "").startswith("audio"):
            return link.get("href", "")
    return ""

def get_episode_id(entry) -> str:
    """取得集數唯一識別碼"""
    return getattr(entry, "id", "") or getattr(entry, "guid", "") or getattr(entry, "link", "")

def get_duration_str(entry) -> str:
    """嘗試取得時長"""
    dur = getattr(entry, "itunes_duration", "") or getattr(entry, "duration", "")
    if not dur:
        return ""
    try:
        parts = str(dur).split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return f"{h}h{m:02d}m" if h > 0 else f"{m}m{s:02d}s"
        elif len(parts) == 2:
            return f"{parts[0]}m{int(parts[1]):02d}s"
        else:
            secs = int(dur)
            return f"{secs//3600}h{(secs%3600)//60:02d}m" if secs >= 3600 else f"{secs//60}m{secs%60:02d}s"
    except Exception:
        return str(dur)

def fetch_rss_episodes(rss_url: str) -> list:
    """取得 RSS 集數列表"""
    try:
        feed = feedparser.parse(rss_url, request_headers={"User-Agent": "Mozilla/5.0"})
        return feed.entries or []
    except Exception as e:
        print(f"  ❌ RSS 錯誤：{e}")
        return []

# ─────────────────────────────────────────────────────────────
# 音訊下載
# ─────────────────────────────────────────────────────────────

def download_audio(url: str, dest: str) -> bool:
    try:
        print(f"  ⬇️  下載音訊...")
        with requests.get(url, stream=True, timeout=180,
                          headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status_code != 200:
                print(f"  ❌ HTTP {r.status_code}")
                return False
            downloaded = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > MAX_AUDIO_MB * 1024 * 1024:
                            print(f"  ⚠️  超過 {MAX_AUDIO_MB}MB，截斷")
                            break
        mb = os.path.getsize(dest) / 1024 / 1024
        print(f"  ✅ 下載完成（{mb:.1f} MB）")
        return True
    except Exception as e:
        print(f"  ❌ 下載失敗：{e}")
        return False

# ─────────────────────────────────────────────────────────────
# 逐字稿（faster-whisper）
# ─────────────────────────────────────────────────────────────

def transcribe(audio_path: str) -> str:
    try:
        from faster_whisper import WhisperModel
        print("  🎙️  載入 Whisper base 模型...")
        model = WhisperModel("base", device="cpu", compute_type="int8")
        print("  🔄  轉錄中（約需 3-8 分鐘）...")
        segments, _ = model.transcribe(audio_path, beam_size=1, vad_filter=True)
        text = " ".join(s.text.strip() for s in segments)
        print(f"  ✅ 轉錄完成（{len(text)} 字元）")
        return text
    except ImportError:
        print("  ⚠️  faster-whisper 未安裝，跳過轉錄")
        return ""
    except Exception as e:
        print(f"  ❌ 轉錄失敗：{e}")
        return ""

# ─────────────────────────────────────────────────────────────
# Claude AI 分析
# ─────────────────────────────────────────────────────────────

try:
    from shared.claude_client import simple_call as _sdk_call
    def claude_call(prompt: str, max_tokens: int = 1600) -> str:
        """使用 Anthropic SDK 呼叫 Claude（優先）。"""
        return _sdk_call(prompt, max_tokens=max_tokens)
except ImportError:
    def claude_call(prompt: str, max_tokens: int = 1600) -> str:
        """降級：直接用 requests.post 呼叫 Claude（SDK 不可用時）。"""
        if not ANTHROPIC_API_KEY:
            return "⚠️ 未設定 ANTHROPIC_API_KEY"
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": CLAUDE_MODEL,
                      "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=90,
            )
            return r.json()["content"][0]["text"].strip() if r.status_code == 200 \
                   else f"分析暫時無法使用（HTTP {r.status_code}）"
        except Exception as e:
            return f"分析暫時無法使用（{str(e)[:100]}）"

def analyze_episode(show_name: str, title: str, description: str,
                    duration_str: str, transcript: str) -> str:
    desc = re.sub(r"<[^>]+>", "", description)[:3000]

    if transcript:
        tlen = len(transcript)
        # 智慧截取：60 分鐘 Podcast 約 4-8 萬字元，只送前 5000 字會嚴重截斷
        # 策略：≤24000 → 全文；>24000 → 首 16000 + 尾 8000（保留開頭重點與結尾結論）
        MAX_CHARS = 24000
        if tlen <= MAX_CHARS:
            chunk_text = transcript
            chunk_note = f"全文 {tlen:,} 字元"
        else:
            head = transcript[:16000]
            tail = transcript[-8000:]
            chunk_text = head + "\n\n……[中段省略，僅保留首尾精華]……\n\n" + tail
            chunk_note = f"節錄：首 16,000 + 尾 8,000 字元（全文共 {tlen:,} 字元）"
        content = f"【逐字稿（{chunk_note}）】\n{chunk_text}"
        basis   = "逐字稿"
    else:
        content = f"【集數描述】\n{desc}"
        basis   = "集數描述"

    prompt = f"""你是一位專業 Podcast 分析師，熟悉台灣財經與科技投資。

【節目】{show_name}
【集數】{title}
【時長】{duration_str}

{content}

請以繁體中文撰寫完整分析報告（1200 字以內），讓沒聽集數的讀者也能了解這集說了什麼：

🎙️ **本集主題概述**（2-3 句說明核心議題與討論方向）

📝 **本集內容摘要**（條列主要討論內容，5-8 點，每點一句，讓讀者知道講了哪些事）

💡 **核心觀點 Top 3-5**
（每點帶具體論述與數據，聚焦投資/科技洞察，禁空話）

📊 **提及的關鍵標的/趨勢**
（條列個股代號、ETF、產業趨勢，說明主持人看法與多空判斷；若無則略過）

🔮 **主持人市場展望或預測**（若有提及，摘要其具體觀點與理由）

⚠️ **重要風險提示或爭議觀點**（若有）

🔑 **聽後行動建議**（2-3 點，具體可行）

*以上分析基於{basis}，由 AI 生成，僅供參考，不構成投資建議。*"""

    return claude_call(prompt, max_tokens=2500)

# ─────────────────────────────────────────────────────────────
# Discord 傳送
# ─────────────────────────────────────────────────────────────

def send_discord(content: str):
    if not content.strip():
        return
    chunks, cur = [], ""
    for line in content.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        chunks.append(cur)
    for chunk in chunks:
        try:
            r = requests.post(DISCORD_PODCAST, json={"content": chunk}, timeout=15)
            print(f"  {'✅' if r.status_code in [200, 204] else '❌'} Discord {r.status_code}")
        except Exception as e:
            print(f"  ❌ Discord 錯誤：{e}")
        time.sleep(0.8)

# ─────────────────────────────────────────────────────────────
# 單集處理
# ─────────────────────────────────────────────────────────────

def process_episode(show_name: str, entry) -> bool:
    title       = getattr(entry, "title", "Unknown")
    description = getattr(entry, "summary", "") or getattr(entry, "description", "")
    audio_url   = get_audio_url(entry)
    pub_date    = parse_entry_date(entry)
    dur_str     = get_duration_str(entry)
    ep_link     = getattr(entry, "link", "")

    print(f"\n  📻 [{show_name}] {title[:60]}")
    print(f"       時長：{dur_str} | 音訊：{'✅' if audio_url else '❌'}")

    transcript = ""
    if audio_url:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            tmp = tf.name
        try:
            if download_audio(audio_url, tmp):
                transcript = transcribe(tmp)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    print("  🤖 Claude 分析中...")
    analysis  = analyze_episode(show_name, title, description, dur_str, transcript)
    mode_tag  = "🎙️ 逐字稿分析" if transcript else "📋 描述摘要"
    ts        = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    pub_str   = pub_date.strftime("%Y-%m-%d") if pub_date.year > 2000 else ""
    link_part = f"\n🔗 <{ep_link}>" if ep_link else ""

    header = (
        f"## 🎧 **{show_name}** 新集數 | {ts}\n"
        f"**{title}**\n"
        f"📅 {pub_str}　⏱ {dur_str}　{mode_tag}{link_part}\n\n"
        f"---\n\n"
    )
    send_discord(header + analysis)
    return True

# ─────────────────────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────────────────────

def send_daily_summary(show_statuses: list):
    """每次執行結束後，發送一則當日 Podcast 檢查摘要到 Discord"""
    ts   = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    lines = [f"🎙️ **Podcast 每日追蹤** | {ts}", ""]
    for name, status in show_statuses:
        lines.append(f"• **{name}**：{status}")
    send_discord("\n".join(lines))


def main():
    print("=" * 60)
    tw_std = _get_tw_standard_time()
    print(f"🎙️  Podcast 追蹤器 — {tw_std.strftime('%Y-%m-%d %H:%M')}（台灣標準時間）")
    print("=" * 60)

    state        = load_state()
    now_tw       = tw_std
    today_start  = now_tw.replace(hour=0, minute=0, second=0, microsecond=0)
    any_new      = False
    show_statuses = []   # [(name, status_str), ...]

    for show in SHOWS:
        name = show["name"]
        rss  = show["rss"]
        print(f"\n{'─'*50}")
        print(f"📻 {name}")

        entries  = fetch_rss_episodes(rss)
        if not entries:
            print("  ⚠️  無法取得 RSS")
            show_statuses.append((name, "⚠️ RSS 無法取得"))
            continue

        seen         = set(state.get(name, []))

        # 最新集數資訊（用於狀態顯示）
        latest_entry = entries[0] if entries else None
        latest_title = getattr(latest_entry, "title", "?")[:40] if latest_entry else "?"
        latest_pub   = parse_entry_date(latest_entry) if latest_entry else None
        latest_date  = latest_pub.strftime("%-m/%-d") if latest_pub else "?"

        # 判斷最新集是否為今天發布（判斷台北時間日期）
        today_updated = bool(latest_pub and latest_pub.date() == now_tw.date())

        # 找出近 48 小時內發布且未分析過的集數
        # 48 小時視窗：能捕捉到昨夜發布的集數，又不會把超過 2 天的舊集送出去
        recent_cutoff = now_tw - datetime.timedelta(hours=48)
        new_eps  = []
        for entry in entries[:10]:
            ep_id  = get_episode_id(entry)
            pub_dt = parse_entry_date(entry)
            if ep_id and ep_id not in seen and pub_dt and pub_dt >= recent_cutoff:
                new_eps.append(entry)

        new_eps = new_eps[:MAX_NEW_PER_SHOW]

        if not new_eps:
            if today_updated:
                status_str = f"今日已更新（已分析），最新日期：{latest_date}"
            else:
                status_str = f"今日未更新，最新日期：{latest_date}"
            print(f"  ✅ {status_str}")
            show_statuses.append((name, status_str))
            continue

        print(f"  🆕 {len(new_eps)} 集新集數")

        for entry in reversed(new_eps):  # 從舊到新發送
            ep_title = getattr(entry, "title", "Unknown")[:40]
            ep_pub   = parse_entry_date(entry)
            ep_date  = ep_pub.strftime("%-m/%-d") if ep_pub else "?"
            try:
                process_episode(name, entry)
                seen.add(get_episode_id(entry))
                state[name] = list(seen)
                save_state(state)
                any_new = True
                show_statuses.append((name, f"今日已更新（已分析），最新日期：{ep_date}"))
                time.sleep(2)
            except Exception as e:
                print(f"  ❌ 處理失敗：{e}")
                show_statuses.append((name, f"❌ 處理失敗：{str(e)[:60]}"))

    # 防重複：同一 cron 被 GitHub 觸發兩次時，2 小時內只送一次摘要
    last_summary = state.get("_last_summary_ts", 0)
    elapsed = (now_tw.timestamp() - last_summary)
    if elapsed < 7200:
        print(f"⏭️  摘要已在 {elapsed/60:.0f} 分鐘前送出，跳過重複發送")
    else:
        send_daily_summary(show_statuses)
        state["_last_summary_ts"] = now_tw.timestamp()
        save_state(state)

    print("\n" + "=" * 60)
    print(f"✅ 完成！{'已傳送 Discord 通知' if any_new else '無新集數'}")
    print("=" * 60)

if __name__ == "__main__":
    main()
