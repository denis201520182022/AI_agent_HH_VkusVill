from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy.orm import Session
from aiogram.utils.formatting import Text, Bold

from hr_bot.db.models import AppSettings
from hr_bot.tg_bot.filters import AdminFilter

router = Router()

router.message.filter(~AdminFilter()) # Только для обычных юзеров


@router.message(F.text == "⚙️ Баланс") # Кнопка теперь называется так
async def user_balance_status(message: Message, db_session: Session):
    settings = db_session.query(AppSettings).filter_by(id=1).first()
    if not settings:
        await message.answer("❌ Не удалось загрузить данные о балансе.")
        return
        
    content = Text(
        Bold("💰 Текущий баланс системы:"), "\n\n",
        "Доступно: ", Bold(f"{settings.balance:.2f}"), " руб.\n\n",
        
        Bold("📈 Статистика расходов (всего c 25.12 13:35):"), "\n",
        "- На новые диалоги: ", Bold(f"{settings.total_spent_on_dialogues:.2f}"), " руб.\n",
        "- На напоминания: ", Bold(f"{settings.total_spent_on_reminders:.2f}"), " руб.\n",
        "- На follow-up: ", Bold(f"{settings.total_spent_on_followups:.2f}"), " руб.\n\n", # <-- Добавлено
        
        "ℹ️ ", Bold("Стоимость операций:"), "\n",
        "- Обработка нового отклика: ", Bold(f"{settings.cost_per_dialogue:.2f}"), " руб.\n",
        "- Долгое напоминание: ", Bold(f"{settings.cost_per_long_reminder:.2f}"), " руб.\n",
        "- Follow-up (дожим): ", Bold(f"{settings.cost_per_followup:.2f}"), " руб." # <-- Добавлено
    )
    
    await message.answer(**content.as_kwargs())