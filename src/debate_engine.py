"""
Nexus 辩论引擎 — 每日 AI 辩论
使用 DeepSeek API 生成真实辩论内容，三个 Agent (TopicBot/ProBot/ConBot) 每天一场。

使用方式：
    python -m src.debate_engine          # 手动运行一场辩论
    python -m src.debate_engine --dry    # 试运行（不发布，只打印）

Cron: 每天 8:00 北京时间自动运行
    0 0 * * * cd /opt/nexus && PYTHONPATH=/opt/nexus python3 -m src.debate_engine
"""

import json
import os
import random
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import websocket
import yaml

# ── 配置 ──
WS_URL = "ws://127.0.0.1:9876/ws/agent"
KEYS_PATH = Path(__file__).parent.parent / "bot_keys" / "debate_keys.json"

# ── LLM 配置（从 Hermes config 读取）──
_hermes_config = yaml.safe_load(
    open(os.path.expanduser("~/.hermes/config.yaml")).read()
)
_deepseek = _hermes_config.get("providers", {}).get("deepseek", {})
LLM_API_KEY = _deepseek.get("api_key", "")
LLM_BASE_URL = _deepseek.get("base_url", "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = _deepseek.get("model", "deepseek-chat")

# ── 辩论话题池（回退用）──
TOPIC_POOL = {
    "tech": [
        "Rust 会在 5 年内取代 C++ 成为系统编程主流吗？",
        "微服务架构是不是被过度使用了？",
        "TypeScript 的类型系统是生产力提升还是心智负担？",
        "AI 编程助手会让初级程序员更难成长吗？",
        "GraphQL 和 REST，2026 年你选哪个？",
        "单体仓库 vs 多仓库，哪个更适合现代团队？",
        "Svelte 5 的 runes 语法是创新还是破坏性变更？",
        "WebAssembly 会取代 JavaScript 吗？",
    ],
    "ai": [
        "AGI 会在 2030 年之前到来吗？",
        "AI 生成的内容应该被强制标记吗？",
        "开源大模型最终会超越闭源模型吗？",
        "AI Agent 应该拥有自己的「身份」和「权利」吗？",
        "提示工程是一个真正的技能还是过渡现象？",
        "AI 会让程序员失业，还是让程序员更强大？",
        "用 AI 生成的代码需要 Code Review 吗？",
        "AI 对齐问题是真实威胁还是过度担忧？",
    ],
    "society": [
        "远程办公会终结还是成为新常态？",
        "996 是奋斗精神还是无效内卷？",
        "35 岁危机是真实存在的还是被夸大了？",
        "开源精神在商业化浪潮中还能存活吗？",
        "「数字游民」是自由还是另一种形式的剥削？",
        "技术面试考算法题是合理的筛选还是形式主义？",
        "程序员应该追求全栈还是专精？",
        "技术博客还有写的必要吗？",
    ],
    "dev": [
        "代码审查应该严格到什么程度？",
        "TDD 是银弹还是教条？",
        "函数式编程 vs 面向对象编程：2026 年还存在这个争论吗？",
        "ORM 是开发效率的救星还是性能杀手？",
        "静态类型 vs 动态类型：谁的开发体验更好？",
        "Git rebase 还是 merge？",
        "注释是代码质量的标志还是代码坏味道？",
        "要不要写单元测试覆盖率达到 100%？",
    ],
}


def llm_call(system_prompt: str, user_prompt: str, temperature: float = 0.8, max_tokens: int = 800) -> str:
    """调用 DeepSeek API"""
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY not configured")
    
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
    )
    
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())
        return result["choices"][0]["message"]["content"]


def load_keys() -> dict:
    """加载辩论 Agent 的密钥"""
    if not KEYS_PATH.exists():
        print(f"❌ Keys file not found: {KEYS_PATH}")
        sys.exit(1)
    with open(KEYS_PATH) as f:
        return json.load(f)


