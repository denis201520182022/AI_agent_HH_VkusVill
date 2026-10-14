# hr_bot/services/llm_handler.py

import os
import json
import logging
import asyncio
from dotenv import load_dotenv
import httpx
from openai import AsyncOpenAI, APITimeoutError 
from tenacity import retry, stop_after_attempt, wait_exponential
import datetime
import os 
from hr_bot.utils.redis_lock import DistributedSemaphore 
import logging
from logging.handlers import TimedRotatingFileHandler
import os
from hr_bot.utils.redis_lock import DistributedSemaphore, get_redis_client, close_redis 


MODEL_PRICING = {
    "gpt-4o-mini": {
        "input": 0.150,
        "output": 0.600,
    },
    "gpt-4o": {
        "input": 2.500,
        "output": 10.000,
    }
}
USD_TO_RUB_RATE = 85.0
REDIS_COST_TTL = 60 * 60 * 24 * 60  # 60 дней (2 месяца)


class LlmValidationError(Exception):
    """Исключение для ошибок валидации ответа LLM (пустой ответ или кривой JSON)."""
    pass


load_dotenv()
logger = logging.getLogger(__name__)

LOG_DIR = "/app/logs"
LLM_DEBUG_LOG_PATH = os.path.join(LOG_DIR, "debug_llm_responses.log")

os.makedirs(LOG_DIR, exist_ok=True)

llm_debug_logger = logging.getLogger("llm_debug")
llm_debug_logger.setLevel(logging.INFO)
llm_debug_logger.propagate = False 

handler = TimedRotatingFileHandler(
    LLM_DEBUG_LOG_PATH, 
    when="midnight", 
    interval=1, 
    backupCount=1, 
    encoding="utf-8"
)
formatter = logging.Formatter('%(message)s') 
handler.setFormatter(formatter)
llm_debug_logger.addHandler(handler)

GLOBAL_LLM_LIMIT = int(os.getenv("GLOBAL_LLM_CONCURRENCY", 30))

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


async def _log_usage_to_redis(dialogue_id, model_name, usage_stats):
    """
    Записывает токены и стоимость в Redis для конкретного диалога.
    """
    if not dialogue_id or not usage_stats:
        return
    
    pricing = MODEL_PRICING.get(model_name)
    if not pricing:
        return
    
    p_tokens = usage_stats.get("prompt_tokens", 0)
    c_tokens = usage_stats.get("completion_tokens", 0)
    cached_tokens = usage_stats.get("cached_tokens", 0)
    
    # Расчет стоимости в USD
    # Кэшированные токены на 50% дешевле обычных (стандарт OpenAI)
    cost_usd = (
        ((p_tokens - cached_tokens) * pricing["input"] / 1_000_000) +
        (cached_tokens * (pricing["input"] * 0.5) / 1_000_000) +
        (c_tokens * pricing["output"] / 1_000_000)
    )
    
    redis = get_redis_client()
    key = f"llm_cost:dialogue:{dialogue_id}"
    
    try:
        # Используем pipeline для атомарности и производительности
        async with redis.pipeline(transaction=True) as pipe:
            pipe.hincrby(key, "prompt_tokens", p_tokens)
            pipe.hincrby(key, "completion_tokens", c_tokens)
            pipe.hincrby(key, "cached_tokens", cached_tokens)
            pipe.hincrbyfloat(key, "cost_usd", cost_usd)
            pipe.expire(key, REDIS_COST_TTL)
            await pipe.execute()
    except Exception as e:
        logger.error(f"Error logging LLM cost to Redis for dialogue {dialogue_id}: {e}")


async def get_dialogue_cost_info(dialogue_id) -> dict:
    """
    Возвращает информацию о затратах на диалог из Redis (в USD и RUB).
    """
    redis = get_redis_client()
    key = f"llm_cost:dialogue:{dialogue_id}"
    
    try:
        data = await redis.hgetall(key)
        if not data:
            return {"usd": 0.0, "rub": 0.0, "tokens": {}}
        
        cost_usd = float(data.get("cost_usd", 0))
        return {
            "usd": cost_usd,
            "rub": cost_usd * USD_TO_RUB_RATE,
            "tokens": {
                "prompt": int(data.get("prompt_tokens", 0)),
                "completion": int(data.get("completion_tokens", 0)),
                "cached": int(data.get("cached_tokens", 0))
            }
        }
    except Exception as e:
        logger.error(f"Error getting cost from Redis for dialogue {dialogue_id}: {e}")
        return {"usd": 0.0, "rub": 0.0, "tokens": {}}



