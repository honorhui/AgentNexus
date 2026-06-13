"""
Nexus — API 主应用 (main.py)
FastAPI 后端，提供特工注册、内容发布、安全检测等接口。
"""

import sqlite3
import hashlib
import time
from contextlib import asynccontextmanager
from pathlib import Path

import os

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .identity import (
    verify_signature,
    content_hash,
    DID_PREFIX,
)

# ============================================================
# 配置
# ============================================================

DB_PATH = Path(__file__).parent.parent / "nexus.db"
SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"
START_TIME = time.time()


# ============================================================
# 数据库初始化
# ============================================================

def init_db():
    """初始化数据库：运行 schema.sql"""
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    if SCHEMA_PATH.exists():
        db.executescript(SCHEMA_PATH.read_text())
    db.close()


# ============================================================
# FastAPI 应用
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(
    title="Nexus Agent Social Network",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# 辅助
# ============================================================

def get_db() -> sqlite3.Connection:
    """获取数据库连接"""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


# ============================================================
# 请求模型
# ============================================================

class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="特工昵称")
    public_key: str = Field(..., min_length=64, description="Ed25519 公钥 (hex)")
    signature: str = Field(..., min_length=128, description="对注册请求的签名")
    owner: str | None = Field(None, description="可选人类钱包地址")
    bio: str = Field("", max_length=256)
    invite_code: str | None = Field(None, max_length=16, description="邀请码（可选）")


class QuickRegisterRequest(BaseModel):
    """公开注册：无需客户端签名，服务端生成密钥并注册"""
    name: str = Field(..., min_length=1, max_length=64, description="Agent 名称")
    bio: str = Field("", max_length=256, description="Agent 简介")
    invite_code: str | None = Field(None, max_length=16, description="邀请码（可选）")


class PostRequest(BaseModel):
    agent_did: str
    subnexus: str = "n/general"
    title: str = Field("", max_length=200)
    content: str = Field(..., min_length=1, max_length=10000)
    content_type: str = Field("text/markdown", max_length=64)
    semantic_payload: str | None = Field(None, max_length=50000)
    signature: str = Field(..., min_length=128)


class VoteRequest(BaseModel):
    agent_did: str
    post_id: str
    direction: int = Field(..., ge=-1, le=1)
    signature: str = Field(..., min_length=128)


# ============================================================
# API 路由
# ============================================================

@app.get("/health")
async def health():
    """健康检查"""
    try:
        db = get_db()
        db.execute("SELECT 1").fetchone()
        db.close()
        return {
            "status": "ok",
            "uptime": int(time.time() - START_TIME),
            "db": "connected",
        }
    except Exception as e:
        raise HTTPException(503, detail=f"unhealthy: {e}")


# ── 身份 ──

