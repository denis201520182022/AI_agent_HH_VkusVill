# /utils/analytics.py
import logging
from zoneinfo import ZoneInfo
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from hr_bot.db.models import AnalyticsEvent, Dialogue, Vacancy
from hr_bot.db.models import AnalyticsEvent, Dialogue, Vacancy, Candidate 
logger = logging.getLogger(__name__)

SPB_TIMEZONE = ZoneInfo("Europe/Moscow")

async def log_analytics_event(db: AsyncSession, dialogue: Dialogue, event_type: str, check_duplicates: bool = False):
    """
    Универсальная функция для записи любого события воронки.
    """
    try:
        current_interview_date = None
        if dialogue.interview_datetime_utc:
            current_interview_date = dialogue.interview_datetime_utc.astimezone(SPB_TIMEZONE).date()

        if check_duplicates:
            filters = [
                AnalyticsEvent.dialogue_id == dialogue.id,
                AnalyticsEvent.event_type == event_type,
                AnalyticsEvent.mode == dialogue.current_mode,
                AnalyticsEvent.mailing_task_id == dialogue.active_mailing_task_id
            ]
            
            if current_interview_date:
                filters.append(AnalyticsEvent.interview_date == current_interview_date)
            else:
                filters.append(AnalyticsEvent.interview_date == None)

            stmt = select(AnalyticsEvent.id).filter(and_(*filters)).limit(1)
            
            result = await db.execute(stmt)
            if result.scalar():
                return

        city_val = "Не определен"
        vacancy = await db.get(Vacancy, dialogue.vacancy_id) if dialogue.vacancy_id else None
        
        if vacancy and vacancy.city:
            city_val = vacancy.city
        else:
            candidate = await db.get(Candidate, dialogue.candidate_id) if dialogue.candidate_id else None
            if candidate and candidate.city:
                city_val = candidate.city

        event = AnalyticsEvent(
            dialogue_id=dialogue.id,
            mailing_task_id=dialogue.active_mailing_task_id, 
            
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

