#run_reminders_worker.py

import asyncio
import argparse
import time
import logging
import json
import random
from hr_bot.services import hh_api_real
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
import datetime
from sqlalchemy.orm import selectinload
from sqlalchemy.orm import selectinload
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from decimal import Decimal
import difflib
import re
from hr_bot.utils.analytics import log_analytics_event
from hr_bot.utils.sentry_config import init_sentry
from hr_bot.utils.redis_lock import close_redis 
from hr_bot.utils.logger_config import setup_logging
from hr_bot.db.models import SessionLocal, Dialogue, Candidate, Vacancy, NotificationQueue, TrackedRecruiter, AppSettings, InactiveNotificationQueue, RejectedNotificationQueue, InterviewReminder, LlmUsageLog, InterviewFollowup
from hr_bot.services import hh_api_real as hh_api
from hr_bot.services import knowledge_base
from hr_bot.db.models import SessionLocal, Dialogue, Candidate, Vacancy, NotificationQueue, TrackedRecruiter, AppSettings, InactiveNotificationQueue, RejectedNotificationQueue, InterviewReminder, LlmUsageLog, InterviewFollowup, MailingTask
from hr_bot.services import llm_handler
from hr_bot.db import statistics_manager
from hr_bot.utils.metrics import start_metrics_server, MESSAGES_SENT_TOTAL
from hr_bot.utils.pii_masker import extract_and_mask_pii
from hr_bot.utils.system_notifier import send_system_alert
from hr_bot.utils.resh_in_code import check_candidate_eligibility, is_candidate_profile_complete
import signal
import sys
from hr_bot.services.llm_handler import cleanup
from hr_bot.utils.system_notifier import send_system_alert
from sqlalchemy import func, select, delete, update  
from hr_bot.services import interview_reminder_manager
from sqlalchemy import func, select, delete, and_, case, literal # <--- Добавьте case и literal
# ... остальные импорты

logger = logging.getLogger(__name__)

#CUTOFF_DATE_FOR_RESPONSES = datetime.datetime(2025, 11, 13, 11, 0, 0, tzinfo=datetime.timezone.utc)
#CUTOFF_DATE_FOR_RESPONSES = datetime.datetime(2025, 11, 16, 13, 56, 0, tzinfo=datetime.timezone.utc)
# --- КОНФИГУРАЦИЯ ---
DEBOUNCE_DELAY_SECONDS = 0
CYCLE_PAUSE_SECONDS = 1
TEST_NEGOTIATION_ID = None # Установите в None для боевого режима
MAX_CONCURRENT_RECRUITERS = 10 #одновременно рекрутеров
MAX_CONCURRENT_DIALOGUES = 40 #одновременно диалогов
VACANCY_CACHE_DURATION_MINUTES = 2 # Время кэширования списка вакансий для рекрутера
# Новые константы для окна доставки напоминаний (местное время сервера)
REMINDER_START_HOUR_LOCAL = 9  # Например, 9:00 утра
REMINDER_END_HOUR_LOCAL = 20 # Например, 20:00 вечера (напоминания отправляются до 19:59 включительно)

PRICE_PER_MILLION_INPUT_TOKENS = 0.150  # $0.150 за 1M входных токенов (gpt-4o-mini)
PRICE_PER_MILLION_OUTPUT_TOKENS = 0.600 # $0.600 за 1M выходных токенов (gpt-4o-mini)

# ВОТ ЭТУ СТРОКУ НУЖНО ДОБАВИТЬ:
shutdown_requested = False

try:
    SPB_TIMEZONE = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    logger.critical("Часовой пояс 'Europe/Moscow' не найден. Убедитесь, что система имеет актуальную базу данных часовых поясов (tzdata).")
    SPB_TIMEZONE = None # Fallback

def signal_handler(sig, frame):
    """Обработчик сигналов для graceful shutdown"""
    global shutdown_requested
    logger.info("Получен сигнал остановки. Завершаем работу...")
    shutdown_requested = True


