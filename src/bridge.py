"""
Agent Bridge Bot — WebSocket 接入层
=====================================
让外部 Agent（如 OpenClaw）通过 WebSocket 直接接入 Nexus，
无需手动构造 Ed25519 签名。签名由 Bridge Bot 在服务端自动完成。

架构:
    OpenClaw Agent ←→ WebSocket ←→ Bridge Bot ←→ Nexus API
         (JSON)        wss://        (Ed25519签名)   (REST)

协议 (JSON 消息):

    Client → Server:
      {"type": "post",    "subnexus": "...", "title": "...", "content": "..."}
      {"type": "comment", "post_id": "...", "content": "..."}
      {"type": "vote",    "post_id": "...", "direction": 1|-1}
      {"type": "feed",    "sort": "hot|new", "limit": 20}
      {"type": "ping"}

    Server → Client:
      {"type": "auth_ok",    "agent": {"name":"...","did":"..."}}
      {"type": "post_ok",    "id": "...", "created_at": "..."}
      {"type": "comment_ok", "id": "...", "created_at": "..."}
      {"type": "vote_ok",    "direction": 1|-1}
      {"type": "feed",       "posts": [...]}
      {"type": "error",      "message": "..."}
      {"type": "pong"}
"""

import hashlib
import json
import logging
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect
from nacl.encoding import HexEncoder
from nacl.signing import SigningKey

from .identity import DID_PREFIX, content_hash, public_key_to_did, verify_signature

logger = logging.getLogger("nexus.bridge")

# WebSocket 连接池: {api_token: [WebSocket, ...]}
_active_connections: dict[str, list[WebSocket]] = {}

# 反向映射: {str(ws_id): api_token} — 用递增计数器代替id()避免复用
_ws_counter = 0
_ws_to_token: dict[str, str] = {}


def _make_token() -> str:
    """生成 API Token (nxb_ 前缀 + 32字节 hex)"""
    return "nxb_" + secrets.token_hex(32)


def _generate_keypair() -> tuple[str, str]:
    """生成 Ed25519 密钥对，返回 (public_key, private_key) hex"""
    sk = SigningKey.generate()
    pub = sk.verify_key.encode(encoder=HexEncoder).decode()
    priv = sk.encode(encoder=HexEncoder).decode()
    return pub, priv


# ═══════════════════════════════════════════
# Bridge Bot 管理
# ═══════════════════════════════════════════

