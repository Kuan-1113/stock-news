"""
podcast_tracker.py
Spotify Podcast 追蹤器：新集數 → RSS下載 → faster-whisper 轉錄 → Claude 分析 → Discord

追蹤：韭菜畢業班、股癌、科技浪
輸出：Discord 名人堂頻道

環境變數：
  SPOTIFY_CLIENT_ID     - Spotify Developer App Client ID
  SPOTIFY_CLIENT_SECRET - Spotify Developer App Client Secret
  ANTHROPIC_API_KEY     - Claude API Key
  DISCORD_PODCAST       - 名人堂 Webhook（有預設值）
"""

import os, sys, json, re, time, datetime, tempfile, warnings, requests, feedparser, pytz
warnings.filterwarnings("ignore")

SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
DISCORD_PODCAST       = os.environ.get("DISCORD_PODCAST",
    "https://discord.com/api/webhooks/1508482881864077312/Vd42Ytajzz80KYyHZHwX_EzjfctiW5ZBWq5CccuLHRnElqET5U_qli_1uis31nuvyYVQ")

TW_TZ            = pytz.timezone("Asia/Taipei")
STATE_FILE        = "podcast_state.json"
MAX_NEW_PER_SHOW  = 2       # 每次最多處理幾集新集數
MAX_BACKLOG_DAYS  = 7       # 只處理最近幾天的集數（防止第一次跑時倒灌舊集）
MAX_AUDIO_MB      = 300     # 最大下載音訊大小限制

SHOWS = [
    {"id": "66ENh5UtNA3pPNOT0IZjO1", "name": "韭菜畢業班"},
    {"id": "1zWxx5pKk0XBEzMupVC7UZ", "name": "股癌"},
    {"id": "50uNQjIutrgwZRhfJc1A8u",  "name": "科技浪"},
]

# ─────────────────────────────────────────────────────────────
# 狀態管理（podcast_state.json 存 repo）
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
# Spotify API（Client Credentials — 不需要用戶登入）
# ─────────────────────────────────────────────────────────────

def get_spotify_token() -> str:
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]

def get_show_info(show_id: str, token: str) -> dict:
    r = requests.get(
        f"https://api.spotify.com/v1/shows/{show_id}",
        headers={"Authorization": f"Bearer {token}"},
        params={"market": "TW"},
        timeout=15,
    )
    return r.json() if r.status_code == 200 else {}

def get_latest_episodes(show_id: str, token: str, limit: int = 5) -> list:
    r = requests.get(
        f"https://api.spotify.com/v1/shows/{show_id}/episodes",
        headers={"Authorization": f"Bearer {token}"},
        params={"market": "TW", "limit": limit},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"  ❌ 取得集數失敗 HTTP {r.status_code}")
        return []
    return r.json().get("items", [])

# ─────────────────────────────────────────────────────────────
# RSS 音訊取得
# ─────────────────────────────────────────────────────────────

def extract_rss_from_text(text: str) -> str:
    pattern = r'https?://\S+(?:rss|feed|xml)\S*'
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(0).rstrip(".,)>\"'") if m else ""

def get_rss_url(show_info: dict) -> str:
    """從 Spotify show 資訊嘗試取得 RSS URL"""
    for field in ["description", "html_description"]:
        rss = extract_rss_from_text(show_info.get(field, ""))
        if rss:
            return rss
    return ""

def find_audio_url_in_rss(rss_url: str, ep_title: str) -> str:
    """從 RSS 找到匹配集數的 MP3 URL"""
    try:
        feed = feedparser.parse(rss_url, request_headers={"User-Agent": "Mozilla/5.0"})
        if not feed.entries:
            return ""

        # 嘗試標題匹配
        clean_ep = re.sub(r"[\s\W]", "", ep_title.lower())[:20]
        for entry in feed.entries[:10]:
            clean_rss = re.sub(r"[\s\W]", "", getattr(entry, "title", "").lower())[:20]
            if clean_ep and (clean_ep in clean_rss or clean_rss in clean_ep):
                for enc in getattr(entry, "enclosures", []):
                    if enc.get("type", "").startswith("audio"):
                        return enc.get("href") or enc.get("url", "")

        # 標題沒匹配到，回傳 RSS 最新一集
        for entry in feed.entries[:3]:
            for enc in getattr(entry, "enclosures", []):
                if enc.get("type", "").startswith("audio"):
                    return enc.get("href") or enc.get("url", "")
    except Exception as e:
        print(f"  ⚠️  RSS 錯誤：{e}")
    return ""

# ─────────────────────────────────────────────────────────────
# 音訊下載
# ─────────────────────────────────────────────────────────────

