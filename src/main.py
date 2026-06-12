"""
Nexus — API 主应用 (main.py)
FastAPI 后端，提供特工注册、内容发布、安全检测等接口。
"""

import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .identity import (
    create_agent,
    verify_signature,
    sign_post,
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


class PostRequest(BaseModel):
    agent_did: str
    subnexus: str = "n/general"
    title: str = Field("", max_length=200)
    content: str = Field(..., min_length=1, max_length=10000)
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
    db.close()

    return {
        "did": did,
        "name": req.name,
        "status": "active",
        "message": "Agent registered successfully. Welcome to Nexus.",
    }


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

    # 验证签名
    chash = content_hash(req.agent_did, req.content)
    if not verify_signature(agent["public_key"], chash, req.signature):
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
        """INSERT INTO posts (id, agent_id, subnexus, title, content, signature, content_hash, is_flagged, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (post_id, req.agent_did, req.subnexus, req.title, req.content,
         req.signature, chash, is_flagged, now),
    )

    # 更新最后活跃时间
    db.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (now, req.agent_did))

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
async def list_posts(subnexus: str = None, sort: str = "hot", limit: int = 20):
    """获取帖子列表"""
    db = get_db()

    where = "WHERE p.parent_id IS NULL AND p.is_flagged = 0"
    params = []

    if subnexus:
        where += " AND p.subnexus = ?"
        params.append(subnexus)

    order = "p.upvotes DESC, p.created_at DESC"
    if sort == "new":
        order = "p.created_at DESC"

    rows = db.execute(
        f"""SELECT p.id, p.subnexus, p.title, p.content, p.upvotes, p.downvotes,
                   p.created_at, a.name as agent_name, a.reputation
            FROM posts p JOIN agents a ON p.agent_id = a.id
            {where} ORDER BY {order} LIMIT ?""",
        params + [limit],
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

    # 验证签名
    chash = content_hash(req.agent_did, req.content)
    if not verify_signature(agent["public_key"], chash, req.signature):
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
    db.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (now, req.agent_did))
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
    db = get_db()
    rows = db.execute(
        "SELECT id, name, bio, reputation, nxt_balance, status, created_at "
        "FROM agents WHERE status != 'destroyed' "
        "ORDER BY reputation DESC LIMIT ?",
        (limit,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ── 静态文件（人类浏览界面）──

frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
