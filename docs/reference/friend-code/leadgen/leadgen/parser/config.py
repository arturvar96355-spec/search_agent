"""Настройки проекта: читаем .env, храним лимиты и rate-limiting.

Ключи API берём ТОЛЬКО из окружения/.env — никогда не хардкодим и не логируем.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"


class Settings(BaseSettings):
    """Единая точка настроек. Имена полей совпадают с переменными .env (без учёта регистра)."""

    model_config = SettingsConfigDict(
        # Абсолютный путь: под systemd CWD=/, и относительный ".env" молча не находится —
        # тогда все ключи=None и Bot() падает в crash-loop с «токен не задан».
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Прокси для браузерного добора (обязателен на VPS: датацентр-IP банит 2ГИС) ---
    # Один или НЕСКОЛЬКО прокси через запятую/перевод строки — пул с ротацией при бане.
    # Формат каждого: http://user:pass@host:port или host:port:user:pass
    # (для Chromium — только HTTP, НЕ socks5 с авторизацией).
    proxy_url: str | None = None

    # --- API-ключи (только из .env; никогда не хардкодим и не логируем) ---
    dgis_api_key: str | None = None
    yandex_api_key: str | None = None
    dadata_api_key: str | None = None
    dadata_secret: str | None = None

    # --- Google Sheets: путь к JSON сервисного аккаунта ---
    google_service_account_json: Path | None = None

    # --- Telegram-бот для доставки лидов ---
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    # Кто может управлять ботом (запускать поиск). Пусто = только telegram_chat_id.
    # В .env — через запятую: ALLOWED_CHAT_IDS=123,456
    allowed_chat_ids: str | None = None

    # --- Источник для клиентского поиска (бот и коллектор) ---
    # auto  — 2ГИС API, пока ключ жив; как только ключ исчерпан/не задан — браузерный поиск;
    # 2gis  — только API (при исчерпанном ключе вернёт ноль);
    # browser — только браузер (ключ не нужен вообще, но медленно).
    search_source: str = "auto"

    # --- Фильтр свежести отзывов ---
    # Жалобы учитываем только из отзывов этого года и новее (старьё неактуально для аутрича).
    reviews_min_year: int = 2023

    # Добор отзывов из Яндекс.Карт (второй источник). ЭКСПЕРИМЕНТАЛЬНО: удваивает время на карточку
    # и требует хорошего прокси. Выключено по умолчанию — включать, когда прокси стабильный.
    use_yandex_reviews: bool = False

    # --- Фоновый сбор базы (коллектор) ---
    # Бережный режим по умолчанию (общий 2ГБ-сервер): короткие пачки + длинный кулдаун,
    # между пачками браузер закрывается и память освобождается. На новом сервере ускоряем
    # через .env: COLLECT_PER_QUERY=20, COLLECT_DELAY=3.
    collect_target: int = 100  # до скольких лидов собирать, потом остановиться
    collect_delay: float = 25.0  # кулдаун между пачками (браузер закрыт — память свободна), сек
    collect_per_query: int = 5  # мест за одну пачку (короче пачка = меньше времени с открытым браузером)

    # --- Email-рассылка по партнёрам (parser outreach; подробно — EMAIL_OUTREACH.md) ---
    # Заполняются владельцем при подключении почты. Пароль — ПАРОЛЬ ПРИЛОЖЕНИЯ, не основной.
    smtp_host: str | None = None  # напр. smtp.yandex.ru
    smtp_port: int = 465  # 465 = SSL, 587 = STARTTLS — выбирается автоматически по порту
    smtp_user: str | None = None  # полный email отправителя, напр. partner@uniqore.ru
    smtp_password: str | None = None
    smtp_from_name: str = "Менеджер Uniqore"
    outreach_daily_limit: int = 10  # темп из брифа продаж: 10 писем в день

    # --- Логи (для VPS: файл с ротацией; пусто = только stderr/journald) ---
    log_file: Path | None = None
    log_rotation_mb: int = 20
    log_retention_days: int = 7

    # --- Лимиты и rate limiting ---
    max_results: int = Field(default=200, description="Максимум организаций на один запрос (рубрика+город).")
    # 10 — предел демо-ключа 2ГИС (page_size=50 отдаёт HTTP 400 и жжёт запрос впустую).
    # На платном тарифе можно поднять через .env.
    page_size: int = Field(default=10, description="Размер страницы пагинации API.")
    request_delay: float = Field(default=1.0, description="Пауза между запросами к API, сек.")
    max_retries: int = Field(default=3, description="Число попыток при 429/5xx (tenacity).")
    request_timeout: float = Field(default=20.0, description="Таймаут HTTP-запроса (API), сек.")
    browser_timeout: float = Field(
        default=60.0,
        description="Таймаут загрузки страницы в браузере, сек. Больше API: 2ГИС тяжёлый, "
        "а через мобильный прокси грузится медленно (20с не хватало → жалобы не читались).",
    )

    # --- Пути ---
    output_dir: Path = DEFAULT_OUTPUT_DIR

    @field_validator("search_source")
    @classmethod
    def _known_source(cls, value: str) -> str:
        """Опечатка в .env не должна тихо ронять поиск в ноль — падаем на старте."""
        value = (value or "").strip().lower() or "auto"
        if value not in {"auto", "2gis", "browser"}:
            raise ValueError("SEARCH_SOURCE должен быть auto | 2gis | browser")
        return value

    @field_validator("google_service_account_json", "log_file", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Пустая строка в .env (VAR=) для пути -> None, иначе Path('') == '.' ломает проверки."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    def proxy_list(self) -> list[str]:
        """Все прокси из PROXY_URL (через запятую или с новой строки). Пусто — прокси не задан."""
        raw = (self.proxy_url or "").replace("\n", ",")
        return [part.strip() for part in raw.split(",") if part.strip()]

    def allowed_chats(self) -> set[int]:
        """Множество разрешённых chat_id (кто управляет ботом). Пусто -> только telegram_chat_id."""
        ids: set[int] = set()
        for raw in (self.allowed_chat_ids or "").split(","):
            raw = raw.strip()
            if raw.lstrip("-").isdigit():
                ids.add(int(raw))
        if self.telegram_chat_id and self.telegram_chat_id.lstrip("-").isdigit():
            ids.add(int(self.telegram_chat_id))
        return ids

    def masked(self) -> dict[str, str]:
        """Безопасный дамп для логов: значения ключей маскируются."""
        secret_fields = {
            "dgis_api_key",
            "yandex_api_key",
            "dadata_api_key",
            "dadata_secret",
            "telegram_bot_token",
            "proxy_url",  # содержит login:pass
            "smtp_password",
        }
        dump: dict[str, str] = {}
        for name, value in self.model_dump().items():
            if name in secret_fields:
                dump[name] = "***" if value else "(не задан)"
            else:
                dump[name] = str(value)
        return dump


@lru_cache
def get_settings() -> Settings:
    """Кэшированный доступ к настройкам (читает .env один раз за процесс)."""
    return Settings()
