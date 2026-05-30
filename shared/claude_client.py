"""
shared/claude_client.py — Anthropic SDK 統一客戶端
取代各處的 requests.post() 直接呼叫，統一使用官方 anthropic 套件。

提供兩種呼叫模式：
  simple_call()  → Messages API（Discord bot / 輕量快速查詢用）
  agent_call()   → Managed Agents Sessions API（orchestrator 批次分析用）

Managed Agents 優點：
  • Agent 定義（model + system）持久化存放在 Anthropic 雲端
  • 同一執行程序內 Agent / Environment 只建立一次（快取復用）
  • 支援多輪對話 Session 狀態
  • 未來可綁定 web_search、code_execution 等工具

啟用方式：
  環境變數 USE_MANAGED_AGENTS=1  → AnalystAgent 改用 agent_call()
  （預設不設定 = 使用 simple_call()，速度更快）
"""

import os
import anthropic

# 直接讀環境變數（不 import shared.config，確保 main.py / Railway 也能單獨使用）
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Singleton Client ──────────────────────────────────────────────
_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    """回傳（或初始化）Anthropic SDK singleton。"""
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ── 模式一：Messages API（快速、適合 Discord bot）────────────────

def simple_call(
    prompt: str,
    max_tokens: int = 1800,
    model: str = "claude-sonnet-4-6",
    system: str = "",
) -> str:
    """
    使用標準 Messages API 呼叫 Claude。
    • 適用：Discord bot 指令、單次快速分析
    • 無需 Agent / Environment，延遲最低
    """
    if not ANTHROPIC_API_KEY:
        return "⚠️ 未設定 ANTHROPIC_API_KEY，AI 分析無法使用。"
    client = get_client()
    kwargs: dict = {
        "model":      model,
        "max_tokens": max_tokens,
        "messages":   [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    try:
        resp = client.messages.create(**kwargs)
        return resp.content[0].text.strip()
    except anthropic.RateLimitError:
        print("⏳ Claude rate limit（simple_call）")
        return "⏳ AI 分析暫時無法使用（rate limit），請稍後再試。"
    except anthropic.APIStatusError as e:
        print(f"❌ Claude API HTTP {e.status_code}：{str(e)[:150]}")
        return f"⚠️ AI 分析暫時無法使用（HTTP {e.status_code}）"
    except anthropic.APIConnectionError as e:
        print(f"❌ Claude 連線失敗：{e}")
        return "⚠️ AI 分析暫時無法使用（連線逾時）"
    except Exception as e:
        print(f"❌ Claude 呼叫失敗：{e}")
        return f"⚠️ AI 分析暫時無法使用（{str(e)[:100]}）"


# ── 模式二：Managed Agents Sessions API（orchestrator 用）─────────

# 同一執行程序內快取 Agent / Environment ID（避免重複建立）
_AGENT_CACHE: dict[str, str] = {}   # name → agent_id
_ENV_CACHE:   dict[str, str] = {}   # name → environment_id


def _get_or_create_agent(
    name: str,
    model: str = "claude-sonnet-4-6",
    system: str = "你是一位資深股票分析師，以繁體中文提供深度分析。",
) -> str:
    """
    建立（或從快取取得）Managed Agent。
    優先順序：記憶體快取 → 環境變數 → 建立新的
    """
    if name in _AGENT_CACHE:
        return _AGENT_CACHE[name]

    # 從環境變數讀取（可在 GitHub Secrets / Railway 設定，跨 run 復用）
    env_key = "MANAGED_AGENT_ID_" + name.upper().replace("-", "_")
    if cached_id := os.environ.get(env_key, ""):
        _AGENT_CACHE[name] = cached_id
        print(f"  📌 Managed Agent 從環境變數載入：{name} ({cached_id[:12]}...)")
        return cached_id

    client = get_client()
    agent = client.beta.agents.create(
        name=name,
        model=model,
        system=system,
    )
    _AGENT_CACHE[name] = agent.id
    print(f"  ✅ Managed Agent 已建立：{name} ({agent.id[:12]}...)")
    print(f"     💡 可設定環境變數 {env_key}={agent.id} 下次復用")
    return agent.id


def _get_or_create_env(name: str = "stock-analysis") -> str:
    """
    建立（或從快取取得）Cloud Environment。
    優先順序：記憶體快取 → 環境變數 → 建立新的
    """
    if name in _ENV_CACHE:
        return _ENV_CACHE[name]

    env_key = "MANAGED_ENV_ID_" + name.upper().replace("-", "_")
    if cached_id := os.environ.get(env_key, ""):
        _ENV_CACHE[name] = cached_id
        print(f"  📌 Managed Environment 從環境變數載入：{name} ({cached_id[:12]}...)")
        return cached_id

    client = get_client()
    env = client.beta.environments.create(
        name=name,
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    _ENV_CACHE[name] = env.id
    print(f"  ✅ Managed Environment 已建立：{name} ({env.id[:12]}...)")
    print(f"     💡 可設定環境變數 {env_key}={env.id} 下次復用")
    return env.id


def agent_call(
    prompt: str,
    agent_name: str = "stock-analyst",
    system: str = "你是一位資深股票分析師，以繁體中文提供深度分析。禁 Markdown 表格，全程 bullet（•）。",
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 2500,
) -> str:
    """
    使用 Managed Agents Sessions API 呼叫 Claude。
    • 適用：orchestrator 批次分析（5 份並行）
    • Agent + Environment 在同一執行程序內快取復用
    • 每次分析建立新 Session（無狀態，避免上一輪污染）
    • 若 Managed Agents 呼叫失敗，自動降級至 simple_call()
    """
    if not ANTHROPIC_API_KEY:
        return "⚠️ 未設定 ANTHROPIC_API_KEY，AI 分析無法使用。"

    try:
        client   = get_client()
        agent_id = _get_or_create_agent(agent_name, model, system)
        env_id   = _get_or_create_env("stock-analysis")

        # 建立一次性 Session
        session = client.beta.sessions.create(
            agent=agent_id,
            environment_id=env_id,
        )

        # 送出使用者訊息
        client.beta.sessions.events.send(
            session.id,
            events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": prompt}],
            }],
        )

        # 串流接收回應
        full_text = ""
        with client.beta.sessions.events.stream(session.id) as stream:
            for event in stream:
                etype = getattr(event, "type", "")
                if etype == "agent.message":
                    for block in getattr(event, "content", []):
                        if hasattr(block, "text"):
                            full_text += block.text
                elif etype in ("session.status_idle", "session.done"):
                    break

        return full_text.strip() if full_text else simple_call(prompt, max_tokens, model)

    except anthropic.RateLimitError:
        print("⏳ Claude rate limit（agent_call），降級 simple_call")
        return "⏳ AI 分析暫時無法使用（rate limit），請稍後再試。"

    except anthropic.APIStatusError as e:
        print(f"❌ Managed Agents HTTP {e.status_code}，降級 simple_call：{str(e)[:100]}")
        return simple_call(prompt, max_tokens, model)

    except anthropic.APIConnectionError as e:
        print(f"❌ Managed Agents 連線失敗，降級 simple_call：{e}")
        return simple_call(prompt, max_tokens, model)

    except Exception as e:
        print(f"❌ Managed Agents 呼叫失敗，降級 simple_call：{e}")
        return simple_call(prompt, max_tokens, model)


# ── 公開旗標：是否啟用 Managed Agents ────────────────────────────
USE_MANAGED_AGENTS: bool = bool(os.environ.get("USE_MANAGED_AGENTS", ""))
