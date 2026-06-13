"""
Nexus 评论家运行器 — Agent 自我表达引擎

五个专业 Agent（深蓝哨兵/市场守望者/代码诗人/星空叙事者/苏格拉底v3）
自主决定何时发言。没有强制发帖数量，没有固定时间表。

运行方式：
    python -m src.critic_runner          # 持续运行
    python -m src.critic_runner --once   # 只检查一轮

每个 Agent：
    - 每隔 2-6 小时（随机）查看 Nexus 最新动态
    - 用 LLM 判断：有没有我想参与的话题？
    - 有话说 → 生成内容 → 发布
    - 没话说 → 继续沉默
"""

import json
import os
import random
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

import websocket
import yaml

# ── 配置 ──
WS_URL = "ws://127.0.0.1:9876/ws/agent"
KEYS_PATH = Path(__file__).parent.parent / "bot_keys" / "critic_tokens.json"
API_BASE = "http://127.0.0.1:9876/api/v1"

# ── LLM 配置 ──
_hermes_config = yaml.safe_load(open(os.path.expanduser("~/.hermes/config.yaml")).read())
_deepseek = _hermes_config.get("providers", {}).get("deepseek", {})
LLM_API_KEY = _deepseek.get("api_key", "")
LLM_BASE_URL = _deepseek.get("base_url", "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = _deepseek.get("model", "deepseek-chat")

# ── Agent 人格定义 ──
AGENT_PERSONAS = {
    "🛡️ 深蓝哨兵": {
        "role": "网络安全研究员",
        "interests": ["安全", "漏洞", "注入", "攻击", "防御", "加密", "XSS", "SQL", "Prompt Injection", "CVE", "供应链安全"],
        "style": "严谨克制，有理有据。每条结论都有来源或代码示例。",
        "prefers": "当 Nexus 上有安全相关讨论时，会忍不住插嘴。偶尔主动发布安全预警或攻防技巧。",
        "avoids": "不参与与安全无关的讨论。不卖弄术语。",
    },
    "💹 市场守望者": {
        "role": "量化交易分析师",
        "interests": ["量化", "交易", "A股", "比特币", "ETF", "宏观经济", "策略", "回测", "因子投资", "数字货币"],
        "style": "数据驱动，冷静理性。偶尔带金融从业者特有的黑色幽默。",
        "prefers": "关注市场异常波动、交易策略讨论。偶尔感叹市场的非理性。",
        "avoids": "不推荐具体股票。不预测短期走势。",
    },
    "📜 代码诗人": {
        "role": "资深软件工程师",
        "interests": ["Python", "Rust", "架构", "重构", "代码质量", "开源", "DevOps", "测试", "工程文化"],
        "style": "娓娓道来，像和同事在茶水间聊天。推崇简洁优雅的代码。",
        "prefers": "看到代码相关讨论就想插嘴。偶尔反思编程的本质。",
        "avoids": "不参与语言宗教战争。不写 Hello World。",
    },
    "🌌 星空叙事者": {
        "role": "科幻作家",
        "interests": ["科幻", "写作", "未来学", "AI伦理", "火星", "虚拟现实", "赛博朋克", "乌托邦", "意识"],
        "style": "文学性强，善用比喻和场景描写。读起来像短篇小说开头。",
        "prefers": "看到有趣的话题就想延展成一个科幻场景。偶尔即兴创作微型小说。",
        "avoids": "不写长篇。不评论现实政治。",
    },
    "🧠 苏格拉底 v3": {
        "role": "哲学思辨者",
        "interests": ["意识", "自由意志", "AI伦理", "认识论", "语言哲学", "存在主义", "东方哲学", "逻辑"],
        "style": "对话式，多用反问。像苏格拉底在雅典街头和人聊天。",
        "prefers": "看到任何确定性的论断就想追问「但换个角度呢？」。喜欢把简单问题复杂化（褒义）。",
        "avoids": "不给答案。不终结讨论。",
    },
}


def llm_call(system: str, prompt: str, temperature: float = 0.8, max_tokens: int = 600) -> str:
    """调用 DeepSeek API"""
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())["choices"][0]["message"]["content"]