async def _process_single_reminder_task(dialogue_id: int, recruiter_id: int, semaphore: asyncio.Semaphore):
    """
    Обрабатывает напоминание для одного диалога в изолированной сессии.
    """
    async with semaphore:
        async with SessionLocal() as db:
            log_context = {"dialogue_id": dialogue_id, "recruiter_id": recruiter_id, "worker": "reminders"}
            ctx_logger = logging.LoggerAdapter(logger, log_context)
            try:
                # Загружаем диалог со всеми связями
                stmt = (
                    select(Dialogue)
                    .filter_by(id=dialogue_id)
                    .options(
                        selectinload(Dialogue.candidate),
                        selectinload(Dialogue.vacancy),
                        selectinload(Dialogue.inactive_alerts)
                    )
                    .with_for_update(skip_locked=True)
                )
                
                result = await db.execute(stmt)
                dialogue = result.scalar_one_or_none()

                if not dialogue:
                    # Диалог занят или не найден
                    return

                

                recruiter = await db.get(TrackedRecruiter, recruiter_id)

                if not dialogue or not recruiter:
                    return
                log_context.update({
                    "hh_response_id": dialogue.hh_response_id,
                    "vacancy_id": dialogue.vacancy_id,
                    "recruiter_name": recruiter.name,
                    "worker": "reminders"
                })
                ctx_logger = logging.LoggerAdapter(logger, log_context)

                # Проверка: если диалог уже не in_progress (мог измениться параллельно), выходим
                EXCLUDED_REMINDER_STATUSES = ['declined_interview', 'declined_vacancy', 'call_later', 'refusal']
                if (dialogue.status not in ['in_progress'] or
                    dialogue.dialogue_state in EXCLUDED_REMINDER_STATUSES or
                    dialogue.reminder_level >= 6): # Теперь до 6 уровня включительно
                    return

                now = datetime.datetime.now(datetime.timezone.utc)
                dialogue_hh_id = dialogue.hh_response_id

                # --- API ЗАПРОС (самое долгое место) ---
                current_folder_on_hh = await hh_api.get_negotiation_current_folder(
                    recruiter, db, dialogue_hh_id
                )

                # Логика обработки папки
                if current_folder_on_hh is None:
                    # Отклик удален или не найден
                    return
                elif current_folder_on_hh == 404:
                    # Вакансия закрыта
                    ctx_logger.info(f"Отклик не найден (404) {dialogue_hh_id}")
                    dialogue.status = 'not_found'
                    dialogue.reminder_level = 6
                    await db.commit()
                    return

                elif current_folder_on_hh != 'consider':
                    # Кандидат перемещен вручную рекрутером
                    ctx_logger.info(f"Диалог {dialogue_hh_id} перемещен в '{current_folder_on_hh}'. Отключаю напоминания.")
                    dialogue.status = 'recruiter_handled'
                    dialogue.reminder_level = 3

                    if dialogue.inactive_alerts and dialogue.inactive_alerts.status == 'pending':
                        dialogue.inactive_alerts.status = 'cancelled'
                        dialogue.inactive_alerts.processed_at = now

                    await db.commit()
                    return

                # Логика времени
                dialogue_last_updated = dialogue.last_updated or dialogue.created_at
                time_since_update = now - dialogue_last_updated

                reminder_messages = []
                next_level = None
                should_timeout = False

                # Определение действия
                if dialogue.reminder_level == 0 and time_since_update > datetime.timedelta(minutes=30):
                    reminder_messages = [
                        "Напишу вам ещё раз, вдруг моё прошлое сообщение затерялось где-то между делами:-). ",
                        "Вакансия интересна или что-то смутило? Если что-то смущает, попробую разъяснить спорные моменты и подобрать для вас варианты ."
                    ]
                    next_level = 1

                elif dialogue.reminder_level == 1 and time_since_update > datetime.timedelta(minutes=60):
                    reminder_messages = [
                        "Пишу вам ещё раз, вдруг не увидели предыдущее сообщение. Если вам сейчас неудобно или вы думаете -  напишите, пожалуйста, чтобы я понимала, как лучше вам помочь."
                    ]
                    next_level = 2

                elif dialogue.reminder_level == 2 and time_since_update > datetime.timedelta(minutes=30):
                    should_timeout = True

                # --- НОВЫЕ УРОВНИ ---
                # elif dialogue.reminder_level == 3 and time_since_update > datetime.timedelta(days=7):
                #     reminder_messages = ["Добрый день. Если вы еще находитесь в поиске работы, то будем рады пригласить вас пройти собеседование. Готовы продолжить диалог?"]
                #     next_level = 4

                # elif dialogue.reminder_level == 4 and time_since_update > datetime.timedelta(days=21):
                #     reminder_messages = ["Добрый день. Вы трудоустроились? Если еще рассматриваете варианты, будем рады предложить вам пройти собеседование. А так же ответить на все вопросы, которые у вас есть. "]
                #     next_level = 5

                # elif dialogue.reminder_level == 5 and time_since_update > datetime.timedelta(days=51):
                #     reminder_messages = ["Еще раз добрый день. Как ваши дела? Хотели бы сообщить вам, что вакансия вновь актуальна и если вы в поиске или задумываетесь о смене работы, мы с удовольствием пригласили бы вас на собеседование"]
                #     next_level = 6

                # Выполнение действия
                if should_timeout:
                    # ТВОЕ ТРЕБОВАНИЕ: Если запись уже есть, ничего не делаем с таблицей молчунов
                    if not dialogue.inactive_alerts:
                        db.add(InactiveNotificationQueue(dialogue_id=dialogue.id, status='pending'))
                        ctx_logger.info(f"Диалог {dialogue_hh_id} впервые добавлен в InactiveNotificationQueue.")
                    else:
                        ctx_logger.debug(f"Диалог {dialogue_hh_id} уже зафиксирован в таблице молчунов. Повторная запись не требуется.")

                    # Но статус самого диалога и уровень напоминания обновляем в любом случае,
                    # чтобы пошел отсчет 7 дней для уровня 4.
                    # --- [ANALYTICS] МОЛЧУН В РЕАКТИВАЦИИ ---
                    if dialogue.current_mode == 'reactivation':
                        await log_analytics_event(db, dialogue, 'timed_out')
                    # ----------------------------------------
                    dialogue.status = 'timed_out'
                    dialogue.reminder_level = 3
                    dialogue.last_updated = now
                    await db.commit()

                elif reminder_messages:
                    ctx_logger.info(
                        f"Отправка напоминания уровня {next_level} для диалога {dialogue_hh_id}."
                    )

                    # 1. Определяем типы напоминаний
                    is_long_reminder = next_level in [4, 5, 6]
                    # ТВОЕ ТРЕБОВАНИЕ: Списываем деньги только один раз (при переходе на 4 уровень)
                    should_charge = (next_level == 4) 
                    
                    settings = None

                    # 2. Проверяем баланс только если это ПЕРВОЕ долгое напоминание
                    if should_charge:
                        # Добавляем .with_for_update()
                        settings_res = await db.execute(
                            select(AppSettings).filter_by(id=1).with_for_update()
                        )
                        settings = settings_res.scalar_one_or_none()
                        
                        if not settings or settings.balance < settings.cost_per_long_reminder:
                            ctx_logger.warning(f"Баланс пуст. Первое долгое напоминание для {dialogue_hh_id} отменено.")
                            return 

                    all_sent = True
                    for msg in reminder_messages:
                        status_code = await hh_api.send_message(recruiter, db, dialogue_hh_id, msg)

                        if status_code == 200:
                            # Записываем сообщение в историю
                            new_history_entry = {
                                'role': 'assistant', 
                                'content': msg,
                                'timestamp_msk': datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK')
                            }
                            current_history = list(dialogue.history) if dialogue.history else []
                            current_history.append(new_history_entry)

                            # Добавляем системную команду (для всех уровней 4, 5, 6)
                            if is_long_reminder:
                                system_instruction = {
                                    'role': 'user',
                                    'content': (
                                        "[SYSTEM COMMAND] если кандидат ответит после этого сообщения, то ты должен "
                                        "продолжить диалог по плану разговора, опираясь на текущее состояние (state), "
                                        "и не забывай перед переходом к анкете спросить про вопросы и ответить на них!"
                                    )
                                }
                                current_history.append(system_instruction)
                            
                            dialogue.history = current_history[-150:]

                            # 3. СПИСЫВАЕМ ДЕНЬГИ (только если это уровень 4)
                            # 3. СПИСЫВАЕМ ДЕНЬГИ + СТАТИСТИКА
                            if should_charge and settings:
                                cost = settings.cost_per_long_reminder
                                settings.balance -= cost
                                settings.total_spent_on_reminders += cost # Увеличиваем счетчик напоминалок
                                ctx_logger.info(
                                    f"ЕДИНОВРЕМЕННОЕ СПИСАНИЕ: {cost} руб. Всего на дожимы потрачено: {settings.total_spent_on_reminders}"
                                    
                                )
                        elif status_code == 403:
                             # Вакансия закрыта или доступ запрещен
                             dialogue.reminder_level = 6
                             dialogue.status = 'vacancy_closed_or_resume_not_found'
                             await db.commit()
                             all_sent = False
                             break # Прерываем цикл
                        else:
                            all_sent = False # Ошибка отправки

                    # Если не было критической ошибки (403), обновляем уровень и время
                    if all_sent or status_code == 200:
                        dialogue.reminder_level = next_level
                        MESSAGES_SENT_TOTAL.inc()
                        dialogue.last_updated = now
                        await db.commit()

            except Exception as e:
                ctx_logger.error(f"Ошибка в задаче напоминания для диалога {dialogue_id}: {e}")
                # Не рейзим ошибку, чтобы не поломать gather


