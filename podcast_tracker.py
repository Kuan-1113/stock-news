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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DISCORD_PODCAST   = os.environ.get("DISCORD_PODCAST",
    "https://discord.com/api/webhooks/1508482881864077312/Vd42Ytajzz80KYyHZHwX_EzjfctiW5ZBWq5CccuLHRnElqET5U_qli_1uis31nuvyYVQ")

TW_TZ            = pytz.timezone("Asia/Taipei")
STATE_FILE        = "podcast_state.json"
MAX_NEW_PER_SHOW  = 1      # 每次只處理最新一集（避免一次處理太多）
MAX_BACKLOG_DAYS  = 7      # 只處理最近 N 天內的集數（避免第一次跑時倒灌舊集）
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

def claude_call(prompt: str, max_tokens: int = 1600) -> str:
    if not ANTHROPIC_API_KEY:
        return "⚠️ 未設定 ANTHROPIC_API_KEY"
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-opus-4-5",
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
    desc = re.sub(r"<[^>]+>", "", description)[:1500]

    if transcript:
        content = f"【逐字稿節錄（前 4000 字）】\n{transcript[:4000]}"
        basis   = "逐字稿"
    else:
        content = f"【集數描述】\n{desc}"
        basis   = "集數描述"

    prompt = f"""你是一位專業 Podcast 分析師，熟悉台灣財經與科技投資。

【節目】{show_name}
【集數】{title}
【時長】{duration_str}

{content}

請以繁體中文撰寫分析報告（900 字以內）：

🎙️ **本集主題概述**（2-3 句說明核心議題）

💡 **核心觀點 Top 3-5**
（條列，每點附簡短說明，聚焦投資/科技洞察）

📊 **提及的關鍵標的/趨勢**
（條列個股、ETF、產業趨勢，並說明主持人看法；若無則略過）

⚠️ **重要風險提示或爭議觀點**（若有）

🔑 **聽眾行動建議**（1-3 點，具體可行）

*以上分析基於{basis}，由 AI 生成，僅供參考，不構成投資建議。*"""

    return claude_call(prompt)

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
    lines = [f"## 🎙️ Podcast 每日追蹤報告 | {ts}", ""]
    for name, status in show_statuses:
        lines.append(f"• **{name}**　{status}")
    lines.append("")
    lines.append("─" * 30)
    send_discord("\n".join(lines))


def main():
    print("=" * 60)
    print(f"🎙️  Podcast 追蹤器 — {datetime.datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    state        = load_state()
    cutoff       = datetime.datetime.now(TW_TZ) - datetime.timedelta(days=MAX_BACKLOG_DAYS)
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

        seen     = set(state.get(name, []))
        new_eps  = []
        for entry in entries:
            ep_id   = get_episode_id(entry)
            pub_dt  = parse_entry_date(entry)
            if ep_id and ep_id not in seen and pub_dt >= cutoff:
                new_eps.append(entry)

        new_eps = new_eps[:MAX_NEW_PER_SHOW]

        if not new_eps:
            latest_title = getattr(entries[0], "title", "?")[:40] if entries else "?"
            latest_date  = parse_entry_date(entries[0]).strftime("%m/%d") if entries else ""
            print(f"  ✅ 無新集數（最新：{latest_title}）")
            show_statuses.append((name, f"✅ 無新集數　最新：{latest_title}（{latest_date}）"))
            continue

        print(f"  🆕 {len(new_eps)} 集新集數")

        for entry in reversed(new_eps):  # 從舊到新發送
            title = getattr(entry, "title", "Unknown")[:40]
            try:
                process_episode(name, entry)
                seen.add(get_episode_id(entry))
                state[name] = list(seen)
                save_state(state)
                any_new = True
                show_statuses.append((name, f"🆕 新集數已分析：{title}"))
                time.sleep(2)
            except Exception as e:
                print(f"  ❌ 處理失敗：{e}")
                show_statuses.append((name, f"❌ 處理失敗：{str(e)[:60]}"))

    # 每次執行都發送摘要（無論有無新集數）
    send_daily_summary(show_statuses)

    print("\n" + "=" * 60)
    print(f"✅ 完成！{'已傳送 Discord 通知' if any_new else '無新集數'}")
    print("=" * 60)

if __name__ == "__main__":
    main()
