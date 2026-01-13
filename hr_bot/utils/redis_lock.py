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
        
        # --- ИСПРАВЛЕНИЕ: ЖЕЛЕЗОБЕТОННАЯ ПРОВЕРКА ПАРОЛЯ ---
        password = os.getenv("REDIS_PASSWORD")
        
        # 1. Если переменная есть, убираем пробелы по краям
        if password is not None:
            password = password.strip()
            
        # 2. Если после этого осталась пустая строка — ставим None
        if not password:
            password = None
        # ---------------------------------------------------
        
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
    """
    Распределенный семафор на базе Redis.
    """
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
                return 0
            end
            """
            try:
                result = await self.client.eval(lua_script, 1, self.name, self.limit, self.timeout)
                
                if result == 1:
                    return True
            except Exception as e:
                # Логируем ошибку, но не спамим в консоль каждую миллисекунду
                # logger.error(f"Redis error acquiring semaphore: {e}")
                await asyncio.sleep(5) # Если Redis упал, ждем 5 секунд
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