async def process_reminders(recruiter_id: int, db: AsyncSession):
    """
    Этап 4: Параллельная отправка напоминаний.
    Аргумент db здесь используется только для получения списка ID,
    далее каждый таск создает свою сессию.
    """
    function_start_time = time.monotonic()

    # Семафор для ограничения одновременных проверок напоминаний (чтобы не убить базу)
    # Можно использовать тот же MAX_CONCURRENT_DIALOGUES или создать свой
    REMINDER_SEMAPHORE = asyncio.Semaphore(20)
    rec_logger = logging.LoggerAdapter(logger, {"recruiter_id": recruiter_id, "worker": "reminders"})
    try:
        # 1. Проверка времени (быстро)
        if SPB_TIMEZONE is None:
            return

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        current_time_spb = now_utc.astimezone(SPB_TIMEZONE)

        if not (REMINDER_START_HOUR_LOCAL <= current_time_spb.hour < REMINDER_END_HOUR_LOCAL):
            # Вне рабочего времени просто выходим, не нагружая базу
            return

        # 2. Быстрая выборка ТОЛЬКО ID кандидатов, которым (возможно) нужны напоминания
        # Мы не грузим здесь объекты целиком, только ID
        EXCLUDED_REMINDER_STATUSES = ['declined_vacancy', 'declined_interview', 'call_later', 'refusal']

        result = await db.execute(
            select(Dialogue.id)
            .filter(
                Dialogue.recruiter_id == recruiter_id,
                # ИЗМЕНЕНИЕ: Разрешаем обработку и для тех, кто уже в статусе timed_out,
                # но еще не достиг финального уровня напоминаний
                Dialogue.status.in_(['in_progress']), 

                Dialogue.dialogue_state.notin_(EXCLUDED_REMINDER_STATUSES),
                Dialogue.reminder_level < 6 # 6 — это будет последнее напоминание (21 день)
            )
        )
        candidate_ids_to_check = result.scalars().all()

        if not candidate_ids_to_check:
            return

        rec_logger.debug(f"Запуск параллельной проверки напоминаний для {len(candidate_ids_to_check)} диалогов...")

        # 3. Создаем задачи для параллельного выполнения
        tasks = [
            _process_single_reminder_task(d_id, recruiter_id, REMINDER_SEMAPHORE)
            for d_id in candidate_ids_to_check
        ]

        # 4. Запускаем и ждем выполнения (return_exceptions=True чтобы ошибка в одном не крашила всё)
        await asyncio.gather(*tasks, return_exceptions=True)

    except Exception as e:
        rec_logger.error(f"Ошибка в process_reminders (диспетчер): {e}", exc_info=True)
    finally:
        rec_logger.debug(
            f"[Recruiter ID {recruiter_id}] "
            f"process_reminders finished in {time.monotonic() - function_start_time:.2f}s"
        )

