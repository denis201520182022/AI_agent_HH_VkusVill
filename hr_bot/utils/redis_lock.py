#hr_bot\utils\redis_lock.py

import asyncio
import logging
import time
import os
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# Инициализация глобального клиента Redis
_redis_client = None

def get_redis_client():
    global _redis_client
    if _redis_client is None:
        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", 6379))
        password = os.getenv("REDIS_PASSWORD")
        if password is not None:
            password = password.strip()
        if not password:
            password = None
        
        db_index = int(os.getenv("REDIS_DB", 0)) 
        
        _redis_client = Redis(
            host=host, 
            port=port, 
            password=password, 
            db=db_index,
            decode_responses=True
        )
    return _redis_client

class DistributedSemaphore:
    def __init__(self, name: str, limit: int, timeout: int = 60):
        self.client = get_redis_client()
        self.name = f"semaphore:{name}"
        self.limit = limit
        self.timeout = timeout 

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()

    async def acquire(self):
        """Пытается захватить слот. Если слотов нет — ждет."""
        start_wait_time = time.time()
        log_interval = 5.0 # Логировать ожидание каждые 5 секунд
        last_log_time = start_wait_time

        while True:
            lua_script = """
            local current = redis.call('get', KEYS[1])
            if current == false then
                current = 0
            else
                current = tonumber(current)
            end

            if current < tonumber(ARGV[1]) then
                redis.call('incr', KEYS[1])
                redis.call('expire', KEYS[1], ARGV[2])
                return 1
            else
                return current
            end
            """
            try:
                result = await self.client.eval(lua_script, 1, self.name, self.limit, self.timeout)
                
                if result == 1:
                    return True
                
                # --- ДОБАВЛЕНО ЛОГИРОВАНИЕ ОЖИДАНИЯ ---
                current_time = time.time()
                if current_time - last_log_time > log_interval:
                    logger.warning(
                        f"⏳ Semaphore '{self.name}' is BUSY (Current: {result}/{self.limit}). "
                        f"Waiting for {int(current_time - start_wait_time)}s..."
                    )
                    last_log_time = current_time
                # --------------------------------------

            except Exception as e:
                logger.error(f"Redis error acquiring semaphore: {e}")
                await asyncio.sleep(5)
                continue
            
            await asyncio.sleep(0.1 + (0.05 * (id(self) % 10)))

    async def release(self):
        """Освобождает слот."""
        lua_script = """
        local current = redis.call('get', KEYS[1])
        if current == false then
            return 0
        else
            if tonumber(current) > 0 then
                redis.call('decr', KEYS[1])
                return 1
            else
                return 0
            end
        end
        """
        try:
            await self.client.eval(lua_script, 1, self.name)
        except Exception as e:
            logger.error(f"Redis error releasing semaphore: {e}")

async def close_redis():
    """Закрывает соединение при остановке приложения"""
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        logger.info("Redis client closed")


# === ДОБАВЬ В КОНЕЦ ФАЙЛА redis_lock.py ===

async def acquire_lock(key: str, timeout: int = 120) -> bool:
    """
    Захватывает блокировку через Redis.
    
    Args:
        key: Уникальный ключ блокировки (например, "dialogue_lock:123")
        timeout: Время жизни блокировки в секундах
    
    Returns:
        True если блокировка успешно захвачена, False если уже занята
    """
    client = get_redis_client()
    try:
        # SET key value NX EX timeout
        # NX - устанавливает только если ключа НЕ существует
        # EX - автоматически удалит через timeout секунд
        result = await client.set(key, "locked", nx=True, ex=timeout)
        return result is not None
    except Exception as e:
        logger.error(f"Redis error acquiring lock '{key}': {e}")
        return False


async def release_lock(key: str):
    """
    Освобождает блокировку.
    
    Args:
        key: Ключ блокировки для освобождения
    """
    client = get_redis_client()
    try:
        await client.delete(key)
    except Exception as e:
        logger.error(f"Redis error releasing lock '{key}': {e}")


# hr_bot\utils\redis_lock.py

# ... (импорты и get_redis_client остаются без изменений)
# ... (DistributedSemaphore остается без изменений)

class DistributedRateLimiter:
    """
    Распределенный ограничитель скорости (Rate Limiter) на основе Redis.
    Использует принцип скользящего окна с счетчиком и TTL (Sliding Window Counter).
    """
    def __init__(self, name: str, limit: int, period: float):
        self.client = get_redis_client()
        # Ключ будет: rate_limit:hh_api_global
        self.key_name = f"rate_limit:{name}"
        self.limit = limit
        self.period = period
        # Lua-скрипт для атомарного инкремента и установки TTL
        self._lua_script = """
        local key = KEYS[1]
        local limit = tonumber(ARGV[1])
        local period = tonumber(ARGV[2])

        -- 1. Инкрементируем счетчик (возвращает новое значение)
        local current = redis.call('incr', key)

        -- 2. Если это первый запрос в окне, устанавливаем TTL
        if current == 1 then
            redis.call('expire', key, period)
            -- Возвращаем 1 (Успех), и TTL (для ожидания)
            return {1, period}
        end

        -- 3. Если счетчик превышает лимит
        if current > limit then
            -- Возвращаем 0 (Лимит превышен), и текущий TTL
            local ttl = redis.call('ttl', key)
            return {0, ttl}
        else
            -- Возвращаем 1 (Успех), и текущий TTL
            local ttl = redis.call('ttl', key)
            return {1, ttl}
        end
        """
        # Загружаем скрипт в Redis
        self.script_sha = None

    async def _load_script(self):
        if self.script_sha is None:
            self.script_sha = await self.client.script_load(self._lua_script)
            logger.debug(f"Loaded DistributedRateLimiter script with SHA: {self.script_sha}")
        
    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass # Для Rate Limiter не нужно освобождать ресурс (декрементировать)

    async def acquire(self):
        """Ждет, пока не появится слот в окне лимита."""
        await self._load_script()

        while True:
            try:
                # ARGV: [limit, period]
                # Результат: [0 или 1 (статус), TTL (для ожидания)]
                result = await self.client.evalsha(
                    self.script_sha, 1, self.key_name, self.limit, int(self.period)
                )

                status = result[0]
                ttl = result[1]

                if status == 1:
                    # Успешно захватили слот
                    return True
                
                # Лимит превышен (status == 0)
                if ttl > 0:
                    # Ждем оставшееся время в окне (TTL) + небольшой буфер
                    wait_time = ttl + 0.1 
                    logger.warning(
                        f"⏳ Rate Limit '{self.key_name}' is EXCEEDED ({self.limit} req / {self.period}s). "
                        f"Waiting for {wait_time:.2f}s (TTL: {ttl})..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # TTL истек или не установлен (менее вероятный сценарий, 
                    # если скрипт сработал корректно). Ждем короткое время.
                    await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"Redis error acquiring rate limit '{self.key_name}': {e}", exc_info=True)
                await asyncio.sleep(1) # Ждем дольше при ошибке Redis


# ... (остальной код в redis_lock.py, включая close_redis, acquire_lock, release_lock)