# /utils/analytics.py
import logging
from zoneinfo import ZoneInfo
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from hr_bot.db.models import AnalyticsEvent, Dialogue, Vacancy
from hr_bot.db.models import AnalyticsEvent, Dialogue, Vacancy, Candidate # <-- Добавь Candidate
logger = logging.getLogger(__name__)

# Часовой пояс для корректной записи даты собеседования
SPB_TIMEZONE = ZoneInfo("Europe/Moscow")

async def log_analytics_event(db: AsyncSession, dialogue: Dialogue, event_type: str, check_duplicates: bool = False):
    """
    Универсальная функция для записи любого события воронки.
    """
    try:
        # 0. Снимок даты собеседования (если она есть в диалоге на момент события)
        # Мы выносим это в начало, чтобы использовать в проверке дубликатов
        current_interview_date = None
        if dialogue.interview_datetime_utc:
            current_interview_date = dialogue.interview_datetime_utc.astimezone(SPB_TIMEZONE).date()

        # 1. Защита от дублей
        # Проверяем по набору: диалог + тип события + режим + дата собеса
        if check_duplicates:
            filters = [
                AnalyticsEvent.dialogue_id == dialogue.id,
                AnalyticsEvent.event_type == event_type,
                AnalyticsEvent.mode == dialogue.current_mode,
                AnalyticsEvent.mailing_task_id == dialogue.active_mailing_task_id
            ]
            
            # Если у события есть дата собеседования, она становится частью "уникального ключа"
            if current_interview_date:
                filters.append(AnalyticsEvent.interview_date == current_interview_date)
            else:
                filters.append(AnalyticsEvent.interview_date == None)

            stmt = select(AnalyticsEvent.id).filter(and_(*filters)).limit(1)
            
            result = await db.execute(stmt)
            if result.scalar():
                return

        # 2. Определяем город (безопасно через await db.get)
        city_val = "Не определен"
        
        # Получаем вакансию явно, чтобы не ловить MissingGreenlet
        vacancy = await db.get(Vacancy, dialogue.vacancy_id) if dialogue.vacancy_id else None
        
        if vacancy and vacancy.city:
            city_val = vacancy.city
        else:
            # Если в вакансии нет города, проверяем кандидата
            candidate = await db.get(Candidate, dialogue.candidate_id) if dialogue.candidate_id else None
            if candidate and candidate.city:
                city_val = candidate.city

        # 4. Создаем запись события
        event = AnalyticsEvent(
            dialogue_id=dialogue.id,
            mailing_task_id=dialogue.active_mailing_task_id, # ОЧЕНЬ ВАЖНО для Реактивации
            
            recruiter_id=dialogue.recruiter_id,
            vacancy_id=dialogue.vacancy_id,
            city=city_val,
            
            event_type=event_type,
            mode=dialogue.current_mode,
            interview_date=current_interview_date
        )
        
        db.add(event)

    except Exception as e:
        logger.error(f"Ошибка при записи аналитики ({event_type}): {e}", exc_info=True)

