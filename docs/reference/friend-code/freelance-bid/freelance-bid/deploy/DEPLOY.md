# Деплой на сервер

Инструкция для разработчика, который поднимает проект у себя.

## Что это

Софт круглосуточно собирает заказы с FL.ru и Profi.ru, генерирует отклики через
Claude API по регламенту из `knowledge/` и отправляет их на биржах. Уведомления
приходят владельцу в Telegram. Подробности механики — в `README.md`.

## Требования

- Ubuntu 22.04+ / Debian 12+, **2 ГБ RAM минимум** (Chromium прожорлив)
- Python 3.12
- **Xvfb** — Playwright запускается не в headless-режиме: биржи заметно
  агрессивнее относятся к headless-браузеру
- Российский IP или мобильный прокси (см. ниже)

## Установка

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv xvfb
sudo mkdir -p /opt/freelance-bid && sudo chown "$USER" /opt/freelance-bid
# скопировать проект в /opt/freelance-bid
cd /opt/freelance-bid
python3.12 -m venv .venv
.venv/bin/pip install -e '.[browser,llm]'
.venv/bin/playwright install --with-deps chromium
```

## Настройка

```bash
cp .env.example .env
```

Обязательно заполнить: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`ANTHROPIC_API_KEY`, `PROXY_URL`. Затем:

```bash
.venv/bin/python -m fbid.cli doctor
```

Команда покажет, чего не хватает.

## Сессии бирж — главный нюанс

Сессии **нельзя** просто скопировать логином и паролем: и FL.ru, и Profi.ru
требуют подтверждение по SMS. Порядок такой:

1. На машине с графикой (или через X11-forwarding / VNC) выполнить:
   ```bash
   .venv/bin/python -m fbid.cli login fl
   .venv/bin/python -m fbid.cli login profi
   ```
   Откроется окно браузера, владелец входит руками.
2. Каталог `profiles/` скопировать на сервер целиком — там лежат куки.
3. Проверить: `.venv/bin/python -m fbid.cli check-session fl`

`profiles/` — это живые доступы к аккаунтам. Права `700`, в git не коммитить
(уже в `.gitignore`).

Если сессия слетит, владелец получит тревогу в Telegram, и вход придётся
повторить.

## Прокси

Мобильный LTE, **приватный** тариф. На общем канале до 5 пользователей, и IP
забанят из-за чужих действий.

Ротация IP каждые 5 минут рвёт туннель (`ERR_TUNNEL_CONNECTION_FAILED`). Код
переживает это повторными попытками, но если ротацию можно отключить — отключить.

## Запуск

Два юнита: основной цикл и телеграм-бот. Разделены намеренно — управление
должно работать, даже если цикл упал или перезапускается.

```bash
sudo cp deploy/freelance-bid.service deploy/freelance-bid-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now freelance-bid freelance-bid-bot
journalctl -u freelance-bid -f
```

Проверить, что бот жив: нажать любую кнопку в чате. Логи бота —
`journalctl -u freelance-bid-bot -f`.

## Лимиты меняются из бота, а не в .env

Кнопка «Лимиты» пишет значения в базу, и они **перебивают `.env`**. Поэтому:

* правка `.env` на сервере может не дать эффекта, если этот лимит уже менялся
  кнопкой — в боте такое значение помечено ✏️, а `fbid doctor` пишет
  «← изменён из бота»;
* вернуть управление в `.env` — кнопкой «Вернуть значение из .env»;
* у каждого лимита в коде жёсткий потолок (`fbid/limits.py`), выше которого не
  пустит никакая кнопка. Это защита от лишнего нуля в сообщении.

База открыта в режиме WAL: бот и цикл пишут в неё одновременно, без блокировок.

## Первый запуск: не снимайте DRY_RUN сразу

В `.env` стоит `DRY_RUN=true`. В этом режиме софт всё считает и присылает
владельцу заходы, но ничего не отправляет. Дайте ему проработать хотя бы
несколько часов, посмотрите в Telegram, что он собирался отправить, и только
потом ставьте `DRY_RUN=false`.

Экстренная остановка откликов без остановки сервиса:

```bash
.venv/bin/python -m fbid.cli pause
```

## Деньги

Отклики на Profi.ru платные, списываются с баланса аккаунта. Потолки в `.env`:

- `PROFI_MAX_BID_PRICE` — дороже не откликаемся
- `PROFI_DAILY_BUDGET` — потолок трат в сутки
- `PROFI_MIN_BALANCE` — ниже не опускаемся

Claude API тоже платный: примерно один запрос на заказ, прошедший фильтры.
Системный промт кэшируется, так что большая его часть оплачивается один раз.
Сгенерированный заход складывается в базу (`bids.draft_json`), поэтому заказ,
отложенный до вечера, второй раз к модели не идёт.

Если правились `knowledge/*.md`, старые заходы в кэше останутся написанными по
прежнему регламенту. Сбросить кэш:

```bash
sqlite3 data/fbid.db "UPDATE bids SET draft_json = NULL WHERE status = 'skipped'"
```

## Обслуживание

```bash
.venv/bin/python -m fbid.cli doctor      # состояние и траты за сутки
journalctl -u freelance-bid -n 200       # логи
sqlite3 data/fbid.db 'SELECT status, COUNT(*) FROM bids GROUP BY status'
```

База — единственное, что нужно бэкапить: в ней дедуп заказов и счётчик трат.
Потеря базы приведёт к повторным откликам на те же заказы.
