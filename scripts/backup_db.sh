#!/bin/bash
# 数据库备份脚本 - 每日自动备份 SQLite 到 OSS
# 用法：./scripts/backup_db.sh
# 推荐放入 crontab：0 3 * * * /app/scripts/backup_db.sh

set -e

DB_PATH="${RAID_SYNC_DB:-/data/sync.db}"
OSS_BUCKET="${OSS_BUCKET:-raidcaptain-backups}"
BACKUP_DIR="/tmp/sync_backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/sync_${TIMESTAMP}.db"

mkdir -p "${BACKUP_DIR}"

# 使用 SQLite 在线备份接口（推荐 .alipay/oss2 在压时）
sqlite3 "${DB_PATH}" ".backup '${BACKUP_FILE}'"
gzip "${BACKUP_FILE}"
BACKUP_FILE="${BACKUP_FILE}.gz"

echo "✅ 本地备份完成: ${BACKUP_FILE} ($(du -h ${BACKUP_FILE} | cut -f1))"

# 上传到 OSS（需要 rclone 配置 OSS remote）
if command -v rclone &> /dev/null; then
    rclone copy "${BACKUP_FILE}" "oss:${OSS_BUCKET}/sync_backups/"
    echo "✅ 已上传到 OSS: oss://${OSS_BUCKET}/sync_backups/$(basename ${BACKUP_FILE})"
fi

# 清理 30 天前的本地备份
find "${BACKUP_DIR}" -name "sync_*.db.gz" -mtime +30 -delete

echo "✅ 备份任务完成"