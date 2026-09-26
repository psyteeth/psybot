#!/usr/bin/env bash
# Обновление бота на VDS. Пока код не в git-репозитории — синхронизирует
# локальную папку на сервер через rsync, ставит зависимости, рестартует сервис.
# Запускать с локальной машины (не на сервере).
set -euo pipefail

SERVER="psybot@69.40.207.79"
KEY="$(dirname "$0")/../.deploy_secrets/psybot_admin_key"
LOCAL_DIR="$(dirname "$0")/.."
REMOTE_DIR="/home/psybot/psybot"

rsync -av \
  --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.env' --exclude='.deploy_secrets' \
  --exclude='data/bot.db' --exclude='data/bot.db-journal' --exclude='data/backups' \
  --exclude='.git' \
  -e "ssh -i $KEY" \
  "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"

ssh -i "$KEY" "$SERVER" "
  cd $REMOTE_DIR
  .venv/bin/pip install -q -r requirements.txt
  export XDG_RUNTIME_DIR=/run/user/\$(id -u)
  systemctl --user restart psybot
  sleep 2
  systemctl --user status psybot --no-pager | head -n 10
"
