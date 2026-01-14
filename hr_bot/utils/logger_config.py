import logging
from logging.handlers import TimedRotatingFileHandler
import os
import sys

def get_log_level(env_var_name: str, default: str) -> int:
    """
    Получает уровень логирования из переменной окружения.
    Если переменная не задана или некорректна, возвращает default.
    """
    value = os.getenv(env_var_name, default).upper()
    
    levels = {
        'CRITICAL': logging.CRITICAL,
        'ERROR': logging.ERROR,
        'WARNING': logging.WARNING,
        'INFO': logging.INFO,
        'DEBUG': logging.DEBUG,
        'NOTSET': logging.NOTSET,
    }
    return levels.get(value, levels[default])

def setup_logging(log_filename: str):
    """
    Настраивает логирование. Уровни берутся из переменных окружения.
    """
    # Создаем папку для логов
    log_dir = 'logs'
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Определяем глобальный уровень из .env (по умолчанию INFO для чистоты)
    # Переменная LOG_LEVEL управляет твоим кодом (hr_bot)
    project_log_level = get_log_level("LOG_LEVEL", "INFO")

    # Формат
    log_format = "%(asctime)s - %(name)s - [%(levelname)s] - %(message)s"
    formatter = logging.Formatter(log_format)

    # Настраиваем корневой логгер
    # Ставим ему самый низкий уровень, чтобы он пропускал всё, 
    # а фильтровать будем уже на уровне конкретных модулей
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG) 

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Хендлеры
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    
    file_handler = TimedRotatingFileHandler(
        os.path.join(log_dir, log_filename),
        when='midnight',
        interval=1,
        backupCount=7, # Храним неделю
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # --- НАСТРОЙКА УРОВНЕЙ ---

    # 1. Твой проект (основная логика)
    logging.getLogger('hr_bot').setLevel(project_log_level)
    logging.getLogger('__main__').setLevel(project_log_level)

    # 2. HTTPX (Запросы к HH.ru и OpenAI)
    # Часто нужно видеть тело запроса, поэтому выносим в отдельную переменную
    httpx_level = get_log_level("LOG_LEVEL_HTTPX", "WARNING") 
    logging.getLogger('httpx').setLevel(httpx_level)
    logging.getLogger('httpcore').setLevel(logging.WARNING) # httpcore почти всегда мусор

    # 3. OpenAI (обычно дублирует httpx, но можно настроить отдельно)
    openai_level = get_log_level("LOG_LEVEL_OPENAI", "WARNING")
    logging.getLogger('openai').setLevel(openai_level)

    # 4. Aiogram (Телеграм)
    aiogram_level = get_log_level("LOG_LEVEL_AIOGRAM", "INFO")
    logging.getLogger('aiogram').setLevel(aiogram_level)

    # 5. Шумные библиотеки (всегда WARNING или ERROR)
    logging.getLogger('googleapiclient').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)