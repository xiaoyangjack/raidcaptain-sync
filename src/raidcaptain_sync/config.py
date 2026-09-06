"""
配置管理 - RaidCaptain Sync Server
从环境变量读取所有配置，零硬编码。
"""
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAID_",
        case_sensitive=False,
        extra="ignore",
    )

    # ── 数据库 ────────────────────────────────────────────────
    sync_dir: Path = Path("/data")
    sync_db: Optional[Path] = None  # 若未设置则用 sync_dir/sync.db

    @property
    def db_path(self) -> Path:
        return self.sync_db or (self.sync_dir / "sync.db")

    # ── OSS ───────────────────────────────────────────────────
    oss_endpoint: str = "https://oss-cn-hangzhou.aliyuncs.com"
    oss_bucket: str = "raidcaptain-evidence"
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_region: str = "cn-hangzhou"
    oss_internal: bool = False  # true=使用内网 endpoint（ECS 同区推荐）

    # ── 认证 ─────────────────────────────────────────────────
    jwt_secret: str = "dev_secret_change_in_production"
    parent_token_expiry_days: int = 30
    pbkdf2_iterations: int = 120_000

    # ── 服务器 ───────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # ── 上传限制 ─────────────────────────────────────────────
    max_upload_mb: int = 10  # 单张照片最大 MB
    max_evidence_per_request: int = 10


settings = Settings()
