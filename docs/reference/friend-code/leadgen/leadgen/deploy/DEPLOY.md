# Деплой leadgen на VPS (2 ГБ RAM)

Чеклист по итогам аудита. Порядок важен. Проверено под Debian/Ubuntu.

## 0. Прежде чем деплоить — про 2 ГБ RAM

Самая тяжёлая часть — headless Chromium (~400–600 МБ RSS на карточке 2ГИС).
В коде уже стоит **глобальный семафор: не больше ОДНОГО Chromium на процесс**
(`parser/providers/browser.py::_BROWSER_SLOT`). Плюс systemd-лимит памяти ниже
не даст боту утащить весь сервер. Один поиск за раз на 2 ГБ работает предсказуемо.

## 1. Swap как страховка от жёсткого OOM (1–2 ГБ)

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 2. Пользователь и код

```bash
# -m ОБЯЗАТЕЛЕН: без домашнего каталога playwright не сможет положить Chromium
# в $HOME/.cache/ms-playwright — шаг 3 упадёт или браузер не найдётся в рантайме.
sudo useradd -r -m -d /home/leadgen -s /usr/sbin/nologin leadgen
sudo mkdir -p /opt/leadgen && sudo chown leadgen:leadgen /opt/leadgen
# скопировать проект в /opt/leadgen (rsync/git). Затем:
cd /opt/leadgen
sudo -u leadgen python3.12 -m venv .venv
# Все три extra нужны: browser — Chromium-добор, enrich — DaData (ИНН),
# partners — режим агентств и email-рассылка. Без partners эти команды не заработают.
sudo -u leadgen .venv/bin/pip install -e '.[browser,enrich,partners]'
```

## 3. Chromium + системные библиотеки (иначе браузерный добор молча не работает)

`pip install` НЕ ставит ни бинарь Chromium, ни системные `.so` (libnss3, libgbm, …).
Обязательно **с `--with-deps`**:

```bash
sudo -u leadgen .venv/bin/playwright install --with-deps chromium
# если нет прав ставить apt-пакеты под leadgen — один раз от root:
#   sudo .venv/bin/playwright install-deps chromium
```
Повторять после каждого апгрейда пакета `playwright`.

## 4. Секреты — `.env`

```bash
sudo -u leadgen cp .env.example .env
sudo -u leadgen nano .env            # вписать реальные ключи
sudo chmod 600 .env                  # только владелец читает (там ключи 2ГИС/DaData/Telegram)
```
Обязательно заполнить: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (без них бот не стартует)
и **`PROXY_URL`** — на сервере он не опционален: с датацентр-IP 2ГИС банит браузер, и
телефоны с отзывами собираться не будут. Крайне желательно `DADATA_API_KEY`/`DADATA_SECRET`
(ИНН и руководитель) и `DGIS_API_KEY` (без него поиск идёт браузером — медленнее, см.
`BROWSER_SEARCH.md`). **`ALLOWED_CHAT_IDS`** — числовые id тех, кто может
управлять ботом (через запятую); свой узнать `parser tg-chat-id`. Если оставить пусто,
доступ будет только у владельца — но лишь когда `TELEGRAM_CHAT_ID` **числовой**. При
`TELEGRAM_CHAT_ID=@канал` allowlist окажется ПУСТ и бот ответит любому (запустит поиск,
Chromium, `/clearbase`) — в логе будет предупреждение. На VPS задавай явно.
Для логов можно задать `LOG_FILE=/var/log/leadgen/bot.log`. Каталог в `/var/log` бот под
пользователем `leadgen` создать НЕ сможет (нет прав) и упадёт в перезапуск — создай заранее:
```bash
sudo mkdir -p /var/log/leadgen && sudo chown leadgen:leadgen /var/log/leadgen
```
Проще вариант: не задавать `LOG_FILE` вовсе — логи уйдут в journald (`journalctl -u leadgen-bot -f`).

## 5. systemd (автозапуск + рестарт при краше + лимит памяти)

```bash
sudo cp deploy/leadgen-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now leadgen-bot
sudo systemctl status leadgen-bot
journalctl -u leadgen-bot -f          # живые логи
```
`MemoryMax=1400M` в юните — cgroup убьёт ТОЛЬКО бота, если Chromium раздуется,
а не весь VPS; `Restart=always` поднимет обратно.

## 6. Один инстанс

НЕ запускать `parser bot` руками, пока крутится сервис — Telegram отдаст **409 Conflict**,
и оба процесса будут флапать (бот теперь логирует это как ERROR). Проверка:
```bash
pgrep -fa 'parser bot'
```

## 7. Ограничить рост логов journald

```bash
sudo sed -i 's/#SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
```
Если пишешь в файл (`LOG_FILE`), ротация уже встроена (20 МБ × 7 дней, zip).

## 8. Проверка после старта

- `journalctl -u leadgen-bot -f` → «Бот запущен», «Меню команд зарегистрировано».
- В Telegram: `/start` → одна ниша + один город + 10 → должны прийти карточки.
- `systemctl status leadgen-bot` → Memory: пик во время поиска не должен упираться в 1400M.

## Что осталось «жив, но может быть бесполезен» (следить)

- **Бан 2ГИС с датацентр-IP.** Браузерный добор с серверного IP может ловить капчу —
  тогда телефоны/отзывы не добираются, бот пишет «жалоб не нашёл». Признак в логе:
  подряд карточки без телефонов. Решение — резидентный/мобильный прокси или платный
  `contact_groups` в API 2ГИС.
- **Демо-ключ 2ГИС** быстро выбьет лимит в режиме «все ниши». Бот теперь логирует это
  WARNING'ом («лимит демо-ключа или бан»). Для боя — свой платный ключ.
- **output/*.jsonl** растёт — периодически чистить (на 30 ГБ не срочно).
