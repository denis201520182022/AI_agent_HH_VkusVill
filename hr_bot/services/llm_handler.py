# hr_bot/services/llm_handler.py

import os
import json
import logging
import asyncio # <--- ДОБАВЛЕНО
from dotenv import load_dotenv
import httpx
from openai import AsyncOpenAI, APITimeoutError # <--- ДОБАВЛЕНО APITimeoutError
from tenacity import retry, stop_after_attempt, wait_exponential
import datetime
import os # <--- Добавь
from hr_bot.utils.redis_lock import DistributedSemaphore # <--- Добавь
from hr_bot.utils.redis_lock import close_redis 



load_dotenv()
logger = logging.getLogger(__name__)

# --- ДОБАВЛЕНО: Глобальный семафор для LLM запросов ---
# Определяет максимальное количество ОДНОВРЕМЕННЫХ запросов к OpenAI.
# Начните с небольшого значения, например, 5-10, и постепенно увеличивайте,
# если производительность позволяет и нет ошибок.
# MAX_CONCURRENT_LLM_REQUESTS = 40 # Например, 5 одновременных запросов
# LLM_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_LLM_REQUESTS)
# ---------------------------------------------------
# Получаем лимит из ENV
GLOBAL_LLM_LIMIT = int(os.getenv("GLOBAL_LLM_CONCURRENCY", 30))

# Загружаем настройки прокси из .env
SQUID_PROXY_HOST = os.getenv("SQUID_PROXY_HOST")
SQUID_PROXY_PORT = os.getenv("SQUID_PROXY_PORT")
SQUID_PROXY_USER = os.getenv("SQUID_PROXY_USER")
SQUID_PROXY_PASSWORD = os.getenv("SQUID_PROXY_PASSWORD")

# Формируем URL прокси с аутентификацией
proxy_url = (
    f"http://{SQUID_PROXY_USER}:{SQUID_PROXY_PASSWORD}@"
    f"{SQUID_PROXY_HOST}:{SQUID_PROXY_PORT}"
)

# Создаем асинхронный HTTP клиент с настройками прокси
async_http_client = httpx.AsyncClient(
    proxy=proxy_url,
    timeout=600.0
)

# Создаем АСИНХРОННЫЙ OpenAI клиент и передаем ему наш HTTP клиент
client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    http_client=async_http_client
)

logger.info(f"Клиент OpenAI настроен на работу через прокси: {SQUID_PROXY_HOST}:{SQUID_PROXY_PORT}")



@retry(
    stop=stop_after_attempt(3),  # Попытаться 3 раза (1 оригинальная + 2 повтора)
    wait=wait_exponential(multiplier=1, min=4, max=10), # Экспоненциальная задержка: 4с, 8с (максимум 10с)
    # По умолчанию tenacity повторяет попытки для любого исключения, наследующегося от Exception.
    # Поэтому retry_if_exception_type явно указывать не нужно для "любых ошибок".
)
async def get_bot_response(system_prompt: str, dialogue_history: list, user_message: str, current_datetime_utc: datetime.datetime, attempt_tracker: list = None, skip_instructions: bool = False, extra_context: dict = None) -> dict:
    """
    Асинхронно отправляет запрос в OpenAI через прокси и получает ответ.
    """

    # --- ДОБАВЛЕНО: СЧЕТЧИК ПОПЫТОК ---
    # При каждом запуске (включая ретраи) добавляем метку в список
    if attempt_tracker is not None:
        attempt_tracker.append(datetime.datetime.now())
    # ----------------------------------
    # Создаем контекстный логгер
    ctx_logger = logging.LoggerAdapter(logger, extra_context or {})
    messages = [
        {"role": "system", "content": system_prompt},
    ]
    messages.extend(dialogue_history)
    messages.append({"role": "user", "content": user_message})
    #print(messages)
    try:
        ctx_logger.info(f"🧬🧬🧬Отправка запроса к LLM через прокси...")

        # --- ИЗМЕНЕНИЕ: ИСПОЛЬЗУЕМ РАСПРЕДЕЛЕННЫЙ СЕМАФОР ---
        # name="llm_global" - это имя ключа в Redis (будет semaphore:llm_global)
        async with DistributedSemaphore(name="llm_global", limit=GLOBAL_LLM_LIMIT):
            response = await client.chat.completions.create(
                model="gpt-4o-mini", # или gpt-5-mini как у тебя было
                messages=messages,
                max_completion_tokens=2500,
                response_format={"type": "json_object"},
                frequency_penalty=0.7
            )
        # ----------------------------------------------------

        response_content = response.choices[0].message.content
        finish_reason = response.choices[0].finish_reason  # ### <--- НОВОЕ: Причина остановки


        # Извлекаем информацию о токенах
        usage = response.usage

        # Безопасно получаем кешированные токены
        cached_tokens = 0
        if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details is not None:
            cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", 0)

        
        ctx_logger.info(
            f"LLM Response received. Total tokens 📊: {usage.total_tokens}",
            extra={
                "action": "llm_response_success",
                "llm_stats": {
                    "total": usage.total_tokens,
                    "prompt": usage.prompt_tokens,
                    "completion": usage.completion_tokens,
                    "cached": cached_tokens,
                },
                "llm_response_raw": response_content[:500]  # Логируем начало ответа для отладки
            }
        )
        #print(response_content)
        parsed_response = json.loads(response_content)

        # --- ИЗМЕНИТЬ ЭТУ ЧАСТЬ (чтобы вернуть статистику наружу) ---
        return {
            "parsed_response": parsed_response,
            "usage_stats": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "cached_tokens": cached_tokens
            }
        }
        # ------------------------------------------------------------

    except Exception as e:
        # Используем ctx_logger вместо обычного logger
        ctx_logger.warning(
            f"Ошибка при запросе к OpenAI: {type(e).__name__}: {e}. Будет ретрай.", 
            exc_info=True,
            extra={"action": "llm_request_retry"}
        )
        raise

