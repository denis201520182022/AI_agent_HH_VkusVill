from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from hr_bot.db.models import SyncSessionLocal 
from sqlalchemy.orm import Session, sessionmaker 

class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_pool: sessionmaker): 
        super().__init__()
        self.session_pool = session_pool

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        with self.session_pool() as session:
            data["db_session"] = session
            return await handler(event, data)