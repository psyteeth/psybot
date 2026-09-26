#!/usr/bin/env bash
# Ежедневный бэкап SQLite. Ставится в cron пользователя psybot, хранит 14 дней.
# Пример crontab: 0 3 * * * /home/psybot/psybot/deploy/backup_db.sh
set -euo pipefail

APP_DIR="/home/psybot/psybot"
DB_PATH="$APP_DIR/data/bot.db"
BACKUP_DIR="$APP_DIR/data/backups"
DATE=$(date +%Y-%m-%d)

mkdir -p "$BACKUP_DIR"
sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/bot-$DATE.db'"

find "$BACKUP_DIR" -name 'bot-*.db' -mtime +14 -delete
