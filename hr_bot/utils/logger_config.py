# hr_bot/utils/logger_config.py

import logging
from logging.handlers import TimedRotatingFileHandler
import os
import sys

def setup_logging(log_filename: str):
    """
    Настраивает логирование в консоль и в указанный файл с ежедневной ротацией.
    """
    # Создаем папку для логов
    log_dir = 'logs'
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Формат
    log_format = "%(asctime)s - %(name)s - [%(levelname)s] - %(message)s"
    formatter = logging.Formatter(log_format)

    # 1. Настраиваем ROOT логгер (по умолчанию ловит всё)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG) # Глобально разрешаем DEBUG

    # Очистка дублей
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Хендлеры
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    
    file_handler = TimedRotatingFileHandler(
        os.path.join(log_dir, log_filename),
        when='midnight',
        interval=1,
        backupCount=4,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # 2. НАСТРОЙКА УРОВНЕЙ ДЛЯ МОДУЛЕЙ

    # Включаем DEBUG для твоего проекта (имен, начинающихся с hr_bot или __main__)
    logging.getLogger('hr_bot').setLevel(logging.DEBUG)
    logging.getLogger('__main__').setLevel(logging.DEBUG)

    # --- Настройка сторонних библиотек ---
    
    # Aiogram - шумит, оставляем INFO
    logging.getLogger('aiogram').setLevel(logging.INFO)
    
    # Httpx - если хочешь видеть запросы к API, ставь DEBUG. Если нет - WARNING.
    # Рекомендую пока поставить DEBUG, чтобы понять, что происходит.
    logging.getLogger('httpx').setLevel(logging.DEBUG) 
    
    # Httpcore - это очень низкоуровневые логи (байты), они обычно не нужны и забивают всё.
    # Глушим их до WARNING.
    logging.getLogger('httpcore').setLevel(logging.WARNING) 
    
    # OpenAI - тоже использует httpx, но можно настроить отдельно
    logging.getLogger('openai').setLevel(logging.DEBUG)

    # Прочие
    logging.getLogger('googleapiclient').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)