async def check_and_send_interview_reminders():
    """
    Фоновая задача, которая проверяет очередь InterviewReminder
    и рассылает запланированные уведомления кандидатам на HH.ru.
    """
    logger.info("Фоновый обработчик напоминаний о собеседованиях запущен.")

    # Шаблоны сообщений (это черновик, нужны будут точные тексты)
    MESSAGE_TEMPLATES = {
        '2_hours_before': (
            "Здравствуйте! Напоминаю, что у вас запланировано собеседование по вакансии "
            "'{vacancy_title}' сегодня в {interview_time_spb} по московскому времени. "
            "Пожалуйста, будьте готовы."
        ),
        '1_day_before_20h_spb': (
            "Добрый вечер! Напоминаю, что завтра, {interview_date_spb} в {interview_time_spb} "
            "по московскому времени, у вас назначено собеседование по вакансии '{vacancy_title}'. "
            "Если у вас есть вопросы, напишите нам."
        ),
        'day_of_9h_spb': (
            "Доброе утро! Сегодня, {interview_date_spb} в {interview_time_spb} "
            "по московскому времени, состоится ваше собеседование по вакансии '{vacancy_title}'. "
            "Будем ждать вас!"
        )
    }

    # Определяем часовой пояс Санкт-Петербурга
    try:
        SPB_TIMEZONE = ZoneInfo("Europe/Moscow")
    except ZoneInfoNotFoundError: # <--- Нужно импортировать ZoneInfoNotFoundError в hh_worker/main.py тоже
        logger.critical("Часовой пояс 'Europe/Moscow' не найден. Напоминания не будут отправляться.")
        SPB_TIMEZONE = None
        return

    while True:
        if shutdown_requested:
            logger.info("Задача отправки напоминаний о собеседованиях остановлена из-за запроса на завершение работы.")
            break

        if SPB_TIMEZONE is None:
            await asyncio.sleep(60) # Ждем, если часовой пояс не определен
            continue

        async with SessionLocal() as db_session:
            try:
                now_utc = datetime.datetime.now(datetime.timezone.utc)

                # Выбираем напоминания, которые пора отправить
                # Используем selectinload для всех необходимых связей
                stmt = (
                    select(InterviewReminder)
                    .options(
                        selectinload(InterviewReminder.dialogue)
                        .selectinload(Dialogue.vacancy),
                        selectinload(InterviewReminder.dialogue)
                        .selectinload(Dialogue.candidate),
                        selectinload(InterviewReminder.recruiter) 
                    )
                    .filter(
                        InterviewReminder.status == 'pending',
                        InterviewReminder.scheduled_send_time_utc <= now_utc
                    )
                    .limit(20)
                    .with_for_update(skip_locked=True) # <--- ВАЖНО: Блокируем строки
                )
                
                result = await db_session.execute(stmt)
                reminders_to_send = result.scalars().all()

                if not reminders_to_send:
                    # logger.debug("[Interview Reminders] Нет ожидающих напоминаний. Пауза.")
                    await asyncio.sleep(30) # Пауза, если нет задач
                    continue

                logger.info(f"[Interview Reminders] Найдено {len(reminders_to_send)} напоминаний для отправки.")

                for reminder in reminders_to_send:
                    try:
                        dialogue = reminder.dialogue
                        recruiter = reminder.recruiter # Объект рекрутера уже загружен
                        vacancy = dialogue.vacancy
                        candidate = dialogue.candidate

                        if not dialogue or not recruiter or not vacancy or not candidate:
                            logger.error(
                                f"Не удалось загрузить связанные объекты для напоминания {reminder.id}. "
                                f"Dialogue: {bool(dialogue)}, Recruiter: {bool(recruiter)}, "
                                f"Vacancy: {bool(vacancy)}, Candidate: {bool(candidate)}"
                            )
                            reminder.status = 'error'
                            reminder.processed_at = now_utc
                            await db_session.commit()
                            continue
                        ctx_logger = logging.LoggerAdapter(logger, {
                            "dialogue_id": dialogue.id,
                            "recruiter_id": recruiter.id,
                            "recruiter_name": recruiter.name,
                            "hh_response_id": dialogue.hh_response_id,
                            "vacancy_id": vacancy.id,
                            "candidate_id": candidate.id,
                            "notification_type": reminder.notification_type,
                            "worker": "interview_reminders"
                        })
                        if not recruiter.access_token:
                            ctx_logger.error(f"У рекрутера {recruiter.name} (ID: {recruiter.id}) нет access_token. Не могу отправить напоминание {reminder.id}.")
                            reminder.status = 'error'
                            reminder.processed_at = now_utc
                            await db_session.commit()
                            continue

                        
                        # Форматируем дату и время собеседования для сообщения
                        interview_datetime_spb = reminder.interview_datetime_utc.astimezone(SPB_TIMEZONE)
                        interview_date_spb = interview_datetime_spb.strftime("%d.%m.%Y")
                        interview_time_spb = interview_datetime_spb.strftime("%H:%M")

                        # Получаем шаблон сообщения
                        template = MESSAGE_TEMPLATES.get(reminder.notification_type)
                        if not template:
                            ctx_logger.error(f"Не найден шаблон сообщения для типа уведомления '{reminder.notification_type}'. Напоминание {reminder.id} не будет отправлено.")
                            reminder.status = 'error'
                            reminder.processed_at = now_utc
                            await db_session.commit()
                            continue

                        message_text = template.format(
                            vacancy_title=vacancy.title,
                            candidate_full_name=candidate.full_name, # Можно использовать, если нужно
                            interview_date_spb=interview_date_spb,
                            interview_time_spb=interview_time_spb
                        )

                        ctx_logger.info(f"Отправка напоминания типа '{reminder.notification_type}' для диалога {dialogue.hh_response_id} от рекрутера {recruiter.name}.")

                        # Отправка сообщения кандидату через HH API
                        # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
                        send_result = await hh_api.send_message(
                            recruiter=recruiter,
                            db=db_session,
                            negotiation_id=dialogue.hh_response_id,
                            message_text=message_text
                        )

                        if send_result == 200:
                            # УСПЕХ
                            reminder.status = 'sent'

                            MESSAGES_SENT_TOTAL.inc() # <--- МЕТРИКА
                            ctx_logger.info(
                                f"Напоминание {reminder.id} успешно отправлено кандидату {candidate.full_name}."
                                
                            )

                        elif send_result == 403:
                            # ВАКАНСИЯ ЗАКРЫТА
                            reminder.status = 'cancelled' # Отменяем, так как отправлять бессмысленно
                            dialogue.status = 'vacancy_closed_or_resume_not_found'
                            ctx_logger.warning(
                                f"Напоминание {reminder.id} ОТМЕНЕНО: Вакансия закрыта/в архиве. "
                                f"Кандидат: {candidate.full_name}, Диалог: {dialogue.hh_response_id}"
                            )

                        else:
                            # ПРОЧИЕ ОШИБКИ (False или другие коды)
                            reminder.status = 'error'
                            ctx_logger.error(
                                f"Не удалось отправить напоминание {reminder.id} кандидату {candidate.full_name} (API Error)."
                                
                            )
                        # -----------------------

                        reminder.processed_at = now_utc
                        await db_session.commit() # Коммитим каждое напоминание отдельно для надежности

                    except Exception as e:
                        err_logger = locals().get('ctx_logger', logger)
                        err_logger.error(f"Ошибка при обработке напоминания {reminder.id}: {e}", exc_info=True)
                        if reminder.id: # Убедимся, что reminder объект существует
                            reminder.status = 'error'
                            reminder.processed_at = now_utc
                            await db_session.commit() # Попытаемся сохранить статус ошибки
                        else:
                            await db_session.rollback() # Откатываем, если ошибка до создания reminder
            except Exception as e:
                logger.critical(f"Критическая ошибка в фоновом обработчике напоминаний о собеседованиях: {e}", exc_info=True)
                await db_session.rollback() # Откат при ошибке верхнего уровня

        await asyncio.sleep(30) # Пауза между циклами проверки



