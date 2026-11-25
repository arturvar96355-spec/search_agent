#!/bin/bash
# Пульт: приложение, туннель и присмотр за ними — одним процессом.
# Окно не закрывай: пока оно открыто, пульт работает. Остановить — Ctrl+C.
#
# Вся логика живёт в orbit/webapp/serve.py. Здесь только запуск: раньше этот
# файл был полноценным скриптом, и два его экземпляра дрались между собой.
command -v cloudflared >/dev/null || { echo "❌ Нет cloudflared. Поставь: brew install cloudflared"; exit 1; }
cd "$HOME/PyCharmMiscProject/orbit" || exit 1
exec env -u ANTHROPIC_BASE_URL .venv/bin/python -m orbit.webapp.serve
