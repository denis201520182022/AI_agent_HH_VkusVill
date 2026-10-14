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

    sentry_logging = LoggingIntegration(
        level=logging.INFO,        
        event_level=logging.ERROR  
    )
    sentry_sdk.init(
        dsn=dsn,
        integrations=[
            AsyncioIntegration(),
            SqlalchemyIntegration(),
            HttpxIntegration(),
            sentry_logging,
        ],
        
        environment="production",
        traces_sample_rate=1.0,
        send_default_pii=True
    )
    logger.info("✅ Sentry успешно инициализирован.")