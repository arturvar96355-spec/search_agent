#!/bin/bash
# Переезд на нового телеграм-бота: новый токен и новый ID администратора.
#
# Спрашивает значения здесь, в терминале, и пишет их прямо в .env. В переписку
# они не попадают — а токен, засветившийся в чате, пришлось бы отзывать.
#
# Меняет два файла: orbit/.env (сам бот) и freelance-bid/.env (оттуда уходят
# тревоги тем же токеном). Лидоген не трогает: он сидит на своём боте и шлёт
# другому человеку.
set -u

ROOT="$HOME/PyCharmMiscProject"
ORBIT="$ROOT/orbit/.env"
FBID="$ROOT/freelance-bid/.env"

for f in "$ORBIT" "$FBID"; do
  [ -f "$f" ] || { echo "❌ Не нашёл $f"; exit 1; }
done

echo "Токен от @BotFather (вида 1234567890:AA...):"
read -r TOKEN
echo "Ваш chat_id (узнать: @userinfobot):"
read -r CHAT

TOKEN="$(echo "$TOKEN" | tr -d '[:space:]')"
CHAT="$(echo "$CHAT" | tr -d '[:space:]')"

# Проверяем ДО записи: неверный токен иначе ляжет в .env, бот молча не
# поднимется, и искать причину пришлось бы в логах.
echo
echo "Проверяю токен у Telegram…"
NAME="$(curl -s -m 20 "https://api.telegram.org/bot$TOKEN/getMe" \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"]["username"] if d.get("ok") else "")' 2>/dev/null)"
if [ -z "$NAME" ]; then
  echo "❌ Telegram не принял этот токен. Ничего не менял."
  exit 1
fi
echo "✅ Токен рабочий: @$NAME"

case "$CHAT" in
  ''|*[!0-9-]*) echo "❌ chat_id должен быть числом. Ничего не менял."; exit 1 ;;
esac

STAMP="$(date +%Y%m%d-%H%M%S)"
for f in "$ORBIT" "$FBID"; do
  cp "$f" "$f.bak-$STAMP"
  # В .env значение идёт как есть, без кавычек: так его читает pydantic.
  /usr/bin/sed -i '' "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=$TOKEN|" "$f"
  /usr/bin/sed -i '' "s|^TELEGRAM_CHAT_ID=.*|TELEGRAM_CHAT_ID=$CHAT|" "$f"
done
echo "✅ Прописал в orbit/.env и freelance-bid/.env (копии рядом: .bak-$STAMP)"

echo
echo "Перезапускаю бота…"
pkill -f "orbit.cli bot" 2>/dev/null
sleep 3
cd "$ROOT/orbit" || exit 1
nohup env -u ANTHROPIC_BASE_URL .venv/bin/python -m orbit.cli bot > /tmp/orbit_bot.log 2>&1 &
sleep 12

if pgrep -f "orbit.cli bot" > /dev/null; then
  echo "✅ Бот запущен. Напишите ему /start в Telegram — он должен ответить главным меню."
else
  echo "❌ Бот не поднялся. Последние строки лога:"
  tail -15 /tmp/orbit_bot.log
fi
