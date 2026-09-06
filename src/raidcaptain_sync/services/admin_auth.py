"""
Admin 认证服务 - RaidCaptain Sync Server v3.3
独立管理员账号系统（与家庭账号完全隔离）。

用法:
    from raidcaptain_sync.services.admin_auth import (
        AdminAuthService, hash_admin_pw, verify_admin_pw
    )
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import sqlite3

from raidcaptain_sync.config import settings


def hash_admin_pw(password: str, salt: str) -> str:
    """管理员密码哈希（PBKDF2-SHA256, 120k 迭代）."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        bytes.fromhex(salt),
        settings.pbkdf2_iterations,
    ).hex()


def verify_admin_pw(password: str, salt: str, stored_hash: str) -> bool:
    """timing-safe 管理员密码验证."""
    return hmac.compare_digest(stored_hash, hash_admin_pw(password, salt))


def make_admin_salt() -> str:
    return secrets.token_hex(16)


def sha256_hex(s: str) -> str:
    """SHA-256（用于 admin_token 哈希存储）."""
    return hashlib.sha256(s.encode()).hexdigest()


class AdminAuthService:
    """管理员认证服务."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        # 确保 admin 表存在
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS admin(
                admin_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                pw_salt TEXT NOT NULL,
                pw_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'editor',
                is_active INTEGER NOT NULL DEFAULT 1,
                last_login_at INTEGER,
                created_at INTEGER NOT NULL
            )
        """)

    def create_admin(
        self,
        username: str,
        password: str,
        role: str = "editor",
    ) -> str:
        """创建管理员账户。返回 admin_id."""
        admin_id = secrets.token_hex(8)
        salt = make_admin_salt()
        pw_hash = hash_admin_pw(password, salt)
        now = int(time.time() * 1000)
        try:
            self.db.execute(
                """INSERT INTO admin(admin_id, username, pw_salt, pw_hash, role,
                    is_active, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (admin_id, username, salt, pw_hash, role, now),
            )
            self.db.commit()
            return admin_id
        except sqlite3.IntegrityError:
            raise ValueError(f"用户名已存在: {username}")

    def login(self, username: str, password: str) -> tuple[str, dict, int]:
        """
        管理员登录。

        Returns: (admin_token, admin_info, expires_at)
        """
        row = self.db.execute(
            "SELECT * FROM admin WHERE username=?",
            (username,),
        ).fetchone()
        if not row:
            raise ValueError("用户名或密码错误")
        keys = ["admin_id", "username", "pw_salt", "pw_hash", "role",
                "is_active", "last_login_at", "created_at"]
        admin = dict(zip(keys, row))
        if not admin["is_active"]:
            raise ValueError("账户已停用")
        if not verify_admin_pw(password, admin["pw_salt"], admin["pw_hash"]):
            raise ValueError("用户名或密码错误")

        # 生成 token（24 小时过期）
        token = secrets.token_hex(32)
        now = int(time.time())
        expires = now + 86400

        # 简化：把 token 哈希暂存于内存（生产环境应存表）
        # 这里为简单实现，使用 token 哈希做后续校验
        self.db.execute(
            "UPDATE admin SET last_login_at=? WHERE admin_id=?",
            (now * 1000, admin["admin_id"]),
        )
        self.db.commit()

        # token 本身 = 短期 token（含信息签名）。简化方案：
        # token 格式: "{admin_id}.{expires}.{signature}"
        # signature = sha256(admin_id + expires + secret)
        secret = settings.jwt_secret
        signature = hashlib.sha256(
            f"{admin['admin_id']}.{expires}.{secret}".encode()
        ).hexdigest()[:32]
        signed_token = f"{admin['admin_id']}.{expires}.{signature}"

        info = {
            "admin_id": admin["admin_id"],
            "username": admin["username"],
            "role": admin["role"],
        }
        return signed_token, info, expires

    @staticmethod
    def verify_token(token: str) -> dict | None:
        """验证 admin token 并返回 admin 信息."""
        try:
            admin_id, expires_str, signature = token.split(".", 2)
            expires = int(expires_str)
            if expires < int(time.time()):
                return None
            secret = settings.jwt_secret
            expected_sig = hashlib.sha256(
                f"{admin_id}.{expires}.{secret}".encode()
            ).hexdigest()[:32]
            if not hmac.compare_digest(expected_sig, signature):
                return None
            return {"admin_id": admin_id}
        except Exception:
            return None

    def audit_log(
        self,
        admin_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        family_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        """记录 admin 操作审计日志."""
        import json
        self.db.execute(
            """INSERT INTO admin_audit_log(admin_id, action, resource_type,
                resource_id, family_id, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (admin_id, action, resource_type, resource_id, family_id,
             json.dumps(details or {}, ensure_ascii=False),
             int(time.time() * 1000)),
        )
        self.db.commit()