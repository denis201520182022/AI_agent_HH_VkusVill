import datetime
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

# --- ИЗМЕНЕНИЕ: Добавили InterviewFollowup в импорт ---
from hr_bot.db.models import InterviewReminder, Dialogue, TrackedRecruiter, InterviewFollowup 

logger = logging.getLogger(__name__)

# Определяем часовой пояс Санкт-Петербурга
try:
    SPB_TIMEZONE = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    logger.critical("Часовой пояс 'Europe/Moscow' не найден. Убедитесь, что система имеет актуальную базу данных часовых поясов (tzdata).")
    SPB_TIMEZONE = None 

async def schedule_interview_reminders(
    dialogue_id: int,
    interview_date_str: str,
    interview_time_str: str,
    db_session: AsyncSession
):
    """
    Планирует напоминания о собеседовании (ДО) и Follow-up (ПОСЛЕ).
    """
    if SPB_TIMEZONE is None:
        logger.error("Не удалось инициализировать часовой пояс Санкт-Петербурга. Напоминания не будут запланированы.")
        return

    logger.info(f"Начало планирования напоминаний для диалога {dialogue_id} на дату {interview_date_str} {interview_time_str}.")

    # 1. Загружаем диалог
    dialogue_result = await db_session.execute(
        select(Dialogue)
        .options(selectinload(Dialogue.recruiter))
        .filter_by(id=dialogue_id)
    )
    dialogue = dialogue_result.scalar_one_or_none()

    if not dialogue or not dialogue.recruiter:
        logger.error(f"Диалог или рекрутер не найдены для ID {dialogue_id}.")
        return

    recruiter_id = dialogue.recruiter_id
    hh_response_id = dialogue.hh_response_id

    # 2. Парсим дату и время
    try:
        interview_date_obj = datetime.datetime.strptime(interview_date_str, "%Y-%m-%d").date()
        interview_time_obj = datetime.datetime.strptime(interview_time_str, "%H:%M").time()
        
        interview_datetime_spb_naive = datetime.datetime.combine(interview_date_obj, interview_time_obj)
        interview_datetime_spb = interview_datetime_spb_naive.replace(tzinfo=SPB_TIMEZONE)
        interview_datetime_utc = interview_datetime_spb.astimezone(datetime.timezone.utc)
        
    except ValueError as e:
        logger.error(f"Ошибка парсинга даты/времени: {e}")
        return

    # 3. Обновляем диалог
    dialogue.interview_datetime_utc = interview_datetime_utc
    db_session.add(dialogue)

    # =================================================================================
    # ЧАСТЬ 1: НАПОМИНАНИЯ "ДО" СОБЕСЕДОВАНИЯ (Существующая логика)
    # =================================================================================
    
    # Сначала отменяем старые
    await db_session.execute(
        update(InterviewReminder)
        .where(InterviewReminder.dialogue_id == dialogue_id)
        .where(InterviewReminder.status == 'pending')
        .values(status='cancelled', processed_at=datetime.datetime.now(datetime.timezone.utc))
    )

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    reminders_to_add = []

    # Напоминание: За 2 часа
    send_2h_before_utc = interview_datetime_utc - datetime.timedelta(hours=2)
    if send_2h_before_utc > now_utc:
        reminders_to_add.append(
            InterviewReminder(
                dialogue_id=dialogue_id,
                recruiter_id=recruiter_id,
                interview_datetime_utc=interview_datetime_utc,
                scheduled_send_time_utc=send_2h_before_utc,
                notification_type='2_hours_before'
            )
        )

    # Напоминание: За 1 день до 20:00 (сложная логика с исключением поздних собеседований)
    send_date_1day_before_spb = interview_datetime_spb.date() - datetime.timedelta(days=1)
    send_1day_before_spb = datetime.datetime.combine(send_date_1day_before_spb, datetime.time(20, 0)).replace(tzinfo=SPB_TIMEZONE)
    send_1day_before_utc = send_1day_before_spb.astimezone(datetime.timezone.utc)

    interview_time_is_20h_or_later = interview_datetime_spb.time() >= datetime.time(20, 0)

    if send_1day_before_utc > now_utc and \
       (interview_datetime_spb.date() - send_date_1day_before_spb).days == 1 and \
       not interview_time_is_20h_or_later:
        reminders_to_add.append(
            InterviewReminder(
                dialogue_id=dialogue_id,
                recruiter_id=recruiter_id,
                interview_datetime_utc=interview_datetime_utc,
                scheduled_send_time_utc=send_1day_before_utc,
                notification_type='1_day_before_20h_spb'
            )
        )

    # Напоминание: В 9:00 утра в день собеседования (если собес после 12:00)
    send_date_day_of_spb = interview_datetime_spb.date()
    send_day_of_9h_spb = datetime.datetime.combine(send_date_day_of_spb, datetime.time(9, 0)).replace(tzinfo=SPB_TIMEZONE)
    send_day_of_9h_utc = send_day_of_9h_spb.astimezone(datetime.timezone.utc)
    
    interview_time_is_after_12h = interview_datetime_spb.time() >= datetime.time(12, 0)

    if send_day_of_9h_utc > now_utc and interview_time_is_after_12h:
        reminders_to_add.append(
            InterviewReminder(
                dialogue_id=dialogue_id,
                recruiter_id=recruiter_id,
                interview_datetime_utc=interview_datetime_utc,
                scheduled_send_time_utc=send_day_of_9h_utc,
                notification_type='day_of_9h_spb'
            )
        )

    if reminders_to_add:
        db_session.add_all(reminders_to_add)

    # =================================================================================
    # ЧАСТЬ 2: FOLLOW-UP "ПОСЛЕ" СОБЕСЕДОВАНИЯ (Новая логика)
    # =================================================================================
    
    # 1. Отменяем старые pending follow-up (на случай переноса даты)
    await db_session.execute(
        update(InterviewFollowup)
        .where(InterviewFollowup.dialogue_id == dialogue_id)
        .where(InterviewFollowup.status == 'pending')
        .values(status='cancelled', processed_at=datetime.datetime.now(datetime.timezone.utc))
    )

    # 2. Рассчитываем время для ШАГА 1
    # Логика: Вечер того же дня (19:00). 
    # Если собес назначен на 19:00 или позже -> переносим на следующее утро (11:00).
    
    followup_time_spb = (interview_datetime_spb + datetime.timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    
    followup_time_utc = followup_time_spb.astimezone(datetime.timezone.utc)

    # 3. Создаем запись (только 1 шаг, остальные создадутся по цепочке)
    # Только если это время в будущем (или совсем немного в прошлом, что допустимо для воркера)
    # Но для надежности планируем всегда, воркер разберется.
    
    # new_followup = InterviewFollowup(
    #     dialogue_id=dialogue_id,
    #     step=1,
    #     scheduled_time_utc=followup_time_utc,
    #     status='pending'
    # )
    # db_session.add(new_followup)
    
    logger.info(f"Запланирован Follow-up (шаг 1) для {hh_response_id} на {followup_time_utc.isoformat()} (UTC).")

    # Коммит изменений произойдет во внешнем коде (hh_worker), который вызвал эту функцию