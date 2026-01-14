import sentry_sdk
import os
import logging
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

logger = logging.getLogger(__name__)

def init_sentry():
    """
    Инициализирует Sentry, если задана переменная окружения SENTRY_DSN.
    """
    dsn = os.getenv("SENTRY_DSN")
    
    if not dsn:
        logger.warning("⚠️ SENTRY_DSN не найден. Мониторинг ошибок отключен.")
        return

    # Настройка интеграции с logging:
    # event_level=ERROR означает, что все logger.error(...) улетят в Sentry как ошибки.
    # level=INFO означает, что хлебные крошки (история действий) будут собираться начиная с INFO.
    sentry_logging = LoggingIntegration(
        level=logging.INFO,        # Capture info and above as breadcrumbs
        event_level=logging.ERROR  # Send errors as events
    )

    sentry_sdk.init(
        dsn=dsn,
        # Подключаем интеграции для твоих библиотек
        integrations=[
            AsyncioIntegration(),
            SqlalchemyIntegration(),
            HttpxIntegration(),
            sentry_logging,
        ],
        
        # Устанавливаем окружение (чтобы не путать тесты с продакшеном)
        environment="production",
        
        # Процент транзакций для трассировки производительности.
        # Для начала ставим 1.0 (100%), чтобы видеть всё. Если кончится лимит, уменьши до 0.1.
        traces_sample_rate=1.0,
        
        # Отправлять ли локальные переменные в отчете об ошибке (ОЧЕНЬ ПОЛЕЗНО)
        send_default_pii=True
    )
    logger.info("✅ Sentry успешно инициализирован.")