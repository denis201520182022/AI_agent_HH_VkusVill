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
async def get_bot_response(system_prompt: str, dialogue_history: list, user_message: str, current_datetime_utc: datetime.datetime, attempt_tracker: list = None, skip_instructions: bool = False) -> dict:
    """
    Асинхронно отправляет запрос в OpenAI через прокси и получает ответ.
    """

    # --- ДОБАВЛЕНО: СЧЕТЧИК ПОПЫТОК ---
    # При каждом запуске (включая ретраи) добавляем метку в список
    if attempt_tracker is not None:
        attempt_tracker.append(datetime.datetime.now())
    # ----------------------------------
    
    messages = [
        {"role": "system", "content": system_prompt},
    ]
    messages.extend(dialogue_history)
    messages.append({"role": "user", "content": user_message})
    #print(messages)
    try:
        logger.info(f"Отправка запроса к LLM через прокси...")

        # --- ИЗМЕНЕНИЕ: ИСПОЛЬЗУЕМ РАСПРЕДЕЛЕННЫЙ СЕМАФОР ---
        # name="llm_global" - это имя ключа в Redis (будет semaphore:llm_global)
        async with DistributedSemaphore(name="llm_global", limit=GLOBAL_LLM_LIMIT):
            response = await client.chat.completions.create(
                model="gpt-4o-mini", # или gpt-5-mini как у тебя было
                messages=messages,
                max_completion_tokens=2500,
                response_format={"type": "json_object"},
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

        # Выводим информацию о токенах и кеше
        print("\n=== ТОКЕНЫ И КЕШ ===")
        print(f"📊 Всего токенов: {usage.total_tokens}")
        print(f"💬 Input токены: {usage.prompt_tokens}")
        print(f"📤 Output токены: {usage.completion_tokens}")
        print(f"⚡ Кешированные токены: {cached_tokens}")

        if usage.prompt_tokens > 0:
            cache_percent = (cached_tokens / usage.prompt_tokens) * 100
            print(f"📈 Процент кеша: {cache_percent:.1f}%")
        print()

        logger.info("Успешный ответ от LLM получен.")
        logger.info(f"Использовано токенов - Total: {usage.total_tokens}, Input: {usage.prompt_tokens}, Output: {usage.completion_tokens}, Cached: {cached_tokens}")

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
        # Логируем, что произошла ошибка и будет предпринята повторная попытка.
        # Tenacity сам логирует попытки на уровне INFO, но здесь можно добавить WARN.
        logger.warning(f"Ошибка при запросе к OpenAI: {type(e).__name__}: {e}. Будет предпринята повторная попытка (если не исчерпаны).", exc_info=True)
        # КРИТИЧЕСКИ ВАЖНО: Перевыбрасываем исключение, чтобы декоратор @retry мог его поймать
        # и решить, нужно ли повторять попытку.
        raise


async def cleanup():
    """
    Закрывает HTTP клиент при завершении работы приложения.
    Вызовите эту функцию в shutdown hook вашего приложения.
    """
    await async_http_client.aclose()
    await close_redis()
    logger.info("🔒 HTTP клиент закрыт")