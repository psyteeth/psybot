#!/usr/bin/env bash
# Обновление бота на VDS: git pull (через deploy key), установка зависимостей,
# рестарт сервиса. Запускать с локальной машины.
set -euo pipefail

SERVER="psybot@69.40.207.79"
KEY="$(dirname "$0")/../.deploy_secrets/psybot_admin_key"
REMOTE_DIR="/home/psybot/psybot"

ssh -i "$KEY" "$SERVER" "
  cd $REMOTE_DIR
  git pull
  .venv/bin/pip install -q -r requirements.txt
  export XDG_RUNTIME_DIR=/run/user/\$(id -u)
  systemctl --user restart psybot
  sleep 2
  systemctl --user status psybot --no-pager | head -n 10
"
