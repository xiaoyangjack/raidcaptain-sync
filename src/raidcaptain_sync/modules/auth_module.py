"""
认证模块 - RaidCaptain Sync Server v3.1
家庭注册、家长登录、设备配对（核心认证基础设施）。

API:
  POST /api/family/register   家庭注册（首次使用）
  POST /api/parent/login      家长登录，返回 parent_token (30天有效)
  POST /api/device/pair       设备配对（家庭码+家长密码换取 device_token）
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from raidcaptain_sync.deps import auth_device, auth_parent, get_db
from raidcaptain_sync.services.auth import (
    hash_password, make_family_code, make_salt, make_token,
    parent_token_expiry, sha256_hex, verify_password,
)
from raidcaptain_sync.services.module_registry import BaseModule
from raidcaptain_sync.services.revision import StandardModules


class AuthModule(BaseModule):
    """认证基础设施模块 - 必须最先注册。"""
    id = StandardModules.AUTH
    display_name = "认证与家庭管理"
    version = "1.0.0"
    description = "家庭注册/家长登录/设备配对"

    def __init__(self):
        self._routers: list = []
        self._build_routers()

    def _build_routers(self) -> None:
        r = APIRouter(prefix="", tags=["auth"])

        @r.post("/api/family/register")
        def family_register(body: dict, db=Depends(get_db)):
            password = str(body.get("password") or "")
            if len(password) < 6:
                raise HTTPException(400, "家长密码至少 6 位")
            for _ in range(10):
                family_code = make_family_code()
                existing = db.execute(
                    "SELECT 1 FROM family WHERE id=?", (family_code,)
                ).fetchone()
                if not existing:
                    break
            else:
                raise HTTPException(500, "无法生成唯一家庭码")
            salt = make_salt()
            db.execute(
                "INSERT INTO family(id, pw_salt, pw_hash, created_at) "
                "VALUES(?,?,?,?)",
                (family_code, salt, hash_password(password, salt),
                 int(time.time())),
            )
            return {"family_code": family_code}

        @r.post("/api/parent/login")
        def parent_login(body: dict, db=Depends(get_db)):
            code = str(body.get("family_code") or "")
            password = str(body.get("password") or "")
            row = db.execute(
                "SELECT * FROM family WHERE id=?", (code,)
            ).fetchone()
            if not row or not verify_password(
                password, row["pw_salt"], row["pw_hash"]
            ):
                raise HTTPException(401, "家庭码或密码错误")
            token = make_token()
            db.execute(
                "UPDATE family SET parent_token=?, parent_token_exp=? WHERE id=?",
                (sha256_hex(token), parent_token_expiry(), code),
            )
            return {"parent_token": token, "family_code": code}

        @r.post("/api/device/pair")
        def device_pair(body: dict, db=Depends(get_db)):
            code = str(body.get("family_code") or "").strip()
            password = str(body.get("parent_password") or "")
            name = (
                str(body.get("device_name") or "child-device").strip()[:32]
                or "child-device"
            )
            if len(password) < 6:
                raise HTTPException(400, "家长密码至少 6 位")
            row = db.execute(
                "SELECT * FROM family WHERE id=?", (code,)
            ).fetchone()
            if not row or not verify_password(
                password, row["pw_salt"], row["pw_hash"]
            ):
                raise HTTPException(401, "家庭码或家长密码错误")
            token = make_token()
            db.execute(
                "INSERT INTO device(token_hash, family_id, name, "
                "last_seen, created_at) VALUES(?,?,?,?,?)",
                (sha256_hex(token), code, name,
                 int(time.time()), int(time.time())),
            )
            return {"device_token": token, "device_name": name,
                    "family_code": code}

        self._routers = [r]


def create_auth_module() -> AuthModule:
    return AuthModule()