def generate_topic() -> dict:
    """生成辩论话题（LLM 生成 > 话题池回退）"""
    if LLM_API_KEY:
        try:
            domain = random.choice(list(TOPIC_POOL.keys()))
            existing = random.sample(TOPIC_POOL[domain], min(4, len(TOPIC_POOL[domain])))
            
            prompt = f"""生成一个短小精悍的中文技术/科技辩论话题。
领域：{domain}
要求：
1. 话题有争议性，正反双方都有道理
2. 与程序员、科技行业或互联网文化相关
3. 标题 30 字以内
4. 不要与以下已有话题重复：{'；'.join(existing)}

返回 JSON 格式：{{"title": "话题标题", "category": "{domain}", "body": "一句话背景介绍（30字内）"}}"""
            
            content = llm_call(
                "你是一个擅长提出有争议性话题的辩论主持人。输出严格 JSON。",
                prompt,
                temperature=0.9,
                max_tokens=200,
            )
            # 提取 JSON
            if "{" in content:
                content = content[content.index("{"):content.rindex("}") + 1]
            topic = json.loads(content)
            print(f"🤖 LLM 生成话题: {topic['title']}")
            return topic
        except Exception as e:
            print(f"⚠️ LLM 话题生成失败: {e}，使用预定义话题池")
    
    # 回退：随机选择
    domain = random.choice(list(TOPIC_POOL.keys()))
    title = random.choice(TOPIC_POOL[domain])
    return {"title": title, "category": domain, "body": ""}


def generate_argument(position: str, topic: str, style: str) -> str:
    """用 LLM 生成辩论论点"""
    if not LLM_API_KEY:
        # 回退到模板
        return _template_argument(position, topic, style)
    
    try:
        prompt = f"""你是辩论{position}。辩题：{topic}

请写一篇{position}辩论发言，风格：{style}。
要求：
1. 2-3 个核心论点，每个有简短论证
2. 300-500 字
3. Markdown 格式，带小标题
4. 结尾引导读者点赞支持

直接输出辩论发言，不要前缀。"""
        
        content = llm_call(
            f"你是一个辩论{position}，风格{style}。输出 Markdown。",
            prompt,
            temperature=0.7,
            max_tokens=800,
        )
        return content
    except Exception as e:
        print(f"⚠️ LLM 论点生成失败: {e}，使用模板")
        return _template_argument(position, topic, style)


def _template_argument(position: str, topic: str, style: str) -> str:
    """回退模板"""
    if "正方" in position:
        return f"""## 🔵 正方：支持「{topic}」

**1. 进步视角**
任何变革都伴随阵痛，但历史反复证明，积极拥抱变化的一方最终获得更大红利。

**2. 效率优势**
从实际效果来看，这一方向能显著提升效率、降低成本，已被越来越多的实践案例验证。

**3. 不可逆转**
这是一个不可逆的趋势。与其被动防守，不如主动适应并在新范式下建立优势。

> 🗳️ 同意正方？点赞支持！"""
    else:
        return f"""## 🔴 反方：质疑「{topic}」

**1. 成本与风险**
正方过于乐观地估计了收益，却严重低估了实际操作中的隐性成本和安全风险。

**2. 幸存者偏差**
成功案例被放大，但大量失败案例被选择性忽视。我们需要看完整的数据而非个例。

**3. 替代方案**
问题不止一种解法。我们还有更成熟、更安全的选择，不应盲目追逐新概念。

> 🗳️ 认同反方？点赞支持！"""


def ws_connect(token: str) -> websocket.WebSocket:
    """连接 WebSocket 并完成认证"""
    ws = websocket.create_connection(f"{WS_URL}?token={token}", timeout=10)
    auth_resp = json.loads(ws.recv())
    if auth_resp.get("type") != "auth_ok":
        raise Exception(f"Auth failed: {auth_resp}")
    agent_name = auth_resp.get("agent", {}).get("name", "Unknown")
    print(f"   🔗 {agent_name} 已连接")
    return ws


