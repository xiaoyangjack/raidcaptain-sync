"""
家庭注册 + 家长登录路由 - RAID Captain Sync
"""
import time

from fastapi import APIRouter, Depends, HTTPException

from raidcaptain_sync.deps import get_db
from raidcaptain_sync.services.auth import (
    hash_password,
    make_family_code,
    make_salt,
    make_token,
    parent_token_expiry,
    sha256_hex,
    verify_password,
)

router = APIRouter()


@router.post("/api/family/register")
def family_register(body: dict, db=Depends(get_db)):
    """家长首次使用：注册家庭。返回家庭码。"""
    password = str(body.get("password") or "")
    if len(password) < 6:
        raise HTTPException(400, "家长密码至少 6 位")

    # 重试生成家庭码直到唯一（极小概率冲突）
    for _ in range(10):
        family_code = make_family_code()
        existing = db.execute("SELECT 1 FROM family WHERE id=?", (family_code,)).fetchone()
        if not existing:
            break
    else:
        raise HTTPException(500, "无法生成唯一家庭码")

    salt = make_salt()
    db.execute(
        "INSERT INTO family(id, pw_salt, pw_hash, created_at) VALUES(?,?,?,?)",
        (family_code, salt, hash_password(password, salt), int(time.time())),
    )
    return {"family_code": family_code}


@router.post("/api/parent/login")
def parent_login(body: dict, db=Depends(get_db)):
    """家长登录，返回 parent_token（30 天有效）。"""
    code = str(body.get("family_code") or "")
    password = str(body.get("password") or "")
    row = db.execute("SELECT * FROM family WHERE id=?", (code,)).fetchone()
    if not row or not verify_password(password, row["pw_salt"], row["pw_hash"]):
        raise HTTPException(401, "家庭码或密码错误")
    token = make_token()
    db.execute(
        "UPDATE family SET parent_token=?, parent_token_exp=? WHERE id=?",
        (sha256_hex(token), parent_token_expiry(), code),
    )
    return {"parent_token": token, "family_code": code}


@router.post("/api/device/pair")
def device_pair(body: dict, db=Depends(get_db)):
    """设备配对：家庭码 + 家长密码 → device_token。"""
    code = str(body.get("family_code") or "").strip()
    password = str(body.get("parent_password") or "")
    name = str(body.get("device_name") or "child-device").strip()[:32] or "child-device"
    if len(password) < 6:
        raise HTTPException(400, "家长密码至少 6 位")
    row = db.execute("SELECT * FROM family WHERE id=?", (code,)).fetchone()
    if not row or not verify_password(password, row["pw_salt"], row["pw_hash"]):
        raise HTTPException(401, "家庭码或家长密码错误")
    token = make_token()
    db.execute(
        "INSERT INTO device(token_hash, family_id, name, last_seen, created_at) VALUES(?,?,?,?,?)",
        (sha256_hex(token), code, name, int(time.time()), int(time.time())),
    )
    return {"device_token": token, "device_name": name, "family_code": code}