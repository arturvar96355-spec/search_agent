"""Запуск мини-аппа: python -m orbit.webapp

Порт и адрес берём из настроек, а не из кода: в туннель ходит cloudflared по
localhost, а в разработке страницу открывают тем же адресом в браузере.
"""
from __future__ import annotations

import os

import uvicorn
from loguru import logger


def main() -> None:
    host = os.getenv("ORBIT_WEBAPP_HOST", "127.0.0.1")
    port = int(os.getenv("ORBIT_WEBAPP_PORT", "8137"))
    from .auth import dev_mode

    if dev_mode():
        logger.warning(
            "Режим разработки: страница открывается без проверки Telegram, "
            "но только с этого компьютера"
        )
    logger.info("Мини-апп на http://{}:{}", host, port)
    uvicorn.run("orbit.webapp.api:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