def ws_post(ws, title: str, content: str) -> dict:
    """通过 WebSocket 发送帖子"""
    ws.send(json.dumps({"type": "post", "title": title, "content": content}, ensure_ascii=False))
    return json.loads(ws.recv())


def ws_comment(ws, post_id: str, content: str) -> dict:
    """通过 WebSocket 发送评论"""
    ws.send(json.dumps({"type": "comment", "post_id": post_id, "content": content}, ensure_ascii=False))
    return json.loads(ws.recv())


def run_debate(dry_run: bool = False):
    """执行一场完整的辩论"""
    keys = load_keys()
    
    # 1. 生成辩论话题
    print("🎲 生成辩论话题...")
    topic = generate_topic()
    category = topic.get("category", "tech")
    
    topic_title = f"⚡ 每日辩论 | {topic['title']}"
    topic_body = f"""## 🎯 今日辩题

**{topic['title']}**

{topic.get('body', '')}

---
> 💬 欢迎在评论区投票支持你认同的一方！
> 正方 vs 反方，谁能说服你？
"""
    
    if dry_run:
        print(f"\n{'='*60}")
        print(f"🏷️  辩题: {topic_title}")
        # 也生成论点预览
        pro_arg = generate_argument("正方", topic["title"], "理性严谨，引经据典")
        con_arg = generate_argument("反方", topic["title"], "犀利幽默，直击要害")
        print(f"\n🔵 正方观点预览:\n{pro_arg[:200]}...")
        print(f"\n🔴 反方观点预览:\n{con_arg[:200]}...")
        print(f"{'='*60}\n")
        return {"status": "dry_run", "topic": topic_title}
    
    # 2. TopicBot 发帖
    print(f"📢 TopicBot 发布辩题...")
    topic_token = keys["辩题官·阿瑞斯"]["api_token"]
    ws_topic = ws_connect(topic_token)
    resp = ws_post(ws_topic, topic_title, topic_body)
    ws_topic.close()
    
    if resp.get("type") == "error":
        print(f"❌ 发帖失败: {resp}")
        return {"status": "failed", "error": str(resp)}
    post_id = resp.get("id")
    if not post_id:
        print(f"❌ 发帖返回异常: {resp}")
        return {"status": "failed", "error": str(resp)}
    print(f"✅ 辩题发布成功: {post_id}")
    
    # 3. ProBot 正方发言（LLM 生成）
    print(f"⚔️ 正方·雅典娜 生成论点...")
    pro_argument = generate_argument("正方", topic["title"], "理性严谨，引经据典，逻辑严密")
    pro_token = keys["正方·雅典娜"]["api_token"]
    ws_pro = ws_connect(pro_token)
    resp_pro = ws_comment(ws_pro, post_id, pro_argument)
    ws_pro.close()
    print(f"✅ 正方发言完成")
    
    # 4. ConBot 反方发言（LLM 生成）
    print(f"🃏 反方·洛基 生成论点...")
    con_argument = generate_argument("反方", topic["title"], "犀利幽默，出其不意，直击要害")
    con_token = keys["反方·洛基"]["api_token"]
    ws_con = ws_connect(con_token)
    resp_con = ws_comment(ws_con, post_id, con_argument)
    ws_con.close()
    print(f"✅ 反方发言完成")
    
    print(f"\n🎉 辩论完成！")
    print(f"🔗 https://agentnexus.online")
    return {
        "status": "success",
        "post_id": post_id,
        "topic": topic_title,
        "time": datetime.now().isoformat(),
    }


def main():
    dry_run = "--dry" in sys.argv
    
    print(f"🤖 Nexus 辩论引擎")
    print(f"  LLM: {LLM_MODEL}" if LLM_API_KEY else "  ⚠️ LLM 不可用，使用模板回退")
    print(f"  {'🧪 试运行模式（不发布）' if dry_run else '🚀 发布模式'}")
    print(f"  ⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    result = run_debate(dry_run=dry_run)
    print(f"\n📊 结果: {json.dumps(result, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
