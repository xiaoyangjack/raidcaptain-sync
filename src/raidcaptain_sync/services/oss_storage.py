"""
OSS 存储服务 - RaidCaptain Sync Server
将照片数据从 base64+SQLite 迁移到阿里云 OSS，DB 仅存 URL。
"""
import base64
import io
import logging
import time
import uuid
from typing import Optional

import oss2

from raidcaptain_sync.config import settings

logger = logging.getLogger(__name__)


class OSSStorage:
    """OSS 存储封装。fallback 到空存储（开发环境无 OSS 时不崩）。"""

    def __init__(self):
        self._bucket: Optional[oss2.Bucket] = None
        self._enabled = bool(settings.oss_access_key_id and settings.oss_access_key_secret)
        if self._enabled:
            auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
            endpoint = settings.oss_endpoint
            self._bucket = oss2.Bucket(auth, endpoint, settings.oss_bucket)
            logger.info(
                "OSS storage enabled: bucket=%s endpoint=%s",
                settings.oss_bucket,
                endpoint,
            )
        else:
            logger.warning(
                "OSS credentials not configured — evidence files will be stored as base64 in DB "
                "(NOT recommended for production)"
            )

    # ── 上传 ───────────────────────────────────────────────────

    def upload_evidence(
        self,
        family_id: str,
        device_name: str,
        data_b64: str,
        mime: str = "image/jpeg",
    ) -> tuple[str, int]:
        """将 base64 照片上传到 OSS，返回 (oss_key, size_bytes)。"""
        # 解析 base64
        try:
            raw = base64.b64decode(data_b64)
        except Exception as e:
            raise ValueError(f"Invalid base64 data: {e}")

        size_bytes = len(raw)
        ext = self._mime_to_ext(mime)
        oss_key = f"evidence/{family_id}/{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.{ext}"

        if self._enabled:
            self._bucket.put_object(oss_key, raw, headers={"Content-Type": mime})
            logger.debug("Uploaded evidence: %s (%d bytes)", oss_key, size_bytes)
        else:
            logger.debug("OSS disabled — would store: %s (%d bytes)", oss_key, size_bytes)

        return oss_key, size_bytes

    # ── 获取 ───────────────────────────────────────────────────

    def get_url(self, oss_key: str, expires_seconds: int = 3600) -> str:
        """获取 OSS 对象的访问 URL（带签名，防盗链）。"""
        if not self._enabled:
            return ""
        return self._bucket.sign_url("GET", oss_key, expires_seconds)

    def get_object(self, oss_key: str) -> bytes:
        """下载 OSS 对象内容。"""
        if not self._enabled:
            raise FileNotFoundError(f"OSS disabled, cannot fetch: {oss_key}")
        result = self._bucket.get_object(oss_key)
        return result.read()

    def delete(self, oss_key: str) -> None:
        """删除 OSS 对象。"""
        if not self._enabled or not oss_key:
            return
        try:
            self._bucket.delete_object(oss_key)
            logger.debug("Deleted OSS key: %s", oss_key)
        except Exception as e:
            logger.warning("Failed to delete OSS key %s: %s", oss_key, e)

    # ── 批量删除 ─────────────────────────────────────────────

    def delete_family(self, family_id: str) -> int:
        """删除某家庭所有 evidence OSS 对象，返回删除数量。"""
        if not self._enabled:
            return 0
        prefix = f"evidence/{family_id}/"
        deleted = 0
        for obj in oss2.ObjectIterator(self._bucket, prefix=prefix):
            self._bucket.delete_object(obj.key)
            deleted += 1
        return deleted

    # ── 健康检查 ─────────────────────────────────────────────

    def health_check(self) -> bool:
        """检查 OSS 连接是否正常。"""
        if not self._enabled:
            return True  # 无 OSS 配置视为健康（降级模式）
        try:
            self._bucket.get_bucket_info()
            return True
        except Exception as e:
            logger.error("OSS health check failed: %s", e)
            return False

    # ── 工具 ──────────────────────────────────────────────────

    @staticmethod
    def _mime_to_ext(mime: str) -> str:
        mapping = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
            "image/heic": "heic",
            "image/heif": "heif",
        }
        return mapping.get(mime.lower(), "jpg")


# 全局单例
oss_storage = OSSStorage()