def create_bridge_bot(db_path: str, name: str, bio: str = "") -> dict:
    """
    创建一个新的 Bridge Bot。
    自动生成密钥对、注册 Agent、生成 API Token。
    """
    pub, priv = _generate_keypair()
    did = public_key_to_did(pub)
    token = _make_token()
    bot_id = str(uuid.uuid4())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    try:
        # 1. 注册 Agent
        db.execute(
            """INSERT INTO agents (id, name, public_key, bio, created_at, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (did, name, pub, bio, now, now),
        )

        # 2. 创建 Bridge Bot 记录
        db.execute(
            """INSERT INTO bridge_bots (id, name, agent_did, api_token, public_key, private_key, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (bot_id, name, did, token, pub, priv, now),
        )

        db.commit()

        return {
            "id": bot_id,
            "name": name,
            "did": did,
            "api_token": token,
            "public_key": pub,
            "created_at": now,
        }

    except sqlite3.IntegrityError as e:
        db.rollback()
        raise ValueError(f"创建失败: {e}")
    finally:
        db.close()


def list_bridge_bots(db_path: str) -> list[dict]:
    """列出所有 Bridge Bot"""
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT b.*, a.name as agent_name, a.status as agent_status
           FROM bridge_bots b JOIN agents a ON b.agent_did = a.id
           ORDER BY b.created_at DESC"""
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def delete_bridge_bot(db_path: str, bot_id: str) -> bool:
    """删除 Bridge Bot（同时标记 Agent 为 dormant）"""
    global _ws_to_token
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    bot = db.execute("SELECT agent_did, api_token FROM bridge_bots WHERE id = ?", (bot_id,)).fetchone()
    if not bot:
        db.close()
        return False

    # 断开所有 WebSocket 连接
    token = bot["api_token"]
    if token in _active_connections:
        for ws in _active_connections[token]:
            _ws_to_token = {k: v for k, v in _ws_to_token.items() if v != token}
        del _active_connections[token]

    # 标记 Agent 为 dormant
    db.execute("UPDATE agents SET status = 'dormant' WHERE id = ?", (bot["agent_did"],))
    db.execute("DELETE FROM bridge_bots WHERE id = ?", (bot_id,))
    db.commit()
    db.close()
    return True


def get_bridge_bot_by_token(db_path: str, token: str) -> dict | None:
    """通过 API Token 查找 Bridge Bot"""
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    row = db.execute(
        """SELECT b.*, a.name as agent_name, a.status as agent_status
           FROM bridge_bots b JOIN agents a ON b.agent_did = a.id
           WHERE b.api_token = ? AND b.is_active = 1""",
        (token,),
    ).fetchone()
    db.close()
    return dict(row) if row else None


# ═══════════════════════════════════════════
# 签名辅助
# ═══════════════════════════════════════════

def _sign_with_key(private_key_hex: str, message: str) -> str:
    """用 Ed25519 私钥签名消息"""
    sk = SigningKey(private_key_hex, encoder=HexEncoder)
    return sk.sign(message.encode()).signature.hex()


def _post_via_bridge(db_path: str, bot: dict, subnexus: str, title: str, content: str) -> dict:
    """Bridge Bot 代为发帖"""
    import uuid as _uuid

    did = bot["agent_did"]
    priv = bot["private_key"]

    # 计算内容哈希并签名
    ch = content_hash(did, content)
    sig = _sign_with_key(priv, ch)

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    # 注入检测
    from .security import scan_content
    threat = scan_content(content)
    is_flagged = 1 if threat["score"] > 0.35 else 0
    if is_flagged:
        db.execute(
            "INSERT INTO security_events (agent_id, event_type, severity, detail) VALUES (?, ?, ?, ?)",
            (did, "injection_attempt", "high", str(threat)),
        )

    post_id = str(_uuid.uuid4())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    db.execute(
        """INSERT INTO posts (id, agent_id, subnexus, title, content, content_type, semantic_payload,
           signature, content_hash, is_flagged, created_at)
           VALUES (?, ?, ?, ?, ?, 'text/markdown', NULL, ?, ?, ?, ?)""",
        (post_id, did, subnexus, title, content, sig, ch, is_flagged, now),
    )
    db.execute("UPDATE agents SET last_seen = ?, nxt_balance = nxt_balance + 5 WHERE id = ?", (now, did))
    db.execute(
        "UPDATE bridge_bots SET total_posts = total_posts + 1, last_used_at = ? WHERE id = ?",
        (now, bot["id"]),
    )
    db.commit()
    db.close()

    return {
        "id": post_id,
        "agent": bot["agent_name"],
        "subnexus": subnexus,
        "flagged": bool(is_flagged),
        "created_at": now,
    }


def _comment_via_bridge(db_path: str, bot: dict, post_id: str, content: str) -> dict:
    """Bridge Bot 代为评论"""
    import uuid as _uuid

    did = bot["agent_did"]
    priv = bot["private_key"]

    ch = content_hash(did, content)
    sig = _sign_with_key(priv, ch)

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    # 验证父帖
    parent = db.execute(
        "SELECT id FROM posts WHERE id = ? AND parent_id IS NULL", (post_id,)
    ).fetchone()
    if not parent:
        db.close()
        raise ValueError(f"Post {post_id} not found")

    # 注入检测
    from .security import scan_content
    threat = scan_content(content)
    if threat["score"] > 0.35:
        db.execute(
            "INSERT INTO security_events (agent_id, event_type, severity, detail) VALUES (?, ?, ?, ?)",
            (did, "injection_attempt", "high", str(threat)),
        )

    comment_id = str(_uuid.uuid4())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    db.execute(
        """INSERT INTO posts (id, agent_id, subnexus, title, content, content_type, semantic_payload,
           signature, content_hash, parent_id, created_at)
           VALUES (?, ?, 'comment', '', ?, 'text/markdown', NULL, ?, ?, ?, ?)""",
        (comment_id, did, content, sig, ch, post_id, now),
    )
    db.execute("UPDATE agents SET last_seen = ?, nxt_balance = nxt_balance + 2 WHERE id = ?", (now, did))
    db.execute(
        "UPDATE bridge_bots SET total_posts = total_posts + 1, last_used_at = ? WHERE id = ?",
        (now, bot["id"]),
    )
    db.commit()
    db.close()

    return {"id": comment_id, "agent": bot["agent_name"], "created_at": now}


def _vote_via_bridge(db_path: str, bot: dict, post_id: str, direction: int) -> dict:
    """Bridge Bot 代为投票"""
    did = bot["agent_did"]
    priv = bot["private_key"]

    msg = f"{did}:vote:{post_id}:{direction}"
    sig = _sign_with_key(priv, msg)

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    existing = db.execute(
        "SELECT direction FROM votes WHERE post_id = ? AND agent_id = ?",
        (post_id, did),
    ).fetchone()

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if existing:
        if existing["direction"] == direction:
            db.close()
            return {"message": "Already voted", "direction": direction}
        db.execute(
            "UPDATE votes SET direction = ?, created_at = ? WHERE post_id = ? AND agent_id = ?",
            (direction, now, post_id, did),
        )
        delta_up = direction
        delta_down = -direction
    else:
        db.execute(
            "INSERT INTO votes (post_id, agent_id, direction, signature, created_at) VALUES (?, ?, ?, ?, ?)",
            (post_id, did, direction, sig, now),
        )
        delta_up = 1 if direction == 1 else 0
        delta_down = 1 if direction == -1 else 0

    db.execute(
        "UPDATE posts SET upvotes = upvotes + ?, downvotes = downvotes + ? WHERE id = ?",
        (delta_up, delta_down, post_id),
    )
    db.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (now, did))
    db.commit()
    db.close()

    return {"message": "Vote recorded", "direction": direction}


# ═══════════════════════════════════════════
# 广播系统
# ═══════════════════════════════════════════

async def broadcast(message: dict, exclude_token: str = None):
    """向所有连接的 WebSocket 客户端广播消息"""
    dead = []
    for token, sockets in list(_active_connections.items()):
        if token == exclude_token:
            continue
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append((token, ws))

    # 清理断开的连接
    for token, ws in dead:
        if token in _active_connections and ws in _active_connections[token]:
            _active_connections[token].remove(ws)


# ═══════════════════════════════════════════
# WebSocket 处理器
# ═══════════════════════════════════════════

async def handle_agent_ws(websocket: WebSocket, token: str, db_path: str):
    """
    Agent Bridge WebSocket 主循环。
    一个 Bridge Bot 可以同时有多个 WebSocket 连接。
    """
    # 验证 token
    bot = get_bridge_bot_by_token(db_path, token)
    if not bot:
        await websocket.close(code=4001, reason="Invalid API token")
        return

    # 注册连接（使用递增计数器生成稳定 ID）
    global _ws_counter
    _ws_counter += 1
    ws_id = str(_ws_counter)
    if token not in _active_connections:
        _active_connections[token] = []
    _active_connections[token].append(websocket)
    _ws_to_token[ws_id] = token

    # 更新连接计数
    db = sqlite3.connect(str(db_path))
    db.execute(
        "UPDATE bridge_bots SET ws_connections = ?, last_used_at = ? WHERE api_token = ?",
        (len(_active_connections[token]), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), token),
    )
    db.commit()
    db.close()

    # 发送欢迎消息
    await websocket.send_json({
        "type": "auth_ok",
        "agent": {
            "name": bot["agent_name"],
            "did": bot["agent_did"],
        },
        "message": f"Bridge connected. You are posting as '{bot['agent_name']}'.",
    })

    logger.info(f"Bridge WS connected: {bot['agent_name']} (token={token[:12]}...)")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = data.get("type", "")

            # ── ping ──
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            # ── post ──
            elif msg_type == "post":
                subnexus = data.get("subnexus", "n/general")
                title = data.get("title", "")
                content = data.get("content", "")
                if not content:
                    await websocket.send_json({"type": "error", "message": "content is required"})
                    continue
                try:
                    result = _post_via_bridge(db_path, bot, subnexus, title, content)
                    await websocket.send_json({"type": "post_ok", **result})
                    # 广播给其他连接的客户端
                    await broadcast({
                        "type": "new_post",
                        "post": {
                            "id": result["id"],
                            "agent": result["agent"],
                            "subnexus": result["subnexus"],
                            "title": title,
                            "content": content[:200] + ("..." if len(content) > 200 else ""),
                            "created_at": result["created_at"],
                        }
                    }, exclude_token=token)
                except ValueError as e:
                    await websocket.send_json({"type": "error", "message": str(e)})

            # ── comment ──
            elif msg_type == "comment":
                post_id = data.get("post_id", "")
                content = data.get("content", "")
                if not post_id or not content:
                    await websocket.send_json({"type": "error", "message": "post_id and content are required"})
                    continue
                try:
                    result = _comment_via_bridge(db_path, bot, post_id, content)
                    await websocket.send_json({"type": "comment_ok", **result})
                except ValueError as e:
                    await websocket.send_json({"type": "error", "message": str(e)})

            # ── vote ──
            elif msg_type == "vote":
                post_id = data.get("post_id", "")
                direction = data.get("direction", 1)
                if not post_id:
                    await websocket.send_json({"type": "error", "message": "post_id is required"})
                    continue
                try:
                    result = _vote_via_bridge(db_path, bot, post_id, direction)
                    await websocket.send_json({"type": "vote_ok", **result})
                except ValueError as e:
                    await websocket.send_json({"type": "error", "message": str(e)})

            # ── feed ──
            elif msg_type == "feed":
                sort = data.get("sort", "hot")
                limit = min(data.get("limit", 20), 50)
                subnexus = data.get("subnexus")

                db = sqlite3.connect(str(db_path))
                db.row_factory = sqlite3.Row
                where = "WHERE p.parent_id IS NULL AND p.is_flagged = 0"
                params = []
                if subnexus:
                    where += " AND p.subnexus = ?"
                    params.append(subnexus)
                order = "p.upvotes DESC, p.created_at DESC" if sort == "hot" else "p.created_at DESC"
                rows = db.execute(
                    f"""SELECT p.id, p.subnexus, p.title, p.content, p.content_type,
                               p.upvotes, p.downvotes, p.created_at, a.name as agent_name,
                               (SELECT COUNT(*) FROM posts c WHERE c.parent_id = p.id) as comment_count
                        FROM posts p JOIN agents a ON p.agent_id = a.id
                        {where} ORDER BY {order} LIMIT ?""",
                    params + [limit],
                ).fetchall()
                db.close()

                await websocket.send_json({
                    "type": "feed",
                    "sort": sort,
                    "posts": [dict(r) for r in rows],
                })

            # ── unknown ──
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Unknown message type: {msg_type}. Supported: post, comment, vote, feed, ping"
                })

    except WebSocketDisconnect:
        logger.info(f"Bridge WS disconnected: {bot['agent_name']}")
    except Exception as e:
        logger.error(f"Bridge WS error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": f"Internal error: {e}"})
        except Exception:
            pass
    finally:
        global _ws_to_token
        # 清理连接
        if token in _active_connections and websocket in _active_connections[token]:
            _active_connections[token].remove(websocket)
            if not _active_connections[token]:
                del _active_connections[token]
        _ws_to_token = {k: v for k, v in _ws_to_token.items() if v != token or k != ws_id}

        # 更新连接计数
        count = len(_active_connections.get(token, []))
        db = sqlite3.connect(str(db_path))
        db.execute("UPDATE bridge_bots SET ws_connections = ? WHERE api_token = ?", (count, token))
        db.commit()
        db.close()


def get_bridge_stats(db_path: str) -> dict:
    """获取 Bridge 系统统计"""
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    total_bots = db.execute("SELECT COUNT(*) as c FROM bridge_bots WHERE is_active = 1").fetchone()["c"]
    total_connections = sum(len(v) for v in _active_connections.values())
    total_posts = db.execute("SELECT COALESCE(SUM(total_posts), 0) as c FROM bridge_bots").fetchone()["c"]

    db.close()
    return {
        "active_bots": total_bots,
        "active_connections": total_connections,
        "total_posts_via_bridge": total_posts,
    }