def download_audio(url: str, dest: str) -> bool:
    try:
        print(f"  ⬇️  下載音訊...")
        with requests.get(url, stream=True, timeout=120,
                          headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status_code != 200:
                return False
            downloaded = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > MAX_AUDIO_MB * 1024 * 1024:
                            print(f"  ⚠️  音訊超過 {MAX_AUDIO_MB}MB，截斷下載")
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
        print("  🔄  轉錄中（約需 5-15 分鐘）...")
        segments, _ = model.transcribe(audio_path, beam_size=3)
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

def analyze_episode(show_name: str, ep: dict, transcript: str) -> str:
    title    = ep.get("name", "")
    desc     = re.sub(r"<[^>]+>", "", ep.get("description", ""))[:1500]
    dur_min  = ep.get("duration_ms", 0) // 60000

    if transcript:
        content = f"【逐字稿節錄（前 4000 字）】\n{transcript[:4000]}"
        basis   = "逐字稿"
    else:
        content = f"【集數描述】\n{desc}"
        basis   = "集數描述"

    prompt = f"""你是一位專業 Podcast 分析師，熟悉台灣財經與科技投資。

【節目】{show_name}
【集數】{title}
【時長】約 {dur_min} 分鐘

{content}

請以繁體中文撰寫分析報告（900 字以內）：

🎙️ **本集主題概述**（2-3 句說明核心議題）

💡 **核心觀點 Top 3-5**
（條列，每點附簡短說明，聚焦投資/科技洞察）

📊 **提及的關鍵標的/趨勢**
（條列個股、ETF、產業趨勢，並說明主持人看法）

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
            print(f"  {'✅' if r.status_code in [200,204] else '❌'} Discord {r.status_code}")
        except Exception as e:
            print(f"  ❌ Discord 錯誤：{e}")
        time.sleep(0.8)

def fmt_dur(ms: int) -> str:
    m = ms // 60000
    return f"{m//60}h{m%60:02d}m" if m >= 60 else f"{m}m"

def parse_date(date_str: str):
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=TW_TZ)
    except Exception:
        return datetime.datetime(2000, 1, 1, tzinfo=TW_TZ)

# ─────────────────────────────────────────────────────────────
# 單集處理
# ─────────────────────────────────────────────────────────────

def process_episode(show_name: str, ep: dict, rss_url: str):
    title = ep.get("name", "Unknown")
    print(f"\n  📻 [{show_name}] {title[:50]}")

    transcript = ""

    if rss_url:
        audio_url = find_audio_url_in_rss(rss_url, title)
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
        else:
            print("  ⚠️  RSS 中未找到對應音訊")
    else:
        print("  ⚠️  無 RSS，使用集數描述分析")

    print("  🤖 Claude 分析中...")
    analysis = analyze_episode(show_name, ep, transcript)

    ts       = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    mode_tag = "🎙️ 逐字稿分析" if transcript else "📋 描述摘要"
    header   = (
        f"## 🎧 **{show_name}** 新集數 | {ts}\n"
        f"**{title}**\n"
        f"📅 {ep.get('release_date','')}　⏱ {fmt_dur(ep.get('duration_ms',0))}　{mode_tag}\n"
        f"🔗 <https://open.spotify.com/episode/{ep.get('id','')}>\n\n"
        f"---\n\n"
    )
    send_discord(header + analysis)

# ─────────────────────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"🎙️  Podcast 追蹤器 — {datetime.datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        print("❌ 缺少 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET")
        sys.exit(1)

    state = load_state()
    cutoff = datetime.datetime.now(TW_TZ) - datetime.timedelta(days=MAX_BACKLOG_DAYS)

    print("\n🔑 取得 Spotify token...")
    token = get_spotify_token()
    print("✅ Token OK")

    for show in SHOWS:
        show_id, show_name = show["id"], show["name"]
        print(f"\n{'─'*50}")
        print(f"📻 {show_name}")

        seen    = set(state.get(show_id, []))
        episodes = get_latest_episodes(show_id, token, limit=5)
        if not episodes:
            continue

        new_eps = [
            ep for ep in episodes
            if ep.get("id") and ep["id"] not in seen
            and parse_date(ep.get("release_date", "")) >= cutoff
        ][:MAX_NEW_PER_SHOW]

        if not new_eps:
            print(f"  ✅ 無新集數（最新：{episodes[0].get('name','?')[:35]}）")
            continue

        print(f"  🆕 {len(new_eps)} 集新集數")

        show_info = get_show_info(show_id, token)
        rss_url   = get_rss_url(show_info)
        print(f"  RSS：{rss_url[:60] if rss_url else '未找到'}")

        for ep in reversed(new_eps):
            try:
                process_episode(show_name, ep, rss_url)
                seen.add(ep["id"])
                state[show_id] = list(seen)
                save_state(state)
                time.sleep(2)
            except Exception as e:
                print(f"  ❌ 處理失敗：{e}")

    print("\n" + "=" * 60)
    print("✅ 完成！")
    print("=" * 60)

if __name__ == "__main__":
    main()
