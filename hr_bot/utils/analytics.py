# hr_bot/utils/analytics.py

import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from hr_bot.db.models import AnalyticsEvent, Dialogue, Vacancy

logger = logging.getLogger(__name__)

async def log_analytics_event(db: AsyncSession, dialogue: Dialogue, event_type: str, check_duplicates: bool = False):
    """
    Универсальная функция для записи события в аналитику.
    Автоматически подтягивает контекст (mode, task_id, city, vacancy, recruiter) из диалога.
    
    :param check_duplicates: Если True, проверяет, было ли уже такое событие в ТЕКУЩЕМ режиме и задаче.
                             Нужно для 'user_reply', чтобы не считать каждое сообщение.
    """
    try:
        # Если диалог не в режиме реактивации, и мы пока трекаем только ее - выходим (опционально)
        # Но по твоей задаче мы пишем всё, просто сейчас фокусируемся на реактивации.
        
        # 1. Защита от дублей (например, фиксируем только ПЕРВЫЙ ответ пользователя в рамках этой задачи)
        if check_duplicates:
            exists_query = select(AnalyticsEvent.id).filter_by(
                dialogue_id=dialogue.id,
                event_type=event_type,
                mode=dialogue.current_mode,
                mailing_task_id=dialogue.active_mailing_task_id
            ).limit(1)
            
            result = await db.execute(exists_query)
            if result.scalar():
                return # Событие уже записано, пропускаем

        # 2. Определяем город (берем из вакансии, так надежнее для срезов)
        city_val = "Не определен"
        if dialogue.vacancy and dialogue.vacancy.city:
            city_val = dialogue.vacancy.city
        elif dialogue.candidate and dialogue.candidate.city:
             city_val = dialogue.candidate.city

        # 3. Создаем запись
        event = AnalyticsEvent(
            dialogue_id=dialogue.id,
            mailing_task_id=dialogue.active_mailing_task_id, # ID задачи реактивации
            
            recruiter_id=dialogue.recruiter_id,
            vacancy_id=dialogue.vacancy_id,
            city=city_val,
            
            event_type=event_type,
            mode=dialogue.current_mode # 'standard' или 'reactivation'
        )
        
        db.add(event)
        # Flush делать не обязательно, если commit будет позже в основном коде,
        # но для надежности id можно сделать flush.
        # await db.flush() 

    except Exception as e:
        logger.error(f"Ошибка при записи аналитики ({event_type}): {e}", exc_info=True)