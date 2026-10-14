import datetime 
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select 
from .models import Statistic

async def update_stats(db: AsyncSession, vacancy_id: int, responses: int = 0, started_dialogs: int = 0, qualified: int = 0):
    """
    Находит или создает запись о статистике за сегодняшний день для вакансии
    и инкрементирует нужные счетчики.
    """
    today = datetime.date.today() 

    result = await db.execute(
        select(Statistic).filter(
            Statistic.vacancy_id == vacancy_id,
            Statistic.date == today
        )
    )
    stats_record = result.scalar_one_or_none()

    if not stats_record:
        stats_record = Statistic(
            vacancy_id=vacancy_id,
            date=today
        )
        db.add(stats_record)
        await db.flush() 

    stats_record.responses_count += responses
    stats_record.started_dialogs_count += started_dialogs
    stats_record.qualified_count += qualified
    
    print(f"  > Статистика для вакансии {vacancy_id} обновлена: +{responses} откликов, +{started_dialogs} диалогов, +{qualified} квалифицировано.")