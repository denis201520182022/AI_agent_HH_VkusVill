
#hr_bot\tg_bot\handlers\user.py

from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy.orm import Session
from aiogram.utils.formatting import Text, Bold

from hr_bot.db.models import AppSettings
from hr_bot.tg_bot.filters import AdminFilter

router = Router()

router.message.filter(~AdminFilter()) # Только для обычных юзеров


@router.message(F.text == "⚙️ Баланс")
async def user_balance_status(message: Message, db_session: Session):
    settings = db_session.query(AppSettings).filter_by(id=1).first()
    if not settings:
        await message.answer("❌ Не удалось загрузить данные о балансе.")
        return
        
    content = Text(
        Bold("💰:"), "\n",
        "- Текущий баланс новых откликов: ", Bold(f"{settings.balance_standard:.2f}"), " руб.\n",
        "- Текущий баланс реактивации и фолоуапов: ", Bold(f"{settings.balance_outbound:.2f}"), " руб.\n\n",
        
        # Bold("📈 Статистика расходов (всего):"), "\n",
        # "- Новые диалоги: ", Bold(f"{settings.total_spent_standard:.2f}"), " руб.\n",
        # "- Follow-up (дожим): ", Bold(f"{settings.total_spent_followup:.2f}"), " руб.\n",
        # "- Реактивация (рассылки): ", Bold(f"{settings.total_spent_reactivation:.2f}"), " руб.\n\n",
        
        "ℹ️ ", Bold("Стоимость операций:"), "\n",
        "- Новый отклик: ", Bold(f"{settings.cost_per_dialogue:.2f}"), " руб.\n",
        "- Follow-up: ", Bold(f"{settings.cost_per_followup:.2f}"), " руб.\n",
        "- Сообщение реактивации: ", Bold(f"{settings.cost_per_reactivation:.2f}"), " руб."
    )
    
    await message.answer(**content.as_kwargs())