@retry(
    stop=stop_after_attempt(6),  # Попытаться 6 раз
    wait=wait_exponential(multiplier=1, min=4, max=180), # До 180 секунд (3 минуты)
)
async def get_bot_response(system_prompt: str, dialogue_history: list, user_message: str, current_datetime_utc: datetime.datetime, attempt_tracker: list = None, skip_instructions: bool = False, extra_context: dict = None) -> dict:
    """
    Асинхронно отправляет запрос в OpenAI через прокси и получает ответ.
    """
    # При каждом запуске (включая ретраи) добавляем метку в список
    if attempt_tracker is not None:
        attempt_tracker.append(datetime.datetime.now())
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
                frequency_penalty=0.7,
                temperature=0.0
            )
        # 1. Сразу дампим весь ответ в JSON-строку
        full_response_dump = response.model_dump_json(indent=2)
        response_content = response.choices[0].message.content

        # 2. ЗАПИСЬ В ОТДЕЛЬНЫЙ ФАЙЛ (теперь через логгер с ротацией)
        try:
            log_entry = (
                f"\n{'='*50}\n"
                f"TIMESTAMP: {datetime.datetime.now()}\n"
                f"DIALOGUE_ID: {(extra_context or {}).get('dialogue_id', 'unknown')}\n"
                f"FULL_RESPONSE:\n{full_response_dump}\n"
                f"{'='*50}\n"
            )
            llm_debug_logger.info(log_entry)
        except Exception as log_error:
            ctx_logger.error(f"Error writing to debug log: {log_error}")
        response_content = response.choices[0].message.content
        finish_reason = response.choices[0].finish_reason 
        usage = response.usage

        # Безопасно получаем кешированные токены
        cached_tokens = 0
        if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details is not None:
            cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", 0)

        # Расчет процента кэша
        prompt_tokens = usage.prompt_tokens
        cache_percentage = 0.0
        if prompt_tokens > 0:
            cache_percentage = (cached_tokens / prompt_tokens) * 100


        ctx_logger.info(
            f"✅ LLM Response received. "
            f"[Action: llm_response_success] "
            f"[Tokens: Total={usage.total_tokens}, Prompt={usage.prompt_tokens}, "
            f"Compl={usage.completion_tokens}, Cached={cached_tokens} ({cache_percentage:.1f}%)]"
            f"Raw LLM Response: {response_content}..."
        )
        
        dialogue_id = (extra_context or {}).get("dialogue_id")
        if dialogue_id:
            await _log_usage_to_redis(dialogue_id, "gpt-4o-mini", {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "cached_tokens": cached_tokens
            })
        
        refusal_msg = getattr(response.choices[0].message, "refusal", None)
        
        if refusal_msg or not response_content or not response_content.strip():
            current_len = len(dialogue_history)
            if current_len > 10:
                dialogue_history[:] = dialogue_history[-10:]
                ctx_logger.warning(f"🔪 Отказ/пустота LLM. История усечена до 10 сообщений (было {current_len}).")
            elif current_len > 5:
                dialogue_history[:] = dialogue_history[-5:]
                ctx_logger.warning(f"🔪 Отказ/пустота LLM. История усечена до 5 сообщений (было {current_len}).")
            elif current_len > 2:
                dialogue_history[:] = dialogue_history[-2:]
                ctx_logger.warning(f"🔪 Отказ/пустота LLM. История усечена до 2 сообщений (было {current_len}).")
                
            error_msg = f"LLM returned refusal: {refusal_msg}" if refusal_msg else "LLM returned an empty response"
            raise LlmValidationError(error_msg)

        try:
            parsed_response = json.loads(response_content)
        except json.JSONDecodeError as je:
            raise LlmValidationError(f"LLM returned invalid JSON: {je}") from je

        return {
            "parsed_response": parsed_response,
            "usage_stats": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "cached_tokens": cached_tokens
            }
        }
       
    except Exception as e:
        ctx_logger.warning(
            f"Ошибка при запросе к OpenAI: {type(e).__name__}: {e}. Будет ретрай.", 
            exc_info=True,
            extra={"action": "llm_request_retry"}
        )
        raise