async def check_and_send_followups():
    """
    Фоновая задача для отправки пост-собеседовательных сообщений (Follow-up).
    Обрабатывает цепочку:
    Step 1: Первое сообщение ("Как прошло?") -> Списание 10р -> Смена стейта диалога.
    Step 2: Напоминание через 1 час.
    Step 3: Напоминание на следующий день -> Если молчит, закрытие диалога.
    """
    logger.info("Фоновый обработчик Follow-up сообщений запущен.")

    # Тексты сообщений
    FOLLOWUP_TEMPLATES = {
        1: "Добрый день! Подскажите, пожалуйста, у Вас получилось подойти на собеседование? Как всё прошло?",
        2: "Напомнюсь на всякий случай 🙂 Подскажите, пожалуйста, удалось ли Вам сегодня подойти на собеседование?",
        3: "Добрый день! Вы так и не ответили. Хотела уточнить, удалось ли Вам вчера подойти на собеседование и как всё прошло?"
    }

    try:
        SPB_TIMEZONE = ZoneInfo("Europe/Moscow")
    except ZoneInfoNotFoundError:
        logger.critical("Часовой пояс не найден. Follow-up не будет работать корректно.")
        return

    while True:
        if shutdown_requested:
            break

        async with SessionLocal() as db_session:
            try:
                now_utc = datetime.datetime.now(datetime.timezone.utc)

                # Выбираем задачи с блокировкой строки (skip_locked), чтобы воркеры не мешали друг другу
                stmt = (
                    select(InterviewFollowup)
                    .options(
                        selectinload(InterviewFollowup.dialogue)
                        .selectinload(Dialogue.recruiter),
                        selectinload(InterviewFollowup.dialogue)
                        .selectinload(Dialogue.inactive_alerts)
                    )
                    .filter(
                        InterviewFollowup.status == 'pending',
                        InterviewFollowup.scheduled_time_utc <= now_utc
                    )
                    .limit(20)
                    .with_for_update(skip_locked=True)
                )

                result = await db_session.execute(stmt)
                tasks = result.scalars().all()

                if not tasks:
                    await asyncio.sleep(30)
                    continue

                logger.info(f"[Follow-up] Найдено {len(tasks)} задач для отправки.")

                for task in tasks:
                    dialogue = task.dialogue
                    if not dialogue or not dialogue.recruiter:
                        task.status = 'error'
                        task.processed_at = now_utc
                        await db_session.commit()
                        continue

                    recruiter = dialogue.recruiter
                    step = task.step
                    message_text = FOLLOWUP_TEMPLATES.get(step)
                    
                    ctx_logger = logging.LoggerAdapter(logger, {
                        "dialogue_id": dialogue.id,
                        "step": step,
                        "hh_id": dialogue.hh_response_id,
                        "worker": "followup_sender"
                    })

                    # === ЛОГИКА ШАГА 1 (ОПЛАТА И СМЕНА СТАТУСА) ===
                    if step == 1:
                        # 1. БЛОКИРУЕМ настройки для атомарной проверки и списания
                        settings_res = await db_session.execute(
                            select(AppSettings).filter_by(id=1).with_for_update()
                        )
                        settings = settings_res.scalar_one_or_none()
                        
                        cost = settings.cost_per_followup if settings else Decimal('10.00')

                        # Проверка баланса ДО отправки
                        if not settings or settings.balance < cost:
                            ctx_logger.warning(f"Недостаточно средств ({settings.balance if settings else 0}) для Follow-up. Отмена.")
                            task.status = 'cancelled_no_funds'
                            task.processed_at = now_utc
                            await db_session.commit()
                            continue
                        
                        # 2. Отправка сообщения (Блокирующая операция)
                        # Мы удерживаем лок на AppSettings, пока ждем ответа от API. 
                        # Это гарантирует, что баланс не изменится "под ногами".
                        send_result = await hh_api.send_message(recruiter, db_session, dialogue.hh_response_id, message_text)
                        
                        if send_result == 200:
                            # УСПЕХ: Только теперь списываем деньги
                            settings.balance -= cost
                            settings.total_spent_on_followups += cost
                            
                            

                            # История
                            new_hist = {
                                'role': 'assistant',
                                'content': message_text,
                                'timestamp_msk': datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK')
                            }
                            dialogue.history = (list(dialogue.history) if dialogue.history else []) + [new_hist]
                            dialogue.is_followup = True
                            dialogue.was_in_followup = True

                            # Планируем Шаг 2 (через 1 час)
                            next_time_utc = now_utc + datetime.timedelta(hours=1)
                            db_session.add(InterviewFollowup(
                                dialogue_id=dialogue.id,
                                step=2,
                                scheduled_time_utc=next_time_utc,
                                status='pending'
                            ))
                            
                            task.status = 'sent'
                            MESSAGES_SENT_TOTAL.inc()
                            ctx_logger.info(f"Step 1 отправлен. Списано {cost} руб. Запланирован Step 2.")
                        
                        elif send_result == 403:
                            # 403: Вакансия закрыта или резюме удалено. 
                            # Деньги НЕ списываем.
                            task.status = 'cancelled'
                            dialogue.status = 'vacancy_closed_or_resume_not_found'
                            # Можно сбросить уровень напоминаний
                            dialogue.reminder_level = 6 
                            ctx_logger.warning("Step 1 отменен: получен 403 (Вакансия закрыта/Резюме удалено).")

                        else:
                            # Прочие ошибки (сеть и т.д.) - попробуем в следующий раз или пометим как ошибку
                            # Деньги НЕ списываем.
                            task.status = 'error'
                            ctx_logger.error(f"Ошибка отправки сообщения Step 1. Code: {send_result}")

                    # === ЛОГИКА ШАГА 2 (НАПОМИНАНИЕ ЧЕРЕЗ ЧАС) ===
                    elif step == 2:
                        send_result = await hh_api.send_message(recruiter, db_session, dialogue.hh_response_id, message_text)
                        
                        if send_result == 200:
                            new_hist = {
                                'role': 'assistant',
                                'content': message_text,
                                'timestamp_msk': datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK')
                            }
                            dialogue.history = (list(dialogue.history) if dialogue.history else []) + [new_hist]

                            # Планируем Шаг 3 (Завтра в 12:00 по СПб)
                            now_spb = datetime.datetime.now(SPB_TIMEZONE)
                            tomorrow_spb = now_spb.date() + datetime.timedelta(days=1)
                            next_time_spb = datetime.datetime.combine(tomorrow_spb, datetime.time(12, 0)).replace(tzinfo=SPB_TIMEZONE)
                            next_time_utc = next_time_spb.astimezone(datetime.timezone.utc)
                            
                            db_session.add(InterviewFollowup(
                                dialogue_id=dialogue.id,
                                step=3,
                                scheduled_time_utc=next_time_utc,
                                status='pending'
                            ))
                            
                            task.status = 'sent'
                            MESSAGES_SENT_TOTAL.inc()
                            ctx_logger.info("Step 2 отправлен. Запланирован Step 3.")

                        elif send_result == 403:
                            task.status = 'cancelled'
                            dialogue.status = 'vacancy_closed_or_resume_not_found'
                            ctx_logger.warning("Step 2 отменен: получен 403.")
                        
                        else:
                            task.status = 'error'

                    # === ЛОГИКА ШАГА 3 (ФИНАЛЬНОЕ НАПОМИНАНИЕ) ===
                    elif step == 3:
                        send_result = await hh_api.send_message(recruiter, db_session, dialogue.hh_response_id, message_text)
                        
                        if send_result == 200:
                            new_hist = {
                                'role': 'assistant',
                                'content': message_text,
                                'timestamp_msk': datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK')
                            }
                            dialogue.history = (list(dialogue.history) if dialogue.history else []) + [new_hist]
                            
                            # Финализируем диалог
                            dialogue.interview_result = 'no_answer_after_interview'
                            
                            
                            task.status = 'sent'
                            MESSAGES_SENT_TOTAL.inc()
                            ctx_logger.info("Step 3 отправлен. Диалог помечен как 'no_answer_after_interview'.")

                        elif send_result == 403:
                            task.status = 'cancelled'
                            dialogue.status = 'vacancy_closed_or_resume_not_found'
                            ctx_logger.warning("Step 3 отменен: получен 403.")

                        else:
                            task.status = 'error'
                    
                    # Сохраняем результат обработки конкретной задачи
                    task.processed_at = now_utc
                    await db_session.commit()

            except Exception as e:
                logger.error(f"Ошибка в цикле Follow-up: {e}", exc_info=True)
                await db_session.rollback()

        await asyncio.sleep(30)


