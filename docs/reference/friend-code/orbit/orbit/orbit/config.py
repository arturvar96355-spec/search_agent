"""Настройки Orbit.

Orbit держит минимум своего: токен бота, кому можно им пользоваться и где искать
подключённые системы. Всё остальное — ключи бирж, прокси, доступы к картам —
живёт в .env самих систем и остаётся там. Дублировать доступы в двух местах
значит однажды поменять их в одном и полдня искать, почему не работает.

Исключение одно: ключ Anthropic и доступ к почте. Тексты пишет сам Orbit
(на биржах это делает freelance-bid, а вот письма партнёрам и клиентам — здесь),
поэтому ключ нужен ему напрямую.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from loguru import logger
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Системы лежат рядом, на одном уровне с Orbit.
WORKSPACE = PROJECT_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram: один бот на все системы ---
    # ВАЖНО: с одним токеном может работать только один процесс. Orbit заменяет
    # собой отдельных ботов систем (`fbid bot`, `parser bot`), а не дополняет их.
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # --- Где искать системы ---
    # Нет каталога — раздел не подключится, Orbit об этом скажет и продолжит работать.
    bids_path: Path = WORKSPACE / "freelance-bid"
    leads_path: Path = WORKSPACE / "leadgen"

    # --- Какие разделы включены ---
    partners_enabled: bool = True
    clients_enabled: bool = True
    freelance_enabled: bool = True

    # --- Тексты писем ---
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5"
    # Адрес API. Пусто — идём напрямую в Anthropic. Заполняется, когда ключ
    # выдан не Anthropic, а сервисом-посредником: у такого ключа свой шлюз, и
    # без адреса он даёт «invalid x-api-key», сколько его ни вставляй.
    anthropic_base_url: str | None = None

    # --- Почта для рассылки ---
    # Пока пусто — письма собираются в очередь, но отправить их нечем.
    # Пароль приложения (не основной пароль ящика!) владелец вписывает сам.
    smtp_host: str | None = None
    smtp_port: int = 465  # 465 = SSL, 587 = STARTTLS, выбирается по номеру порта
    smtp_user: str | None = None  # partnership@uniqore.ru
    smtp_password: str | None = None
    smtp_from_name: str = "UniqOre"

    # --- Чтение того же ящика ---
    # Ответы агентств и возвраты писем читаются по IMAP. Логин и пароль те же,
    # что у отправки, поэтому отдельных полей для них нет.
    imap_host: str | None = "imap.beget.com"
    imap_port: int = 993
    # Читать ящик автоматически. Выключено — бот в почту не заходит вовсе.
    imap_enabled: bool = True

    # --- Пределы рассылки ---
    # Новый ящик, который в первый же день разослал сотню писем, уезжает в спам
    # вместе с доменом. Начинаем с малого и наращиваем вручную.
    daily_limit_partners: int = 20
    daily_limit_clients: int = 20
    # Автоотправка: пока выключена, письма ждут кнопки «Отправить».
    autosend: bool = False

    # --- Формы обратной связи на сайтах ---
    # Запасной канал для тех, у кого на сайте нет почты. ВЫКЛЮЧЕНО: пока
    # владелец не проверит это руками, Orbit только находит формы и
    # показывает их, но ничего не отправляет.
    webform_enabled: bool = False
    # Телефон, который подставляется в форму. Без него часть форм не примет
    # заявку: поле «телефон» на них обязательное.
    contact_phone: str | None = None

    db_path: Path = PROJECT_ROOT / "data" / "orbit.db"
    log_file: Path | None = None

    @property
    def api_gateway(self) -> str | None:
        """Адрес посредника — или None, если идём напрямую в Anthropic.

        Настоящий ключ Anthropic начинается с «sk-ant-» и работает только на
        api.anthropic.com. Если при таком ключе в настройках остался адрес
        посредника, это не «другой шлюз», а отправка НАСТОЯЩЕГО КЛЮЧА чужому
        сервису: он увидит его целиком и сможет тратить деньги со счёта.
        Поэтому такой адрес игнорируем и говорим об этом вслух.
        """
        gateway = (self.anthropic_base_url or "").strip()
        if not gateway:
            return None
        if (self.anthropic_api_key or "").startswith("sk-ant-"):
            logger.warning(
                "Ключ выдан самим Anthropic, а в настройках стоит посредник {} — "
                "игнорирую его: иначе ключ уйдёт чужому сервису",
                gateway,
            )
            return None
        return gateway

    @property
    def smtp_ready(self) -> bool:
        """Настроена ли почта. Без неё письма копятся в очереди, но не уходят."""
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    @property
    def imap_ready(self) -> bool:
        """Можно ли читать ящик. Логин и пароль общие с отправкой."""
        return bool(
            self.imap_enabled and self.imap_host and self.smtp_user and self.smtp_password
        )

    @property
    def allowed_chats(self) -> set[str]:
        """Кто может управлять ботом. Пусто — значит бот никому не отвечает."""
        return {self.telegram_chat_id} if self.telegram_chat_id else set()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
