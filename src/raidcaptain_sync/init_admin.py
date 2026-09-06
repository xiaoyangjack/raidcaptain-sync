"""
init_admin.py — 初始化第一个管理员账户

用法:
    python -m raidcaptain_sync.init_admin --username admin --password yourpass
    python -m raidcaptain_sync.init_admin --username admin --password yourpass --role admin
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="创建 RaidCaptain Admin 账户")
    parser.add_argument("--username", required=True, help="管理员用户名")
    parser.add_argument("--password", help="管理员密码（不传则交互输入）")
    parser.add_argument("--role", default="admin", choices=["admin", "editor", "viewer"])
    parser.add_argument("--db", help="数据库路径（默认从配置读取）")
    args = parser.parse_args()

    # 设置环境变量（如果指定了 --db）
    if args.db:
        os.environ["RAID_SYNC_DB"] = args.db

    from raidcaptain_sync.config import settings
    import sqlite3
    from raidcaptain_sync.services.admin_auth import AdminAuthService

    if not args.password:
        args.password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm: ")
        if args.password != confirm:
            print("两次密码不一致", file=sys.stderr)
            sys.exit(1)

    db = sqlite3.connect(str(settings.db_path))
    auth = AdminAuthService(db)
    try:
        admin_id = auth.create_admin(args.username, args.password, args.role)
        print(f"✅ Admin 创建成功：id={admin_id}, username={args.username}, role={args.role}")
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()