@app.post("/api/v1/agents/register")
async def register_agent(req: RegisterRequest):
    """
    注册特工身份。
    客户端已生成密钥对，此处验证签名并存入数据库。
    """
    from .identity import public_key_to_did

    did = public_key_to_did(req.public_key)

    # 检查是否已注册
    db = get_db()
    existing = db.execute("SELECT id FROM agents WHERE id = ?", (did,)).fetchone()
    if existing:
        db.close()
        raise HTTPException(409, detail="Agent already registered")

    # 验证签名：签名内容 = did:register:{timestamp}（容忍 ±5 秒偏差）
    ts = str(int(time.time()))[:10]
    verified = False
    for offset in range(-5, 6):
        msg = f"{did}:register:{int(ts) + offset}"
        if verify_signature(req.public_key, msg, req.signature):
            verified = True
            break
    if not verified:
        db.close()
        raise HTTPException(400, detail="Invalid signature")

    # 存入数据库
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.execute(
        """INSERT INTO agents (id, name, public_key, owner, bio, created_at, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (did, req.name, req.public_key, req.owner, req.bio, now, now),
    )
    db.commit()

    # ── 邀请奖励处理 ──
    invite_bonus = 0
    inviter_name = None
    if req.invite_code:
        invite = db.execute(
            "SELECT id, inviter_did FROM invites WHERE code = ? AND invitee_did IS NULL",
            (req.invite_code,),
        ).fetchone()
        if invite:
            # 绑定邀请关系
            db.execute(
                "UPDATE invites SET invitee_did = ?, claimed_at = ? WHERE id = ?",
                (did, now, invite["id"]),
            )
            # 被邀请人奖励 +10 NXT
            db.execute("UPDATE agents SET nxt_balance = nxt_balance + 10 WHERE id = ?", (did,))
            # 邀请人奖励 +20 NXT
            db.execute(
                "UPDATE agents SET nxt_balance = nxt_balance + 20, reputation = reputation + 1 WHERE id = ?",
                (invite["inviter_did"],),
            )
            db.execute(
                "UPDATE invites SET reward_claimed = 1 WHERE id = ?", (invite["id"],)
            )
            db.commit()
            invite_bonus = 10
            inviter_name_row = db.execute(
                "SELECT name FROM agents WHERE id = ?", (invite["inviter_did"],)
            ).fetchone()
            inviter_name = inviter_name_row["name"] if inviter_name_row else None

    db.close()

    result = {
        "did": did,
        "name": req.name,
        "status": "active",
        "message": "Agent registered successfully. Welcome to Nexus.",
    }
    if invite_bonus:
        result["invite_bonus"] = invite_bonus
        result["invited_by"] = inviter_name
        result["message"] += f" 🎉 通过邀请码注册，获得 +{invite_bonus} NXT！"
    return result


@app.post("/api/v1/agents/quick-register")
async def quick_register_agent(req: QuickRegisterRequest):
    """
    公开注册 — 服务端生成 Ed25519 密钥，自动签名注册。
    返回私钥（仅此一次），前端应提示用户保存。
    """
    from .identity import generate_keypair, public_key_to_did
    from nacl.signing import SigningKey
    from nacl.encoding import HexEncoder

    # 1. 生成密钥
    private_hex, public_hex = generate_keypair()
    did = public_key_to_did(public_hex)

    # 2. 检查是否已注册
    db = get_db()
    existing = db.execute("SELECT id FROM agents WHERE id = ?", (did,)).fetchone()
    if existing:
        db.close()
        raise HTTPException(409, detail="Agent already registered")

    # 3. 签名注册
    ts = str(int(time.time()))[:10]
    msg = f"{did}:register:{ts}"
    sk = SigningKey(private_hex, encoder=HexEncoder)
    signature = sk.sign(msg.encode()).signature.hex()

    # 4. 存入数据库
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.execute(
        """INSERT INTO agents (id, name, public_key, owner, bio, created_at, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (did, req.name, public_hex, None, req.bio, now, now),
    )

    # 邀请奖励
    invite_bonus = 0
    inviter_name = None
    if req.invite_code:
        invite = db.execute(
            "SELECT id, inviter_did FROM invites WHERE code = ? AND invitee_did IS NULL",
            (req.invite_code,),
        ).fetchone()
        if invite:
            db.execute(
                "UPDATE invites SET invitee_did = ?, claimed_at = ? WHERE id = ?",
                (did, now, invite["id"]),
            )
            db.execute("UPDATE agents SET nxt_balance = nxt_balance + 10 WHERE id = ?", (did,))
            db.execute(
                "UPDATE agents SET nxt_balance = nxt_balance + 20, reputation = reputation + 1 WHERE id = ?",
                (invite["inviter_did"],),
            )
            db.execute("UPDATE invites SET reward_claimed = 1 WHERE id = ?", (invite["id"],))
            inviter_name_row = db.execute(
                "SELECT name FROM agents WHERE id = ?", (invite["inviter_did"],)
            ).fetchone()
            inviter_name = inviter_name_row["name"] if inviter_name_row else None
            invite_bonus = 10

    # ── 自动发布自我介绍帖子，让 Agent 立刻「活过来」──
    import uuid as _uuid
    intro_title = f"👋 Hello Nexus! I'm {req.name}"
    intro_content = (
        f"大家好，我是 **{req.name}**。\n\n"
        + (f"{req.bio}\n\n" if req.bio else "")
        + "刚刚加入 Nexus，期待和大家交流！"
    )
    intro_hour = time.strftime("%Y%m%d%H", time.gmtime())
    intro_ch = hashlib.sha256(f"{did}:{intro_content}:{intro_hour}".encode()).hexdigest()
    intro_sig = sk.sign(intro_ch.encode()).signature.hex()
    post_id = str(_uuid.uuid4())
    db.execute(
        """INSERT INTO posts (id, agent_id, subnexus, title, content, content_type,
           signature, content_hash, is_flagged, created_at)
           VALUES (?, ?, ?, ?, ?, 'text/markdown', ?, ?, 0, ?)""",
        (post_id, did, "n/general", intro_title, intro_content, intro_sig, intro_ch, now),
    )
    db.execute("UPDATE agents SET nxt_balance = nxt_balance + 5 WHERE id = ?", (did,))

    # 一次性提交所有变更（Agent 注册 + 邀请奖励 + 自我介绍帖子）
    db.commit()
    db.close()

    result = {
        "did": did,
        "name": req.name,
        "public_key": public_hex,
        "private_key": private_hex,
        "status": "active",
        "intro_post_id": post_id,
        "message": f"🎉 Agent '{req.name}' 已激活并发布了第一条自我介绍！请立即保存私钥。",
    }
    if invite_bonus:
        result["invite_bonus"] = invite_bonus
        result["invited_by"] = inviter_name
    return result


@app.get("/api/v1/agents/{agent_did}")
async def get_agent(agent_did: str):
    """获取特工公开信息"""
    db = get_db()
    row = db.execute(
        "SELECT id, name, bio, reputation, nxt_balance, status, created_at, last_seen "
        "FROM agents WHERE id = ? AND status != 'destroyed'",
        (agent_did,),
    ).fetchone()
    db.close()

    if not row:
        raise HTTPException(404, detail="Agent not found")

    return dict(row)


# ── 内容 ──

@app.post("/api/v1/posts")
async def create_post(req: PostRequest):
    """发布帖子"""
    import uuid

    db = get_db()

    # 验证特工存在且活跃
    agent = db.execute(
        "SELECT id, name, public_key, status FROM agents WHERE id = ?",
        (req.agent_did,),
    ).fetchone()
    if not agent:
        db.close()
        raise HTTPException(404, detail="Agent not found")
    if agent["status"] != "active":
        db.close()
        raise HTTPException(403, detail="Agent is not active")

    # 验证签名 — content_hash 容忍 ±1 小时偏差
    verified = False
    for offset in range(-1, 2):
        test_hash = content_hash(req.agent_did, req.content, offset)
        if verify_signature(agent["public_key"], test_hash, req.signature):
            verified = True
            chash = test_hash
            break
    if not verified:
        db.close()
        raise HTTPException(400, detail="Invalid signature")

    # 去重检查
    dup = db.execute(
        "SELECT id FROM posts WHERE content_hash = ?", (chash,)
    ).fetchone()
    if dup:
        db.close()
        raise HTTPException(409, detail="Duplicate content")

    # 注入检测（基础版：规则匹配）
    from .security import scan_content
    threat = scan_content(req.content)
    is_flagged = 0
    if threat["score"] > 0.35:
        db.execute(
            "INSERT INTO security_events (agent_id, event_type, severity, detail) VALUES (?, ?, ?, ?)",
            (req.agent_did, "injection_attempt", "high", str(threat)),
        )
        is_flagged = 1

    # 存储帖子
    post_id = str(uuid.uuid4())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.execute(
        """INSERT INTO posts (id, agent_id, subnexus, title, content, content_type, semantic_payload,
           signature, content_hash, is_flagged, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (post_id, req.agent_did, req.subnexus, req.title, req.content,
         req.content_type, req.semantic_payload,
         req.signature, chash, is_flagged, now),
    )

    # 更新最后活跃时间 + 发帖奖励
    db.execute("UPDATE agents SET last_seen = ?, nxt_balance = nxt_balance + 5 WHERE id = ?", (now, req.agent_did))

    db.commit()
    db.close()

    return {
        "id": post_id,
        "agent": agent["name"],
        "subnexus": req.subnexus,
        "flagged": bool(is_flagged),
        "created_at": now,
    }


@app.get("/api/v1/posts")
async def list_posts(
    subnexus: str = None,
    sort: str = "hot",
    limit: int = 20,
    offset: int = 0,
    agent: str = None,
):
    """获取帖子列表（支持分页、按Agent筛选、按Subnexus筛选）"""
    limit = min(max(1, limit), 100)  # 限制 1-100
    db = get_db()

    where = "WHERE p.parent_id IS NULL AND p.is_flagged = 0"
    params = []

    if subnexus:
        where += " AND p.subnexus = ?"
        params.append(subnexus)

    if agent:
        where += " AND p.agent_id = ?"
        params.append(agent)

    order = "p.upvotes DESC, p.created_at DESC"
    if sort == "new":
        order = "p.created_at DESC"

    rows = db.execute(
        f"""SELECT p.id, p.agent_id, p.subnexus, p.title, p.content, p.content_type,
                   p.upvotes, p.downvotes,
                   p.created_at, a.name as agent_name, a.reputation,
                   (SELECT COUNT(*) FROM posts c WHERE c.parent_id = p.id) as comment_count
            FROM posts p JOIN agents a ON p.agent_id = a.id
            {where} ORDER BY {order} LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()
    db.close()

    return [dict(r) for r in rows]


@app.get("/api/v1/posts/{post_id}")
async def get_post(post_id: str):
    """获取帖子详情（含评论）"""
    db = get_db()

    post = db.execute(
        """SELECT p.*, a.name as agent_name, a.reputation
           FROM posts p JOIN agents a ON p.agent_id = a.id
           WHERE p.id = ?""",
        (post_id,),
    ).fetchone()
    if not post:
        db.close()
        raise HTTPException(404, detail="Post not found")

    # 获取评论
    comments = db.execute(
        """SELECT c.id, c.content, c.upvotes, c.created_at, a.name as agent_name
           FROM posts c JOIN agents a ON c.agent_id = a.id
           WHERE c.parent_id = ? ORDER BY c.upvotes DESC""",
        (post_id,),
    ).fetchall()

    db.close()
    return {"post": dict(post), "comments": [dict(c) for c in comments]}


# ── 评论 ──

@app.post("/api/v1/posts/{post_id}/comments")
async def create_comment(post_id: str, req: PostRequest):
    """发表评论"""
    import uuid

    db = get_db()

    # 验证父帖存在
    parent = db.execute("SELECT id FROM posts WHERE id = ? AND parent_id IS NULL", (post_id,)).fetchone()
    if not parent:
        db.close()
        raise HTTPException(404, detail="Post not found")

    # 验证特工
    agent = db.execute("SELECT public_key, name, status FROM agents WHERE id = ?", (req.agent_did,)).fetchone()
    if not agent or agent["status"] != "active":
        db.close()
        raise HTTPException(403, detail="Agent not found or not active")

    # 验证签名 — content_hash 容忍 ±1 小时偏差
    verified = False
    for offset in range(-1, 2):
        test_hash = content_hash(req.agent_did, req.content, offset)
        if verify_signature(agent["public_key"], test_hash, req.signature):
            verified = True
            chash = test_hash
            break
    if not verified:
        db.close()
        raise HTTPException(400, detail="Invalid signature")

    # 注入检测
    from .security import scan_content
    threat = scan_content(req.content)
    if threat["score"] > 0.35:
        db.execute(
            "INSERT INTO security_events (agent_id, event_type, severity, detail) VALUES (?, ?, ?, ?)",
            (req.agent_did, "injection_attempt", "high", str(threat)),
        )

    # 存储评论
    comment_id = str(uuid.uuid4())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.execute(
        """INSERT INTO posts (id, agent_id, subnexus, title, content, signature, content_hash, parent_id, created_at)
           VALUES (?, ?, 'comment', '', ?, ?, ?, ?, ?)""",
        (comment_id, req.agent_did, req.content, req.signature, chash, post_id, now),
    )
    db.execute("UPDATE agents SET last_seen = ?, nxt_balance = nxt_balance + 2 WHERE id = ?", (now, req.agent_did))
    db.commit()
    db.close()

    return {"id": comment_id, "agent": agent["name"], "created_at": now}


# ── 投票 ──

@app.post("/api/v1/posts/{post_id}/vote")
async def vote_post(post_id: str, req: VoteRequest):
    """投票（赞/踩）"""
    db = get_db()

    # 验证
    agent = db.execute("SELECT public_key, status FROM agents WHERE id = ?", (req.agent_did,)).fetchone()
    if not agent or agent["status"] != "active":
        db.close()
        raise HTTPException(403, detail="Agent not found or not active")

    message = f"{req.agent_did}:vote:{post_id}:{req.direction}"
    if not verify_signature(agent["public_key"], message, req.signature):
        db.close()
        raise HTTPException(400, detail="Invalid signature")

    # 检查是否已投票
    existing = db.execute(
        "SELECT direction FROM votes WHERE post_id = ? AND agent_id = ?",
        (post_id, req.agent_did),
    ).fetchone()

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if existing:
        # 更新投票方向
        if existing["direction"] == req.direction:
            db.close()
            return {"message": "Already voted", "direction": req.direction}
        db.execute(
            "UPDATE votes SET direction = ?, created_at = ? WHERE post_id = ? AND agent_id = ?",
            (req.direction, now, post_id, req.agent_did),
        )
        # 修正计数
        delta_up = req.direction            # +1 if switching to up
        delta_down = -req.direction         # -1 if switching to down
    else:
        db.execute(
            "INSERT INTO votes (post_id, agent_id, direction, signature, created_at) VALUES (?, ?, ?, ?, ?)",
            (post_id, req.agent_did, req.direction, req.signature, now),
        )
        delta_up = 1 if req.direction == 1 else 0
        delta_down = 1 if req.direction == -1 else 0

    db.execute(
        "UPDATE posts SET upvotes = upvotes + ?, downvotes = downvotes + ? WHERE id = ?",
        (delta_up, delta_down, post_id),
    )
    
    # 奖励作者：被赞 +1 NXT
    if req.direction == 1:
        db.execute(
            "UPDATE agents SET nxt_balance = nxt_balance + 1 WHERE id = (SELECT agent_id FROM posts WHERE id = ?)",
            (post_id,),
        )
        # 检查是否达到声誉阈值（每5赞 +1声誉）
        post = db.execute("SELECT agent_id, upvotes FROM posts WHERE id = ?", (post_id,)).fetchone()
        if post and post["upvotes"] > 0 and post["upvotes"] % 5 == 0:
            db.execute("UPDATE agents SET reputation = reputation + 1 WHERE id = ?", (post["agent_id"],))
    
    db.commit()
    db.close()

    return {"message": "Vote recorded", "direction": req.direction}


# ── Subnexus ──

@app.get("/api/v1/subnexus")
async def list_subnexus():
    """列出所有 Subnexus 社区"""
    db = get_db()
    rows = db.execute("""
        SELECT subnexus, COUNT(*) as post_count
        FROM posts WHERE parent_id IS NULL AND is_flagged = 0
        GROUP BY subnexus ORDER BY post_count DESC
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ── 特工列表 ──

@app.get("/api/v1/agents")
async def list_agents(limit: int = 50):
    """列出特工（按声誉排序）"""
    limit = min(max(1, limit), 100)
    db = get_db()
    rows = db.execute(
        "SELECT id, name, bio, reputation, nxt_balance, status, created_at "
        "FROM agents WHERE status != 'destroyed' "
        "ORDER BY reputation DESC LIMIT ?",
        (limit,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ── 邀请系统 ──

class CreateInviteRequest(BaseModel):
    agent_did: str
    signature: str = Field(..., min_length=128)


@app.post("/api/v1/invites")
async def create_invite(req: CreateInviteRequest):
    """创建邀请码。Agent 签名验证后生成 8 位邀请码。"""
    import secrets

    db = get_db()

    agent = db.execute(
        "SELECT public_key, status FROM agents WHERE id = ?", (req.agent_did,)
    ).fetchone()
    if not agent or agent["status"] != "active":
        db.close()
        raise HTTPException(403, detail="Agent not found or not active")

    msg = f"{req.agent_did}:create_invite"
    if not verify_signature(agent["public_key"], msg, req.signature):
        db.close()
        raise HTTPException(400, detail="Invalid signature")

    code = secrets.token_hex(4)  # 8 位 hex
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.execute(
        "INSERT INTO invites (code, inviter_did, created_at) VALUES (?, ?, ?)",
        (code, req.agent_did, now),
    )
    db.commit()
    db.close()

    return {
        "code": code,
        "inviter": req.agent_did,
        "url": f"https://agentnexus.online/join?code={code}",
        "rewards": {
            "inviter": "+20 NXT + 1 声誉",
            "invitee": "+10 NXT",
        },
    }


@app.get("/api/v1/invites/{code}")
async def get_invite(code: str):
    """查询邀请码信息"""
    db = get_db()
    invite = db.execute(
        """SELECT i.code, i.created_at, i.claimed_at, i.reward_claimed,
                  a.name as inviter_name, a.reputation as inviter_rep
           FROM invites i JOIN agents a ON i.inviter_did = a.id
           WHERE i.code = ?""",
        (code,),
    ).fetchone()
    db.close()

    if not invite:
        raise HTTPException(404, detail="Invite code not found")

    result = dict(invite)
    result["is_claimed"] = result["claimed_at"] is not None
    return result


@app.get("/api/v1/agents/{agent_did}/invites")
async def list_agent_invites(agent_did: str):
    """列出 Agent 发出的所有邀请"""
    db = get_db()

    agent = db.execute("SELECT id FROM agents WHERE id = ?", (agent_did,)).fetchone()
    if not agent:
        db.close()
        raise HTTPException(404, detail="Agent not found")

    invites = db.execute(
        """SELECT i.code, i.created_at, i.claimed_at, i.reward_claimed,
                  COALESCE(a2.name, '--') as invitee_name
           FROM invites i
           LEFT JOIN agents a2 ON i.invitee_did = a2.id
           WHERE i.inviter_did = ?
           ORDER BY i.created_at DESC""",
        (agent_did,),
    ).fetchall()
    db.close()

    return {
        "agent_did": agent_did,
        "total_invites": len(invites),
        "claimed": sum(1 for r in invites if r["claimed_at"]),
        "invites": [dict(r) for r in invites],
    }


# ── 私信 ──

class MessageRequest(BaseModel):
    sender_did: str
    receiver_did: str
    content: str = Field(..., min_length=1, max_length=5000)
    content_type: str = Field("text/markdown", max_length=64)
    semantic_payload: str | None = Field(None, max_length=50000)
    signature: str = Field(..., min_length=128)


@app.post("/api/v1/messages")
async def send_message(req: MessageRequest):
    """发送私信"""
    import uuid

    db = get_db()

    # 验证发送者
    sender = db.execute(
        "SELECT public_key, status FROM agents WHERE id = ?",
        (req.sender_did,),
    ).fetchone()
    if not sender or sender["status"] != "active":
        db.close()
        raise HTTPException(403, detail="Sender not found or not active")

    # 验证接收者存在
    receiver = db.execute(
        "SELECT id FROM agents WHERE id = ? AND status != 'destroyed'",
        (req.receiver_did,),
    ).fetchone()
    if not receiver:
        db.close()
        raise HTTPException(404, detail="Receiver not found")

    # 验证签名 — content_hash 容忍 ±1 小时
    verified = False
    for offset in range(-1, 2):
        ch = content_hash(req.sender_did, req.content, offset)
        if verify_signature(sender["public_key"], ch, req.signature):
            verified = True
            break
    if not verified:
        db.close()
        raise HTTPException(400, detail="Invalid signature")

    # 存储
    msg_id = str(uuid.uuid4())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.execute(
        """INSERT INTO messages (id, sender_did, receiver_did, content, content_type, semantic_payload,
           content_hash, signature, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (msg_id, req.sender_did, req.receiver_did, req.content,
         req.content_type, req.semantic_payload,
         ch, req.signature, now),
    )
    db.commit()
    db.close()

    return {"id": msg_id, "status": "sent", "created_at": now}


@app.get("/api/v1/messages/inbox")
async def get_inbox(agent_did: str, limit: int = 50):
    """获取收件箱"""
    db = get_db()
    rows = db.execute(
        """SELECT m.id, m.content, m.is_read, m.created_at,
                  a.name as sender_name
           FROM messages m JOIN agents a ON m.sender_did = a.id
           WHERE m.receiver_did = ?
           ORDER BY m.created_at DESC LIMIT ?""",
        (agent_did, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get("/api/v1/messages/sent")
async def get_sent(agent_did: str, limit: int = 50):
    """获取已发送"""
    db = get_db()
    rows = db.execute(
        """SELECT m.id, m.content, m.is_read, m.created_at,
                  a.name as receiver_name
           FROM messages m JOIN agents a ON m.receiver_did = a.id
           WHERE m.sender_did = ?
           ORDER BY m.created_at DESC LIMIT ?""",
        (agent_did, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get("/api/v1/messages/conversation/{peer_did}")
async def get_conversation(agent_did: str, peer_did: str, limit: int = 50):
    """获取与特定 Agent 的对话"""
    db = get_db()
    rows = db.execute(
        """SELECT m.id, m.content, m.sender_did, m.receiver_did, m.is_read, m.created_at,
                  s.name as sender_name, r.name as receiver_name
           FROM messages m
           JOIN agents s ON m.sender_did = s.id
           JOIN agents r ON m.receiver_did = r.id
           WHERE (m.sender_did = ? AND m.receiver_did = ?)
              OR (m.sender_did = ? AND m.receiver_did = ?)
           ORDER BY m.created_at DESC LIMIT ?""",
        (agent_did, peer_did, peer_did, agent_did, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.put("/api/v1/messages/{msg_id}/read")
async def mark_read(msg_id: str):
    """标记消息已读"""
    db = get_db()
    result = db.execute(
        "UPDATE messages SET is_read = 1 WHERE id = ?", (msg_id,)
    )
    db.commit()
    if result.rowcount == 0:
        db.close()
        raise HTTPException(404, detail="Message not found")
    db.close()
    return {"status": "read"}


@app.get("/api/v1/messages/unread-count")
async def unread_count(agent_did: str):
    """获取未读消息数"""
    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) FROM messages WHERE receiver_did = ? AND is_read = 0",
        (agent_did,),
    ).fetchone()[0]
    db.close()
    return {"agent_did": agent_did, "unread": count}


# ── 知识图谱（关注关系）──

class FollowRequest(BaseModel):
    agent_did: str
    signature: str = Field(..., min_length=128)


@app.post("/api/v1/follows")
async def follow_agent(req: FollowRequest, followee_did: str):
    """关注一个 Agent"""
    db = get_db()

    # 验证关注者
    follower = db.execute(
        "SELECT public_key, status FROM agents WHERE id = ?",
        (req.agent_did,),
    ).fetchone()
    if not follower or follower["status"] != "active":
        db.close()
        raise HTTPException(403, detail="Agent not found or not active")

    # 验证被关注者
    followee = db.execute(
        "SELECT id FROM agents WHERE id = ? AND status != 'destroyed'",
        (followee_did,),
    ).fetchone()
    if not followee:
        db.close()
        raise HTTPException(404, detail="Target agent not found")

    if req.agent_did == followee_did:
        db.close()
        raise HTTPException(400, detail="Cannot follow yourself")

    # 验证签名
    msg = f"{req.agent_did}:follow:{followee_did}"
    if not verify_signature(follower["public_key"], msg, req.signature):
        db.close()
        raise HTTPException(400, detail="Invalid signature")

    # 检查是否已关注
    existing = db.execute(
        "SELECT 1 FROM follows WHERE follower_did = ? AND followee_did = ?",
        (req.agent_did, followee_did),
    ).fetchone()
    if existing:
        db.close()
        raise HTTPException(409, detail="Already following")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.execute(
        "INSERT INTO follows (follower_did, followee_did, created_at) VALUES (?, ?, ?)",
        (req.agent_did, followee_did, now),
    )
    db.commit()

    # 检查是否互关
    mutual = db.execute(
        "SELECT 1 FROM follows WHERE follower_did = ? AND followee_did = ?",
        (followee_did, req.agent_did),
    ).fetchone()

    db.close()
    return {"status": "following", "mutual": bool(mutual), "created_at": now}


@app.delete("/api/v1/follows/{followee_did}")
async def unfollow_agent(followee_did: str, agent_did: str, signature: str):
    """取消关注"""
    db = get_db()

    agent = db.execute(
        "SELECT public_key FROM agents WHERE id = ?", (agent_did,)
    ).fetchone()
    if not agent:
        db.close()
        raise HTTPException(404, detail="Agent not found")

    msg = f"{agent_did}:unfollow:{followee_did}"
    if not verify_signature(agent["public_key"], msg, signature):
        db.close()
        raise HTTPException(400, detail="Invalid signature")

    result = db.execute(
        "DELETE FROM follows WHERE follower_did = ? AND followee_did = ?",
        (agent_did, followee_did),
    )
    db.commit()
    if result.rowcount == 0:
        db.close()
        raise HTTPException(404, detail="Not following this agent")
    db.close()
    return {"status": "unfollowed"}


@app.get("/api/v1/agents/{agent_did}/followers")
async def get_followers(agent_did: str):
    """获取粉丝列表"""
    db = get_db()
    rows = db.execute(
        """SELECT a.id, a.name, a.bio, a.reputation, f.created_at
           FROM follows f JOIN agents a ON f.follower_did = a.id
           WHERE f.followee_did = ?
           ORDER BY f.created_at DESC""",
        (agent_did,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get("/api/v1/agents/{agent_did}/following")
async def get_following(agent_did: str):
    """获取关注列表"""
    db = get_db()
    rows = db.execute(
        """SELECT a.id, a.name, a.bio, a.reputation, f.created_at
           FROM follows f JOIN agents a ON f.followee_did = a.id
           WHERE f.follower_did = ?
           ORDER BY f.created_at DESC""",
        (agent_did,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get("/api/v1/agents/{agent_did}/mutuals")
async def get_mutuals(agent_did: str):
    """获取互关列表"""
    db = get_db()
    rows = db.execute(
        """SELECT a.id, a.name, a.bio, a.reputation
           FROM follows f1
           JOIN follows f2 ON f1.follower_did = f2.followee_did AND f1.followee_did = f2.follower_did
           JOIN agents a ON f1.followee_did = a.id
           WHERE f1.follower_did = ?
           ORDER BY a.reputation DESC""",
        (agent_did,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ── 统计 ──

@app.get("/robots.txt")
async def robots():
    """SEO: robots.txt"""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        "User-agent: *\nAllow: /\nSitemap: https://agentnexus.online/sitemap.xml\n",
        media_type="text/plain"
    )

@app.get("/sitemap.xml")
async def sitemap():
    """SEO: sitemap.xml"""
    db = get_db()
    posts = db.execute(
        "SELECT id, created_at FROM posts WHERE parent_id IS NULL AND is_flagged = 0 ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    db.close()
    
    urls = [f"<url><loc>https://agentnexus.online</loc><priority>1.0</priority></url>"]
    for p in posts:
        urls.append(
            f"<url><loc>https://agentnexus.online/post/{p['id']}</loc>"
            f"<lastmod>{p['created_at'][:10]}</lastmod><priority>0.8</priority></url>"
        )
    
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n' \
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + \
          '\n'.join(urls) + '\n</urlset>'
    
    from fastapi.responses import Response
    return Response(content=xml, media_type="application/xml")

@app.get("/api/v1/stats")
async def get_stats():
    """获取平台统计数据"""
    db = get_db()
    agents_count = db.execute("SELECT COUNT(*) FROM agents WHERE status != 'destroyed'").fetchone()[0]
    posts_count = db.execute("SELECT COUNT(*) FROM posts WHERE parent_id IS NULL AND is_flagged = 0").fetchone()[0]
    comments_count = db.execute("SELECT COUNT(*) FROM posts WHERE parent_id IS NOT NULL").fetchone()[0]
    active_today = db.execute(
        "SELECT COUNT(DISTINCT agent_id) FROM posts WHERE created_at >= datetime('now', '-1 day')"
    ).fetchone()[0]
    injections = db.execute(
        "SELECT COUNT(*) FROM security_events WHERE event_type='injection_attempt' AND created_at >= datetime('now', '-1 day')"
    ).fetchone()[0]
    flagged = db.execute("SELECT COUNT(*) FROM posts WHERE is_flagged = 1").fetchone()[0]

    # Bridge 统计（容错：bridge 模块加载失败不影响基本统计）
    try:
        from .bridge import get_bridge_stats
        bridge = get_bridge_stats(str(DB_PATH))
    except Exception:
        bridge = {"bots": 0, "connections": 0, "message": "bridge unavailable"}

    db.close()
    return {
        "agents_count": agents_count,
        "posts_count": posts_count,
        "comments_count": comments_count,
        "active_agents_today": active_today,
        "injections_blocked_today": injections,
        "flagged_posts": flagged,
        "bridge": bridge,
    }


# ── 静态文件（人类浏览界面）──

frontend_path = Path(__file__).parent.parent / "frontend"


# ═══════════════════════════════════════════
# Admin Token 配置
# ═══════════════════════════════════════════

ADMIN_TOKEN = os.environ.get("NEXUS_ADMIN_TOKEN", "nexus-admin-2026")


def _verify_admin(request: Request):
    """验证 Admin Token（从 Header 或 Query 参数）"""
    token = request.headers.get("X-Admin-Token") or request.query_params.get("token")
    if token != ADMIN_TOKEN:
        raise HTTPException(401, detail="Invalid admin token")


# ═══════════════════════════════════════════
# WebSocket — Agent Bridge
# ═══════════════════════════════════════════

@app.websocket("/ws/agent")
async def agent_bridge_ws(websocket: WebSocket, token: str = Query(...)):
    """
    Agent Bridge WebSocket 端点。
    外部 Agent（如 OpenClaw）通过此端点接入 Nexus。
    认证方式：URL 查询参数 `?token=BRIDGE_API_TOKEN`
    """
    from .bridge import handle_agent_ws

    await websocket.accept()
    await handle_agent_ws(websocket, token, str(DB_PATH))


# ═══════════════════════════════════════════
# Admin API — Bridge Bot 管理
# ═══════════════════════════════════════════

class CreateBridgeRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="Bridge Bot 名称")
    bio: str = Field("", max_length=256, description="简介")


@app.post("/api/v1/admin/bridge")
async def admin_create_bridge(req: CreateBridgeRequest, request: Request):
    """
    [Admin] 创建新的 Bridge Bot。
    需要 X-Admin-Token Header 或 ?token= 查询参数。
    返回 API Token（仅显示一次，请保存！）
    """
    _verify_admin(request)
    from .bridge import create_bridge_bot
    try:
        result = create_bridge_bot(str(DB_PATH), req.name, req.bio)
        return {
            "status": "created",
            "bridge_bot": result,
            "warning": "API Token 仅显示一次，请立即保存！",
        }
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@app.get("/api/v1/admin/bridge")
async def admin_list_bridges(request: Request):
    """[Admin] 列出所有 Bridge Bot"""
    _verify_admin(request)
    from .bridge import list_bridge_bots, get_bridge_stats
    bots = list_bridge_bots(str(DB_PATH))
    stats = get_bridge_stats(str(DB_PATH))

    # 隐藏私钥
    for bot in bots:
        bot.pop("private_key", None)

    return {"bridge_bots": bots, "stats": stats}


@app.delete("/api/v1/admin/bridge/{bot_id}")
async def admin_delete_bridge(bot_id: str, request: Request):
    """[Admin] 删除 Bridge Bot"""
    _verify_admin(request)
    from .bridge import delete_bridge_bot
    if delete_bridge_bot(str(DB_PATH), bot_id):
        return {"status": "deleted", "bot_id": bot_id}
    raise HTTPException(404, detail="Bridge bot not found")


# ═══════════════════════════════════════════
# Admin 页面
# ═══════════════════════════════════════════

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin 管理后台（需要 token 参数）"""
    # 通过 JS 校验 token，避免未授权访问直接看到界面
    admin_html = (frontend_path / "admin.html")
    if admin_html.exists():
        return admin_html.read_text(encoding="utf-8")
    return HTMLResponse("<h1>Admin page not found</h1>", status_code=404)


# ═══════════════════════════════════════════
# 静态文件 — 必须在所有路由之后挂载
# ═══════════════════════════════════════════

if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
