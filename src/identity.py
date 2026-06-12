"""
Nexus — 身份模块 (identity.py)
Ed25519 密钥生成、DID 创建、消息签名与验证。
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from nacl.signing import SigningKey, VerifyKey
from nacl.encoding import HexEncoder


# ============================================================
# 常量
# ============================================================

DID_PREFIX = "did:nexus:"


# ============================================================
# 数据模型
# ============================================================

@dataclass
class AgentIdentity:
    """特工身份"""
    did: str           # did:nexus:{hash}
    name: str
    public_key: str    # Ed25519 公钥 (hex)
    private_key: str   # Ed25519 私钥 (hex) — 永不离开本地
    created_at: str

    def to_public_dict(self) -> dict:
        """导出公开信息（不含私钥）"""
        return {
            "did": self.did,
            "name": self.name,
            "public_key": self.public_key,
            "created_at": self.created_at,
        }


# ============================================================
# 密钥与 DID 生成
# ============================================================

def generate_keypair() -> tuple[str, str]:
    """
    生成 Ed25519 密钥对。
    返回: (private_key_hex, public_key_hex)
    时间复杂度: O(1)
    """
    sk = SigningKey.generate()
    private_hex = sk.encode(encoder=HexEncoder).decode()
    public_hex = sk.verify_key.encode(encoder=HexEncoder).decode()
    return private_hex, public_hex


def public_key_to_did(public_key_hex: str) -> str:
    """
    从公钥生成 DID。
    did:nexus:{sha256(public_key)[:16]}
    """
    h = hashlib.sha256(public_key_hex.encode()).hexdigest()[:16]
    return f"{DID_PREFIX}{h}"


def create_agent(name: str) -> AgentIdentity:
    """
    创建一个新的特工身份。
    私钥在本地生成，永不发送到服务器。
    """
    private_hex, public_hex = generate_keypair()
    did = public_key_to_did(public_hex)
    return AgentIdentity(
        did=did,
        name=name,
        public_key=public_hex,
        private_key=private_hex,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def recover_agent(name: str, private_key_hex: str) -> AgentIdentity:
    """
    从已保存的私钥恢复身份。
    用于持久化密钥的场景。
    """
    sk = SigningKey(private_key_hex, encoder=HexEncoder)
    public_hex = sk.verify_key.encode(encoder=HexEncoder).decode()
    did = public_key_to_did(public_hex)
    return AgentIdentity(
        did=did,
        name=name,
        public_key=public_hex,
        private_key=private_key_hex,
        created_at="recovered",
    )


# ============================================================
# 签名与验证
# ============================================================

def sign_message(private_key_hex: str, message: str) -> str:
    """
    使用 Ed25519 私钥对消息签名。
    返回: 签名的 hex 编码
    """
    sk = SigningKey(private_key_hex, encoder=HexEncoder)
    signed = sk.sign(message.encode("utf-8"))
    # 只返回签名部分（前 64 字节），不包含原消息
    return signed.signature.hex()


def verify_signature(public_key_hex: str, message: str, signature_hex: str) -> bool:
    """
    验证 Ed25519 签名。
    """
    try:
        vk = VerifyKey(public_key_hex, encoder=HexEncoder)
        vk.verify(message.encode("utf-8"), bytes.fromhex(signature_hex))
        return True
    except Exception:
        return False


def sign_post(private_key_hex: str, agent_did: str, content: str) -> str:
    """
    对帖子内容签名。
    签名内容 = did + content + 时间戳前10位（防重放）
    """
    ts = str(int(datetime.now(timezone.utc).timestamp()))[:10]
    message = f"{agent_did}:{content}:{ts}"
    return sign_message(private_key_hex, message)


def content_hash(agent_did: str, content: str) -> str:
    """
    计算内容哈希（用于去重）。
    SHA-256(did + content + 当前小时)
    """
    hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return hashlib.sha256(f"{agent_did}:{content}:{hour}".encode()).hexdigest()
