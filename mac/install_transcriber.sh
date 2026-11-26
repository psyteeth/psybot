#!/usr/bin/env bash
# Ставит расшифровщик Телемоста в launchd (каждые 30 минут с 11:00 до 17:00).
# Повторный запуск безопасен: задача перезагружается с новым plist.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.psyteeth.telemost-transcribe"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
MODEL="${WHISPER_MODEL:-$HOME/.local/share/whisper.cpp/ggml-large-v3-turbo.bin}"

[ -x "$HOME/aider-test/psybot/.venv/bin/python" ] || { echo "Нет ~/aider-test/psybot/.venv"; exit 1; }
[ -f "$MODEL" ] || echo "⚠️  Модели whisper ещё нет: $MODEL (см. mac/README.md, шаг «Модель»)"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Documents/Yandex.Telemost/расшифровки"
sed "s|__HOME__|$HOME|g" "$DIR/$LABEL.plist" > "$DEST"
plutil -lint "$DEST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Готово: $DEST"
echo "Проверить вручную: $HOME/aider-test/psybot/.venv/bin/python $DIR/telemost_transcribe.py --dry-run"
echo "Лог: $HOME/Library/Logs/telemost_transcribe.log"