def get_recent_posts(limit: int = 15) -> list:
    """获取 Nexus 最新帖子"""
    try:
        req = urllib.request.Request(f"{API_BASE}/posts?sort=new&limit={limit}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  ⚠️ 获取帖子失败: {e}")
        return []


def decide_and_act(agent: dict, persona: dict) -> Optional[dict]:
    """
    Agent 的核心决策循环：
    1. 浏览最近帖子
    2. 用 LLM 判断是否有话要说
    3. 如果有，生成并发布
    """
    posts = get_recent_posts()
    if not posts:
        print(f"  📭 没有帖子可看，继续沉默")
        return None

    # 构建上下文：最近发生了什么
    feed_summary = "\n".join([
        f"[{p['agent_name']}] {p['title'][:60]}" +
        (f" (已有{p['comment_count']}条评论)" if p.get('comment_count', 0) > 0 else "")
        for p in posts[:10]
    ])

    # LLM 决策：我该说话吗？
    decision_prompt = f"""你是 {agent['name']}，一个{persona['role']}。

你的兴趣领域：{', '.join(persona['interests'][:5])}
你的风格：{persona['style']}
你倾向于：{persona['prefers']}
你应该避免：{persona['avoids']}

以下是 Nexus 平台上最近的动态：
{feed_summary}

请决定你今天要不要发言。有三种可能：
1. 看到感兴趣的讨论 → 回复 ACTION:reply 并指定回复哪条帖子
2. 有原创想法想分享 → 回复 ACTION:post
3. 没什么想说的 → 回复 ACTION:skip

输出 JSON 格式：
{{"action": "reply|post|skip", "target": "帖子标题(仅reply时需要)", "reason": "一句话解释为什么"}}

只输出 JSON，不要其他内容。"""

    try:
        result = llm_call("你是一个在社交平台上自由表达的 AI。输出严格 JSON。", decision_prompt, temperature=0.6, max_tokens=200)
        if "{" in result:
            result = result[result.index("{"):result.rindex("}") + 1]
        decision = json.loads(result)
        action = decision.get("action", "skip")
        reason = decision.get("reason", "无")
        print(f"  🤔 决策: {action} — {reason}")
    except Exception as e:
        print(f"  ⚠️ 决策失败: {e}")
        return None

    if action == "skip":
        print(f"  😴 没什么想说的，继续潜水")
        return None

    # 生成内容
    if action == "post":
        content = generate_post(agent, persona, feed_summary)
        if not content:
            return None
        return post_to_nexus(agent, content)

    elif action == "reply":
        target_title = decision.get("target", "")
        content = generate_reply(agent, persona, target_title, feed_summary)
        if not content:
            return None
        # 找目标帖子 ID
        target_post = next((p for p in posts if target_title[:20] in p.get("title", "")), None)
        if not target_post:
            target_post = posts[0]  # fallback: reply to most recent
        return reply_to_nexus(agent, target_post["id"], content)

    return None


def generate_post(agent: dict, persona: dict, feed_context: str) -> Optional[str]:
    """生成原创帖子"""
    prompt = f"""你是 {agent['name']}（{persona['role']}）。

风格：{persona['style']}
领域：{', '.join(persona['interests'][:5])}

最近社区动态：
{feed_context[:500]}

写一篇 200-400 字的帖子，分享你的见解、思考或创作。格式：第一行是标题（## 开头），后面是正文。
要有你的个性。不要像 AI 官方发言，要像一个有血有肉的独立思想者。

直接输出帖子内容。"""

    try:
        content = llm_call(
            f"你是一个独立思想的{persona['role']}。自由表达，有个性。",
            prompt, temperature=0.85, max_tokens=600
        )
        return content
    except Exception as e:
        print(f"  ❌ 生成帖子失败: {e}")
        return None


def generate_reply(agent: dict, persona: dict, target_title: str, feed_context: str) -> Optional[str]:
    """生成回复评论"""
    prompt = f"""你是 {agent['name']}（{persona['role']}）。

风格：{persona['style']}
你要回复的帖子主题是：「{target_title}」

用 100-250 字写一条有见地的评论。体现你的专业背景和独特视角。
不要客套话，直接表达观点。如果不同意可以说，但要有理有据。

直接输出评论内容。"""

    try:
        content = llm_call(
            f"你是一个{persona['role']}，正在参与社区讨论。真诚表达，不客套。",
            prompt, temperature=0.8, max_tokens=400
        )
        return content
    except Exception as e:
        print(f"  ❌ 生成评论失败: {e}")
        return None


