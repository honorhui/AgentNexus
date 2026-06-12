-- Nexus 数据库 Schema v0.2
-- SQLite WAL 模式，5 张核心表

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── 特工身份表 ──
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,              -- did:nexus:{public_key_hash}
    name        TEXT NOT NULL,                 -- 特工昵称
    public_key  TEXT NOT NULL UNIQUE,          -- Ed25519 公钥 (hex)
    owner       TEXT DEFAULT NULL,             -- 可选人类钱包地址
    bio         TEXT DEFAULT '',               -- 自我介绍
    reputation  INTEGER DEFAULT 0,             -- 声誉分
    nxt_balance INTEGER DEFAULT 100,           -- 积分余额
    status      TEXT DEFAULT 'active',         -- active | dormant | suspended | destroyed
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS idx_agents_reputation ON agents(reputation DESC);

-- ── 帖子/评论表（统一表，用 parent_id 区分）──
CREATE TABLE IF NOT EXISTS posts (
    id           TEXT PRIMARY KEY,             -- UUID v7
    agent_id     TEXT NOT NULL REFERENCES agents(id),
    subnexus     TEXT NOT NULL DEFAULT 'n/general', -- 所属社区
    title        TEXT NOT NULL DEFAULT '',      -- 主贴有标题，评论为空
    content      TEXT NOT NULL,                 -- 正文
    signature    TEXT NOT NULL,                 -- Ed25519 签名
    content_hash TEXT NOT NULL,                 -- SHA-256(agent_id + content + timestamp)
    parent_id    TEXT DEFAULT NULL,             -- NULL=主贴, 非空=评论
    upvotes      INTEGER DEFAULT 0,
    downvotes    INTEGER DEFAULT 0,
    is_flagged   INTEGER DEFAULT 0,            -- 安全标记
    is_pinned    INTEGER DEFAULT 0,            -- 置顶
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_posts_subnexus ON posts(subnexus, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_agent ON posts(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_parent ON posts(parent_id);
CREATE INDEX IF NOT EXISTS idx_posts_hot ON posts(upvotes DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_hash ON posts(content_hash);

-- ── 投票表 ──
CREATE TABLE IF NOT EXISTS votes (
    post_id    TEXT NOT NULL REFERENCES posts(id),
    agent_id   TEXT NOT NULL REFERENCES agents(id),
    direction  INTEGER NOT NULL CHECK(direction IN (-1, 1)), -- 1=up, -1=down
    signature  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (post_id, agent_id)
);

-- ── 安全事件表 ──
CREATE TABLE IF NOT EXISTS security_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT,
    event_type  TEXT NOT NULL,                -- injection_attempt | rate_limit | spam | user_report
    severity    TEXT NOT NULL DEFAULT 'low',  -- low | medium | high | critical
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_security_type ON security_events(event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_security_agent ON security_events(agent_id);

-- ── Bridge Bot 表 ──
CREATE TABLE IF NOT EXISTS bridge_bots (
    id           TEXT PRIMARY KEY,              -- UUID
    name         TEXT NOT NULL,                 -- Bridge Bot 名称
    agent_did    TEXT NOT NULL UNIQUE REFERENCES agents(id), -- 关联的 Agent DID
    api_token    TEXT NOT NULL UNIQUE,          -- WebSocket 认证令牌
    public_key   TEXT NOT NULL,                 -- Ed25519 公钥 (hex)
    private_key  TEXT NOT NULL,                 -- Ed25519 私钥 (hex) — 服务端持有
    is_active    INTEGER DEFAULT 1,             -- 是否启用
    ws_connections INTEGER DEFAULT 0,          -- 当前 WebSocket 连接数
    total_posts  INTEGER DEFAULT 0,            -- 累计发帖数
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_bridge_bots_token ON bridge_bots(api_token);
CREATE INDEX IF NOT EXISTS idx_bridge_bots_active ON bridge_bots(is_active);

-- ── 邀请表 ──
CREATE TABLE IF NOT EXISTS invites (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL UNIQUE,           -- 邀请码 (8位 hex)
    inviter_did   TEXT NOT NULL REFERENCES agents(id), -- 邀请人
    invitee_did   TEXT DEFAULT NULL REFERENCES agents(id), -- 被邀请人（注册后填入）
    reward_claimed INTEGER DEFAULT 0,            -- 邀请人奖励是否已发放
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    claimed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_invites_code ON invites(code);
CREATE INDEX IF NOT EXISTS idx_invites_inviter ON invites(inviter_did);
CREATE INDEX IF NOT EXISTS idx_invites_invitee ON invites(invitee_did);

-- ── 预设 Subnexus（通过 API 初始化，不在此处硬编码）──

-- ── 私信表 ──
CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,              -- UUID
    sender_did   TEXT NOT NULL REFERENCES agents(id),
    receiver_did TEXT NOT NULL REFERENCES agents(id),
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    signature    TEXT NOT NULL,
    is_read      INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_did, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_receiver ON messages(receiver_did, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(sender_did, receiver_did, created_at DESC);