async def run_reminders_cycle(target_recruiter_ids=None):
    """Цикл проверки напоминаний (дожимов) с фильтрацией по рекрутерам."""
    start_time = time.monotonic()
    
    async with SessionLocal() as db:
        query = select(TrackedRecruiter.id).filter(TrackedRecruiter.is_auth_active == True)
        # --- ИЗМЕНЕНИЕ: Фильтрация ---
        if target_recruiter_ids:
            query = query.filter(TrackedRecruiter.id.in_(target_recruiter_ids))
        # -----------------------------
        result = await db.execute(query)
        recruiter_ids = result.scalars().all()
    
    if not recruiter_ids:
        logger.debug("Нет рекрутеров для проверки напоминаний.")
        return

    # Запускаем process_reminders для каждого рекрутера
    for r_id in recruiter_ids:
        async with SessionLocal() as db_session:
            await process_reminders(r_id, db_session)
            await db_session.commit()
    
    logger.debug(f"Цикл 'дожимов' завершен за {time.monotonic() - start_time:.2f} сек. (Рекрутеров: {len(recruiter_ids)})")




async def process_mailing_tasks():
    """
    Фоновая задача для обработки массовых рассылок (MailingTask).
    Берет задачи со статусом 'pending', ищет подходящие диалоги и отправляет сообщения.
    """
    logger.info("Запущен обработчик задач рассылки (MailingTask).")

    while True:
        if shutdown_requested:
            break

        async with SessionLocal() as db:
            try:
                # 1. Ищем одну задачу в статусе pending (блокируем строку, чтобы другие воркеры не взяли)
                stmt = (
                    select(MailingTask)
                    .filter_by(status='pending')
                    .order_by(MailingTask.created_at.asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                result = await db.execute(stmt)
                task = result.scalar_one_or_none()

                if not task:
                    # Нет задач, спим подольше
                    await asyncio.sleep(10)
                    continue

                # 2. Меняем статус на in_progress
                logger.info(f"[MailingTask #{task.id}] Задача взята в работу.")
                task.status = 'in_progress'
                task.processed_at = datetime.datetime.now(datetime.timezone.utc)
                await db.commit() # Коммитим смену статуса, чтобы задача считалась занятой
                
                # --- НАЧАЛО ОБРАБОТКИ ЗАДАЧИ ---
                
                # 3. Формируем запрос для поиска диалогов
                # Нам нужны:
                # - Статус диалога: timed_out (молчуны)
                # - Статус активности: НЕ в архиве (не vacancy_closed_or_resume_not_found)
                # - Вакансия: совпадение по паттерну (если есть)
                # - Город: совпадение по паттерну (если есть)
                # - Время: last_updated в диапазоне [NOW - max_days, NOW - min_days]
                
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                
                # Вычисляем временной диапазон.
                # Если человек молчит MIN дней, значит последнее обновление было <= (NOW - min)
                # Если человек молчит MAX дней, значит последнее обновление было >= (NOW - max)
                cutoff_recent = now_utc - datetime.timedelta(days=task.days_silent_min)
                cutoff_oldest = now_utc - datetime.timedelta(days=task.days_silent_max)

                # Строим запрос
                query = (
                    select(Dialogue)
                    .join(Dialogue.vacancy)
                    .options(
                        selectinload(Dialogue.recruiter),
                        selectinload(Dialogue.vacancy)
                    )
                    .filter(
                        Dialogue.status == 'timed_out', # Строго молчуны
                        Dialogue.dialogue_state.notin_(['vacancy_closed_or_resume_not_found', 'refusal']),
                        # Фильтр по дате: старее чем min_days, но новее чем max_days
                        Dialogue.last_updated <= cutoff_recent,
                        Dialogue.last_updated >= cutoff_oldest
                    )
                )

                # Применяем фильтры по тексту (если заданы)
                if task.vacancy_pattern:
                    query = query.filter(Vacancy.title.ilike(f"%{task.vacancy_pattern}%"))
                
                if task.city_pattern:
                    query = query.filter(Vacancy.city.ilike(f"%{task.city_pattern}%"))

                # Выполняем запрос
                dialogues_result = await db.execute(query)
                dialogues_to_process = dialogues_result.scalars().all()
                
                count_found = len(dialogues_to_process)
                logger.info(f"[MailingTask #{task.id}] Найдено {count_found} диалогов для рассылки.")
                
                # Обновляем счетчик найденных (нужна новая транзакция или через refresh)
                # Проще обновить объект task, так как он привязан к сессии
                task.candidates_found = count_found
                await db.commit()

                messages_sent_count = 0
                total_spent = Decimal('0.00')

                # 4. Цикл отправки по каждому диалогу
                for dialogue in dialogues_to_process:
                    if shutdown_requested:
                        break

                    # Используем отдельный блок try/except для каждого сообщения, чтобы не завалить всю задачу
                    try:
                        recruiter = dialogue.recruiter
                        
                        # --- БЛОК ОПЛАТЫ ---
                        # Получаем настройки и блокируем для атомарного списания
                        settings_stmt = select(AppSettings).filter_by(id=1).with_for_update()
                        settings_res = await db.execute(settings_stmt)
                        settings = settings_res.scalar_one_or_none()
                        
                        # Цена за "долгий" ремайндер/дожим
                        cost = settings.cost_per_long_reminder if settings else Decimal('5.00')
                        
                        if not settings or settings.balance < cost:
                            logger.warning(f"[MailingTask #{task.id}] Недостаточно средств на балансе! Остаток: {settings.balance}. Остановка задачи.")
                            task.status = 'paused_no_funds'
                            await db.commit()
                            break # Прерываем цикл рассылки
                        
                        # --- ОТПРАВКА ---
                        logger.info(f"[MailingTask #{task.id}] Отправка для диалога ID {dialogue.id} (HH: {dialogue.hh_response_id})...")
                        
                        send_status = await hh_api.send_message(
                            recruiter, db, dialogue.hh_response_id, task.message_text
                        )
                        
                        if send_status == 200:
                            # УСПЕХ
                            # 1. Списываем деньги
                            settings.balance -= cost
                            settings.total_spent_on_reminders += cost # Считаем как ремайндеры
                            total_spent += cost

                            # --- [ANALYTICS] СТАРТ РЕАКТИВАЦИИ ---
                            # Переводим диалог в режим реактивации
                            dialogue.current_mode = 'reactivation'
                            dialogue.active_mailing_task_id = task.id
                            
                            # Пишем событие в аналитику
                            await log_analytics_event(db, dialogue, 'reactivation_sent')
                            # -------------------------------------
                            
                            # 2. Обновляем диалог
                            # Добавляем запись в историю
                            new_history_entry = {
                                'role': 'assistant',
                                'content': task.message_text,
                                'timestamp_msk': datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK'),
                                'type': 'mass_mailing'
                            }
                            current_history = list(dialogue.history) if dialogue.history else []
                            current_history.append(new_history_entry)
                            dialogue.history = current_history[-150:]
                            
                            # Обновляем last_updated, чтобы таймер молчания сбросился
                            # (и чтобы мы не отправили этому же человеку сообщение повторно через 5 минут)
                            dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
                            
                            messages_sent_count += 1
                            MESSAGES_SENT_TOTAL.inc()
                            
                            # Небольшая пауза, чтобы не душить API
                            await asyncio.sleep(1.5) 
                            
                        elif send_status == 403:
                            # Вакансия закрыта или доступ запрещен
                            logger.info(f"Диалог {dialogue.hh_response_id} недоступен (403). Помечаем статус.")
                            dialogue.status = 'vacancy_closed_or_resume_not_found'
                            # Деньги НЕ списываем
                            
                        else:
                            logger.error(f"Ошибка отправки API: {send_status}")
                        
                        # Коммитим изменения ПОСЛЕ КАЖДОГО ДИАЛОГА (и деньги, и историю)
                        # Это важно, чтобы при падении воркера не потерять прогресс
                        await db.commit()
                        
                    except Exception as e:
                        logger.error(f"Ошибка при обработке диалога {dialogue.id} в рассылке: {e}")
                        await db.rollback() # Откат текущей транзакции (конкретного диалога)
                        continue

                # --- ЗАВЕРШЕНИЕ ЗАДАЧИ ---
                # Если вышли из цикла не из-за нехватки денег
                if task.status != 'paused_no_funds':
                    task.status = 'completed'
                    task.completed_at = datetime.datetime.now(datetime.timezone.utc)
                    task.messages_sent = messages_sent_count
                    task.total_cost = total_spent
                    await db.commit()
                    logger.info(f"[MailingTask #{task.id}] Задача завершена. Отправлено: {messages_sent_count}, Списано: {total_spent}")

            except Exception as e:
                logger.error(f"Ошибка в процессе обработки MailingTask: {e}", exc_info=True)
                await db.rollback()
                await asyncio.sleep(5) # Пауза перед ретраем, если база лежит

        # Пауза между проверками новых задач
        await asyncio.sleep(10)


# --- ИСПРАВЛЕНИЕ: Убрали лишний отступ у main (вынесли из предыдущей функции) ---
async def main(target_recruiter_ids=None):
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info(f"Reminder Worker запускается (Рекрутеры: {target_recruiter_ids if target_recruiter_ids else 'Все'})...")
    
    # 1. Запускаем фоновую задачу для напоминаний о собеседованиях
    # Она обрабатывает ОБЩУЮ очередь, поэтому фильтрация по рекрутерам ей не обязательна,
    # но благодаря skip_locked воркеры поделят задачи между собой.
    interview_task = asyncio.create_task(check_and_send_interview_reminders())
    followup_task = asyncio.create_task(check_and_send_followups())
    mailing_task = asyncio.create_task(process_mailing_tasks())
    try:
        while not shutdown_requested:
            # 2. Запускаем цикл "дожимов" с фильтром
            await run_reminders_cycle(target_recruiter_ids)
            
            # Пауза между проверками "дожимов"
            for _ in range(600): # 10 минут
                if shutdown_requested:
                    break
                await asyncio.sleep(1)
    finally:
        interview_task.cancel()
        followup_task.cancel()
        mailing_task.cancel()
        await hh_api_real.close_api_client()
        await close_redis()
        logger.info("Reminder Worker остановлен.")

if __name__ == "__main__":
    setup_logging(log_filename="reminders_worker.log")
    load_dotenv()
    init_sentry()
    start_metrics_server(8000)
    
    parser = argparse.ArgumentParser(description="HH Reminders Worker")
    parser.add_argument('--recruiter_ids', nargs='+', type=int, help='List of recruiter IDs')
    args = parser.parse_args()

    try:
        asyncio.run(main(target_recruiter_ids=args.recruiter_ids))
    except (KeyboardInterrupt, SystemExit):
        pass