# Найти в конце файла или после функции get_bot_response

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
)
async def get_smart_bot_response(system_prompt: str, dialogue_history: list, user_message: str, current_datetime_utc: datetime.datetime, attempt_tracker: list = None, skip_instructions: bool = False, extra_context: dict = None) -> dict:
    """
    Асинхронно отправляет запрос к УМНОЙ модели (gpt-4o) для сложных задач (аудит дат, верификация).
    """
    if attempt_tracker is not None:
        attempt_tracker.append(datetime.datetime.now())
    ctx_logger = logging.LoggerAdapter(logger, extra_context or {})
    # Рекомендуется использовать роль 'developer' для системных инструкций в GPT-5
    messages = [
        {"role": "developer", "content": system_prompt},
    ]
    messages.extend(dialogue_history)
    messages.append({"role": "user", "content": user_message})

    try:
        ctx_logger.info(f"🧬🧬Отправка запроса к GPT-5 (SMART mode)...")

        async with DistributedSemaphore(name="llm_global", limit=GLOBAL_LLM_LIMIT):
            response = await client.chat.completions.create(
                model="gpt-5", # Или "gpt-5-thinking" для сверхсложных задач
                messages=messages,
                max_completion_tokens=1500, # Увеличили запас для reasoning tokens
                response_format={"type": "json_object"},
                # НОВЫЕ ПАРАМЕТРЫ GPT-5:
                reasoning_effort="low",  # Максимальная точность для аудита дат
                verbosity="low",         # Чтобы ответ был чистым JSON без лишнего текста
                
            )

        response_content = response.choices[0].message.content
        usage = response.usage
        cached_tokens = 0
        
        if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details is not None:
            cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", 0)

        ctx_logger.info(
            f"SMART LLM Response received. Tokens ⚡: {usage.total_tokens}",
            extra={
                "action": "smart_llm_success",
                "llm_stats": {
                    "total": usage.total_tokens,
                    "prompt": usage.prompt_tokens,
                    "completion": usage.completion_tokens,
                    "cached": cached_tokens,
                }
            }
        )

        return {
            "parsed_response": json.loads(response_content),
            "usage_stats": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "cached_tokens": cached_tokens
            }
        }

    except Exception as e:
        ctx_logger.warning(f"Ошибка в SMART LLM: {type(e).__name__}: {e}. Ретрай...", extra={"action": "smart_llm_retry"})
        raise


async def cleanup():
    """
    Закрывает HTTP клиент при завершении работы приложения.
    Вызовите эту функцию в shutdown hook вашего приложения.
    """
    await async_http_client.aclose()
    await close_redis()
    logger.info("🔒 HTTP клиент закрыт")