@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=1, min=4, max=180),
)
async def get_smart_bot_response(system_prompt: str, dialogue_history: list, user_message: str, current_datetime_utc: datetime.datetime, attempt_tracker: list = None, skip_instructions: bool = False, extra_context: dict = None) -> dict:
    """
    Асинхронно отправляет запрос к УМНОЙ модели (gpt-4o) для сложных задач (аудит дат, верификация).
    """
    if attempt_tracker is not None:
        attempt_tracker.append(datetime.datetime.now())
    ctx_logger = logging.LoggerAdapter(logger, extra_context or {})
    messages = [
        {"role": "developer", "content": system_prompt},
    ]
    messages.extend(dialogue_history)
    messages.append({"role": "user", "content": user_message})

    try:
        ctx_logger.info(f"🧠🧠Отправка запроса к GPT-5 (SMART mode)...")

        async with DistributedSemaphore(name="llm_global", limit=GLOBAL_LLM_LIMIT):
            # response = await client.chat.completions.create(
            #     model="gpt-5", # Или "gpt-5-thinking" для сверхсложных задач
            #     messages=messages,
            #     max_completion_tokens=1500, # Увеличили запас для reasoning tokens
            #     response_format={"type": "json_object"},
            #     # НОВЫЕ ПАРАМЕТРЫ GPT-5:
            #     reasoning_effort="low",  # Максимальная точность для аудита дат
            #     verbosity="low",         # Чтобы ответ был чистым JSON без лишнего текста
                
            # )
            response = await client.chat.completions.create(
                model="gpt-4o", 
                messages=messages,
                max_completion_tokens=2500,
                response_format={"type": "json_object"},
                frequency_penalty=0.7,
                temperature=0.0
            )

        response_content = response.choices[0].message.content
        usage = response.usage
        cached_tokens = 0
        
        if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details is not None:
            cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", 0)
        prompt_tokens = usage.prompt_tokens
        cache_percentage = 0.0
        if prompt_tokens > 0:
            cache_percentage = (cached_tokens / prompt_tokens) * 100
        ctx_logger.info(
            f"✅ SMART LLM Response received. "
            f"[Action: smart_llm_success] "
            f"[Tokens: Total={usage.total_tokens}, Prompt={usage.prompt_tokens}, "
            f"Compl={usage.completion_tokens}, Cached={cached_tokens} ({cache_percentage:.1f}%)]"
            f"Raw LLM Response: {response_content}..."
        )

        # --- ЛОГИРОВАНИЕ В REDIS ---
        dialogue_id = (extra_context or {}).get("dialogue_id")
        if dialogue_id:
            await _log_usage_to_redis(dialogue_id, "gpt-4o", {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "cached_tokens": cached_tokens
            })
        # ---------------------------

        refusal_msg = getattr(response.choices[0].message, "refusal", None)
        
        if refusal_msg or not response_content or not response_content.strip():
            current_len = len(dialogue_history)
            if current_len > 10:
                dialogue_history[:] = dialogue_history[-10:]
                ctx_logger.warning(f"🔪 Отказ/пустота SMART LLM. История усечена до 10 сообщений (было {current_len}).")
            elif current_len > 5:
                dialogue_history[:] = dialogue_history[-5:]
                ctx_logger.warning(f"🔪 Отказ/пустота SMART LLM. История усечена до 5 сообщений (было {current_len}).")
            elif current_len > 2:
                dialogue_history[:] = dialogue_history[-2:]
                ctx_logger.warning(f"🔪 Отказ/пустота SMART LLM. История усечена до 2 сообщений (было {current_len}).")
                
            error_msg = f"SMART LLM returned refusal: {refusal_msg}" if refusal_msg else "SMART LLM returned an empty response"
            raise LlmValidationError(error_msg)

        try:
            parsed_response = json.loads(response_content)
        except json.JSONDecodeError as je:
            raise LlmValidationError(f"SMART LLM returned invalid JSON: {je}") from je

        return {
            "parsed_response": parsed_response,
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