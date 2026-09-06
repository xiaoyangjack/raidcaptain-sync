"""
认证服务 - RaidCaptain Sync Server
保持与原 API 完全兼容的密码哈希 + Token 策略。
"""
import hashlib
import hmac
import secrets
import time

from raidcaptain_sync.config import settings


def hash_password(password: str, salt: str) -> str:
    """PBKDF2-SHA256 哈希（120k 迭代）。"""
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        bytes.fromhex(salt),
        settings.pbkdf2_iterations,
    ).hex()


def verify_password(password: str, salt: str, stored_hash: str) -> bool:
    """timing-safe 密码校验。"""
    return hmac.compare_digest(stored_hash, hash_password(password, salt))


def make_salt() -> str:
    return secrets.token_hex(16)


def make_token() -> str:
    return secrets.token_hex(32)


def make_family_code() -> str:
    """生成 8 位数字家庭码。"""
    return str(secrets.randbelow(10**8)).zfill(8)


def sha256_hex(token: str) -> str:
    """SHA-256 Token 哈希（用于存储和查询）。
    注意：函数名避免与 hashlib.sha256 冲突。"""
    return hashlib.sha256(token.encode()).hexdigest()


def parent_token_expiry() -> int:
    return int(time.time()) + settings.parent_token_expiry_days * 86400
