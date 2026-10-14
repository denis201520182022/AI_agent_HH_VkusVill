import logging
from logging.handlers import TimedRotatingFileHandler
import os
import sys
from pythonjsonlogger import jsonlogger
from logging.handlers import RotatingFileHandler

def get_log_level(env_var_name: str, default: str) -> int:
    """
    Получает уровень логирования из переменной окружения.
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
    Настраивает JSON логирование в консоль и файл.
    """
    log_dir = 'logs'
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    project_log_level = get_log_level("LOG_LEVEL", "INFO")

    format_str = '%(asctime)s %(name)s %(levelname)s %(message)s %(filename)s %(funcName)s %(lineno)d'
    
    formatter = jsonlogger.JsonFormatter(
        format_str,
        rename_fields={
            'asctime': 'timestamp',
            'levelname': 'level',
            'name': 'logger_name'
        },
        datefmt='%Y-%m-%dT%H:%M:%S%z'
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO) 

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Хендлер консоли (stdout) - его читает Docker и Promtail
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    
    # Хендлер файла (на всякий случай храним и в файлах)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, log_filename),
        maxBytes=20 * 1024 * 1024,  # 20 МБ в байтах
        backupCount=5,               # Хранить 5 старых файлов 
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    
    logging.getLogger('hr_bot').setLevel(project_log_level)
    logging.getLogger('__main__').setLevel(project_log_level)

    httpx_level = get_log_level("LOG_LEVEL_HTTPX", "WARNING") 
    logging.getLogger('httpx').setLevel(httpx_level)
    logging.getLogger('httpcore').setLevel(logging.WARNING) 

    openai_level = get_log_level("LOG_LEVEL_OPENAI", "WARNING")
    logging.getLogger('openai').setLevel(openai_level)

    aiogram_level = get_log_level("LOG_LEVEL_AIOGRAM", "INFO")
    logging.getLogger('aiogram').setLevel(aiogram_level)

    logging.getLogger('googleapiclient').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    logging.getLogger('google_auth_httplib2').setLevel(logging.WARNING) 
    logging.getLogger('google').setLevel(logging.WARNING)