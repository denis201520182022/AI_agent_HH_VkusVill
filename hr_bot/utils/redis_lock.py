# hr_bot/utils/redis_lock.py

import asyncio
import logging
import time
import os
from redis.asyncio import Redis
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Инициализация глобального клиента Redis (будет создан при первом вызове)
_redis_client = None

def get_redis_client():
    global _redis_client
    if _redis_client is None:
        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", 6379))
        password = os.getenv("REDIS_PASSWORD", None)
        db = int(os.getenv("REDIS_DB", 0))
        _redis_client = Redis(
            host=host, 
            port=port, 
            password=password,
            db=db, 
            decode_responses=True
        )
    return _redis_client

class DistributedSemaphore:
    """
    Распределенный семафор на базе Redis.
    Гарантирует, что во всей системе (среди всех воркеров)
    одновременно выполняется не более `limit` операций.
    """
    def __init__(self, name: str, limit: int, timeout: int = 60):
        self.client = get_redis_client()
        self.name = f"semaphore:{name}"
        self.limit = limit
        self.timeout = timeout # Время жизни ключа (на случай краша воркера)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()

    async def acquire(self):
        """Пытается захватить слот. Если слотов нет — ждет."""
        while True:
            # Lua скрипт для атомарной проверки и инкремента
            # Если текущее значение < лимита: инкрементируем, обновляем TTL, возвращаем 1 (успех)
            # Иначе: возвращаем 0 (неудача)
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
                return 0
            end
            """
            result = await self.client.eval(lua_script, 1, self.name, self.limit, self.timeout)
            
            if result == 1:
                # logger.debug(f"Acquired semaphore {self.name}")
                return True
            
            # Если не удалось захватить, ждем немного и пробуем снова
            await asyncio.sleep(0.1 + (0.05 * (id(self) % 10))) # Небольшой джиттер

    async def release(self):
        """Освобождает слот (декремент)."""
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
        await self.client.eval(lua_script, 1, self.name)
        # logger.debug(f"Released semaphore {self.name}")

async def close_redis():
    """Закрывает соединение при остановке приложения"""
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        logger.info("Redis client closed")