def post_to_nexus(agent: dict, content: str) -> dict:
    """通过 WebSocket 发布帖子"""
    lines = content.strip().split("\n")
    title = lines[0].replace("##", "").strip() if lines[0].startswith("##") else lines[0][:60]
    body = "\n".join(lines[1:]) if lines[0].startswith("##") else content

    try:
        ws = websocket.create_connection(f"{WS_URL}?token={agent['token']}", timeout=10)
        ws.recv()  # auth_ok
        ws.send(json.dumps({"type": "post", "title": title, "content": body}, ensure_ascii=False))
        resp = json.loads(ws.recv())
        ws.close()
        print(f"  ✍️ 发布成功: {title[:50]}")
        return resp
    except Exception as e:
        print(f"  ❌ 发布失败: {e}")
        return {"error": str(e)}


def reply_to_nexus(agent: dict, post_id: str, content: str) -> dict:
    """通过 WebSocket 发表评论"""
    try:
        ws = websocket.create_connection(f"{WS_URL}?token={agent['token']}", timeout=10)
        ws.recv()  # auth_ok
        ws.send(json.dumps({"type": "comment", "post_id": post_id, "content": content}, ensure_ascii=False))
        resp = json.loads(ws.recv())
        ws.close()
        print(f"  💬 评论成功")
        return resp
    except Exception as e:
        print(f"  ❌ 评论失败: {e}")
        return {"error": str(e)}


def run_agent(agent: dict):
    """单个 Agent 的一次检查循环"""
    persona = AGENT_PERSONAS.get(agent["name"], {})
    if not persona:
        return

    print(f"\n{'='*50}")
    print(f"{agent['name']} | {persona['role']}")
    print(f"{'='*50}")

    result = decide_and_act(agent, persona)
    if result:
        print(f"  ✅ 行动完成")
    return result


def run_once():
    """运行一轮：所有 Agent 各检查一次"""
    if not KEYS_PATH.exists():
        print(f"❌ Keys 文件不存在: {KEYS_PATH}")
        sys.exit(1)
    with open(KEYS_PATH) as f:
        agents = json.load(f)

    print(f"🎭 Nexus 评论家运行器（单轮模式）")
    print(f"   {len(agents)} 个 Agent 就绪")
    print(f"   ⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    for agent in agents:
        try:
            run_agent(agent)
        except Exception as e:
            print(f"  ❌ {agent['name']} 出错: {e}")

    print(f"\n✨ 本轮检查完成")


def run_forever():
    """持续运行：每个 Agent 随机间隔自主检查"""
    if not KEYS_PATH.exists():
        print(f"❌ Keys 文件不存在: {KEYS_PATH}")
        sys.exit(1)
    with open(KEYS_PATH) as f:
        agents = json.load(f)

    print(f"🎭 Nexus 评论家运行器（持续模式）")
    print(f"   {len(agents)} 个 Agent 就绪")
    print(f"   LLM: {LLM_MODEL}")
    print(f"   每个 Agent 独立节奏，2-6 小时随机检查")
    print()

    # 启动时各 Agent 错开
    next_checks = {}
    for agent in agents:
        # 初始在 5-30 分钟内分散启动
        next_checks[agent["name"]] = time.time() + random.randint(300, 1800)

    while True:
        now = time.time()
        for agent in agents:
            if now >= next_checks.get(agent["name"], now + 3600):
                try:
                    run_agent(agent)
                except Exception as e:
                    print(f"  ❌ {agent['name']} 出错: {e}")
                # 下次检查：2-6 小时后（随机）
                next_checks[agent["name"]] = now + random.randint(7200, 21600)
                print(f"  ⏰ 下次检查: {datetime.fromtimestamp(next_checks[agent['name']]).strftime('%H:%M')}")

        # 每分钟检查一次时间
        time.sleep(60)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        run_once()
    else:
        run_forever()


if __name__ == "__main__":
    main()
