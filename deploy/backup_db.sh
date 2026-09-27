#!/usr/bin/env bash
# Ежедневный бэкап SQLite + persistence-файла разговоров. Ставится в cron
# пользователя psybot, хранит 14 дней.
# Пример crontab: 0 3 * * * /home/psybot/psybot/deploy/backup_db.sh
set -euo pipefail

APP_DIR="/home/psybot/psybot"
DB_PATH="$APP_DIR/data/bot.db"
PERSISTENCE_PATH="$APP_DIR/data/bot_persistence.pickle"
BACKUP_DIR="$APP_DIR/data/backups"
DATE=$(date +%Y-%m-%d)

mkdir -p "$BACKUP_DIR"
sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/bot-$DATE.db'"
[ -f "$PERSISTENCE_PATH" ] && cp "$PERSISTENCE_PATH" "$BACKUP_DIR/persistence-$DATE.pickle"

find "$BACKUP_DIR" -name 'bot-*.db' -mtime +14 -delete
find "$BACKUP_DIR" -name 'persistence-*.pickle' -mtime +14 -delete
