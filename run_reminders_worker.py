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
from sqlalchemy import update
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


# --- ХЕЛПЕР: Добавление события в лог обработки ---
def add_to_processing_log(task_obj, dialogue_id, hh_id, action, reason=None):
    """Добавляет запись в processing_log задачи рассылки."""
    current_log = task_obj.processing_log if task_obj.processing_log else []
    current_log.append({
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dialogue_id": dialogue_id,
        "hh_response_id": hh_id,
        "action": action,      # "sent", "skipped", "error"
        "reason": reason       # например: "vacancy_closed", "network_error"
    })
    task_obj.processing_log = current_log  # Важно: переназначение, чтобы SQLAlchemy увидел изменение
# ----------------------------------------------------
# --- ХЕЛПЕР: Формирование отчёта по задаче рассылки ---
def format_mailing_report(task_obj) -> str:
    """Формирует читаемый отчёт о завершении задачи рассылки."""
    total = task_obj.candidates_found
    sent = task_obj.messages_sent
    skipped = task_obj.skipped_count
    errors = task_obj.error_count
    processed = task_obj.processed_count
    cost = task_obj.total_cost or 0
    
    # Процент от найденных, которые реально обработали
    progress_pct = round((processed / total * 100) if total > 0 else 0, 1)
    # Процент успешных от обработанных
    success_rate = round((sent / processed * 100) if processed > 0 else 0, 1)
    
    report = (
        f"📬 <b>Рассылка #{task_obj.id} завершена</b>\n"
        f"🔍 Найдено кандидатов: {total}\n"
        f"✅ Обработано: {processed} ({progress_pct}%)\n"
        f"   ├─ Отправлено: {sent} ({success_rate}%)\n"
        f"   ├─ Пропущено: {skipped}\n"
        f"   └─ Ошибки: {errors}\n"
        f"💰 Списано: {cost} руб."
    )
    
    # Добавляем причины пропусков, если есть детализация
    if task_obj.processing_log:
        skip_reasons = {}
        for entry in task_obj.processing_log:
            if entry.get("action") == "skipped" and entry.get("reason"):
                reason = entry["reason"]
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        
        if skip_reasons:
            reasons_str = " | ".join([f"{k}: {v}" for k, v in skip_reasons.items()])
            report += f"\n⚠️ Причины пропусков: {reasons_str}"
    
    return report
# --------------------------------------------------------
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
                # --- НОВАЯ ЛОГИКА: ТАЙМАУТ ДЛЯ РЕАКТИВАЦИИ (РАССЫЛОК) ---
                if dialogue.current_mode == 'reactivation' and dialogue.status == 'in_progress':
                    if time_since_update > datetime.timedelta(hours=3):
                        # Проверяем, что последнее сообщение было от бота (assistant)
                        history = dialogue.history or []
                        # Ищем последнее значимое сообщение (не системную команду)
                        last_message_role = None
                        for msg in reversed(history):
                            if "[SYSTEM COMMAND]" not in msg.get('content', ''):
                                last_message_role = msg.get('role')
                                break
                        
                        if last_message_role == 'assistant':
                            ctx_logger.info(f"Диалог {dialogue_hh_id} (реактивация) молчит > 3ч. Перевод в timed_out.")
                            
                            dialogue.status = 'timed_out'
                            # Сбрасываем режим, чтобы он снова мог попасть под рассылку или обычные дожимы
                            
                            dialogue.last_updated = now
                            
                            await log_analytics_event(db, dialogue, 'timed_out')
                            await db.commit()
                            return # Выходим, так как диалог обработан
                # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

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
                        dialogue.last_message_at = now
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
                            dialogue.last_message_at = now_utc

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
                        if not settings or settings.balance_outbound < cost:
                            ctx_logger.warning(f"Недостаточно средств ({settings.balance if settings else 0}) для Follow-up. Отмена.")
                            task.status = 'cancelled_no_funds'
                            task.processed_at = now_utc
                            await db_session.commit()
                            continue
                        if settings.balance_outbound < settings.low_balance_threshold and not settings.notified_low_outbound:
                            asyncio.create_task(send_system_alert(
                                f"⚠️ Внимание! БАЛАНС РАССЫЛОК (Outbound) ниже {settings.low_balance_threshold} руб. "
                                f"Текущий остаток: {settings.balance_outbound} руб.", alert_type="balance"
                            ))
                            settings.notified_low_outbound = True

                        if settings.balance_outbound >= settings.low_balance_threshold:
                            settings.notified_low_outbound = False

                            
                        # 2. Отправка сообщения (Блокирующая операция)
                        # Мы удерживаем лок на AppSettings, пока ждем ответа от API. 
                        # Это гарантирует, что баланс не изменится "под ногами".
                        send_result = await hh_api.send_message(recruiter, db_session, dialogue.hh_response_id, message_text)
                        
                        if send_result == 200:
                            # УСПЕХ: Только теперь списываем деньги
                            settings.balance_outbound -= cost
                            settings.total_spent_followup += cost
                            
                             # --- [ANALYTICS] СТАРТ FOLLOW-UP ---
                            # --- [ANALYTICS] ВХОД В ВОРОНКУ FOLLOW-UP ---
                            dialogue.current_mode = 'followup' # Переключаем режим!
                            dialogue.last_message_at = now_utc
                            await log_analytics_event(db_session, dialogue, 'followup_sent')
                            # ----------------------------------

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
                            dialogue.last_message_at = now_utc
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
                            await log_analytics_event(db_session, dialogue, 'followup_no_show')
                            
                            task.status = 'sent'
                            dialogue.last_message_at = now_utc
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
    Версия: Высокопроизводительная (PARALLEL BATCH) с фильтрацией живых рекрутеров.
    """
    logger.info("Запущен оптимизированный обработчик задач рассылки (MailingTask).")
    BATCH_SIZE = 50  # Увеличили размер пачки
    CONCURRENT_LIMIT = 5  # Сколько сообщений слать одновременно (чтобы не забанил HH)
    
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)

    while True:
        if shutdown_requested:
            break

        # 1. ПРОВЕРКА ВРЕМЕНИ ТИШИНЫ
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        current_time_spb = now_utc.astimezone(SPB_TIMEZONE)
        if not (REMINDER_START_HOUR_LOCAL <= current_time_spb.hour < REMINDER_END_HOUR_LOCAL):
            await asyncio.sleep(60)
            continue

        async with SessionLocal() as db:
            try:
                # 2. ИЩЕМ ЗАДАЧУ
                stmt = (
                    select(MailingTask)
                    .filter(MailingTask.status.in_(['pending', 'in_progress']))
                    .order_by(case((MailingTask.status == 'in_progress', 1), else_=2), MailingTask.created_at.asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                result = await db.execute(stmt)
                task = result.scalar_one_or_none()

                if not task:
                    await asyncio.sleep(20)
                    continue

                # 3. ИНИЦИАЛИЗАЦИЯ НОВОЙ ЗАДАЧИ (с фильтром по активным рекрутерам)
                if task.status == 'pending':
                    task.status = 'in_progress'
                    task.processed_at = now_utc
                    
                    count_query = (
                        select(func.count(Dialogue.id))
                        .join(Dialogue.vacancy)
                        .join(Dialogue.recruiter) # Присоединяем для фильтрации
                        .filter(
                            Dialogue.status == 'timed_out',
                            Dialogue.dialogue_state.notin_(['vacancy_closed_or_resume_not_found', 'refusal']),
                            Dialogue.last_message_at <= now_utc - datetime.timedelta(days=task.days_silent_min),
                            Dialogue.last_message_at >= now_utc - datetime.timedelta(days=task.days_silent_max),
                            # ФИЛЬТР: Только активные с токенами
                            TrackedRecruiter.is_auth_active == True,
                            TrackedRecruiter.access_token.isnot(None)
                        )
                    )
                    if task.vacancy_pattern:
                        count_query = count_query.filter(Vacancy.title.ilike(f"%{task.vacancy_pattern}%"))
                    if task.city_pattern:
                        count_query = count_query.filter(Vacancy.city.ilike(f"%{task.city_pattern}%"))
                    
                    count_res = await db.execute(count_query)
                    task.candidates_found = count_res.scalar()
                    await db.commit()
                    logger.info(f"[MailingTask #{task.id}] Старт. Найдено активных кандидатов: {task.candidates_found}")

                # 4. ВЫБИРАЕМ ПОРЦИЮ ДИАЛОГОВ (с фильтром по активным рекрутерам)
                cutoff_recent = now_utc - datetime.timedelta(days=task.days_silent_min)
                cutoff_oldest = now_utc - datetime.timedelta(days=task.days_silent_max)

                query = (
                    select(Dialogue)
                    .join(Dialogue.vacancy)
                    .join(Dialogue.recruiter) # Присоединяем рекрутера
                    .options(selectinload(Dialogue.recruiter), selectinload(Dialogue.vacancy))
                    .filter(
                        Dialogue.status == 'timed_out',
                        Dialogue.dialogue_state.notin_(['vacancy_closed_or_resume_not_found', 'refusal']),
                        Dialogue.last_message_at <= cutoff_recent,
                        Dialogue.last_message_at >= cutoff_oldest,
                        (Dialogue.active_mailing_task_id != task.id) | (Dialogue.active_mailing_task_id == None),
                        # ФИЛЬТР: Пропускаем диалоги неактивных рекрутеров
                        TrackedRecruiter.is_auth_active == True,
                        TrackedRecruiter.access_token.isnot(None)
                    )
                    .limit(BATCH_SIZE)
                )
                if task.vacancy_pattern:
                    query = query.filter(Vacancy.title.ilike(f"%{task.vacancy_pattern}%"))
                if task.city_pattern:
                    query = query.filter(Vacancy.city.ilike(f"%{task.city_pattern}%"))

                res = await db.execute(query)
                batch = res.scalars().all()

                if not batch:
                    task.status = 'completed'
                    task.completed_at = datetime.datetime.now(datetime.timezone.utc)
                    await db.commit()
                    logger.info(f"[MailingTask #{task.id}] Полностью завершена.")
                    
                    # === ОТПРАВКА АЛЕРТА АДМИНАМ ===
                    # Формируем и отправляем отчёт асинхронно (не блокируем основной поток)
                    report_text = format_mailing_report(task)
                    asyncio.create_task(
                        send_system_alert(report_text, alert_type="admin_only")
                    )
                    # =================================
                    continue

                # 5. ФУНКЦИЯ ДЛЯ ПАРАЛЛЕЛЬНОЙ ОБРАБОТКИ ОДНОГО ДИАЛОГА
                async def process_single_candidate(d_id):
                    async with semaphore:
                        # Каждому кандидату своя сессия, чтобы не мешать другим и быстро коммитить
                        async with SessionLocal() as local_db:
                            try:
                                # Подгружаем диалог и настройки внутри сессии
                                settings_res = await local_db.execute(select(AppSettings).filter_by(id=1))
                                settings = settings_res.scalar_one_or_none()
                                stmt_d = select(Dialogue).options(selectinload(Dialogue.recruiter)).filter(Dialogue.id == d_id).with_for_update(skip_locked=True)
                                d_res = await local_db.execute(stmt_d)
                                dialogue = d_res.scalar_one_or_none()
                                
                                
                                
                                
                                if not dialogue or not settings: return False
                                # === НОВАЯ ЛОГИКА: ПРОВЕРКА ПАПКИ НА HH ===
                                current_folder_on_hh = await hh_api.get_negotiation_current_folder(
                                    dialogue.recruiter, local_db, dialogue.hh_response_id
                                )
                                if current_folder_on_hh is None:
                                    return
                                if current_folder_on_hh == 404:
                                    # Отклик удален или вакансия пропала
                                    dialogue.status = 'not_found'
                                    dialogue.active_mailing_task_id = task.id
                                    await local_db.commit()
                                    return {
                                        "result": "skipped",
                                        "dialogue_id": dialogue.id,
                                        "hh_response_id": dialogue.hh_response_id,
                                        "reason": "not_found_on_hh"
                                    }

                                if current_folder_on_hh != 'consider':
                                    # Рекрутер уже обработал кандидата вручную (перевел в отказ/интервью)
                                    dialogue.status = 'recruiter_handled'
                                    # Чтобы он больше не попадал в выборку для этой рассылки
                                    dialogue.active_mailing_task_id = task.id 
                                    await local_db.commit()
                                    return {
                                        "result": "skipped",
                                        "dialogue_id": dialogue.id,
                                        "hh_response_id": dialogue.hh_response_id,
                                        "reason": f"moved_to_{current_folder_on_hh}"
                                    }
                                # =========================================

                                cost = settings.cost_per_reactivation
                                if settings.balance_outbound < cost:
                                    return "out_of_funds"

                                # Проверка порогов баланса
                                if settings.balance_outbound < settings.low_balance_threshold and not settings.notified_low_outbound:
                                    asyncio.create_task(send_system_alert(
                                        f"⚠️ БАЛАНС РАССЫЛОК ниже {settings.low_balance_threshold} руб. Оставлось: {settings.balance_outbound} руб."
                                    ))
                                    settings.notified_low_outbound = True
                                elif settings.balance_outbound >= settings.low_balance_threshold:
                                    settings.notified_low_outbound = False

                                # ОТПРАВКА
                                status = await hh_api.send_message(dialogue.recruiter, local_db, dialogue.hh_response_id, task.message_text)
                                
                                if status == 200:
                                    await local_db.execute(
                                        update(AppSettings)
                                        .where(AppSettings.id == 1)
                                        .values(
                                            balance_outbound=AppSettings.balance_outbound - cost,
                                            total_spent_reactivation=AppSettings.total_spent_reactivation + cost
                                        )
                                    )
                                    
                                    dialogue.current_mode = 'reactivation'
                                    dialogue.status = 'in_progress'
                                    dialogue.active_mailing_task_id = task.id
                                    dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
                                    dialogue.last_message_at = datetime.datetime.now(datetime.timezone.utc)
                                    new_history = {
                                        'role': 'assistant',
                                        'content': task.message_text,
                                        'timestamp_msk': datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK'),
                                        'type': 'mass_mailing'
                                    }
                                    dialogue.history = (list(dialogue.history) if dialogue.history else []) + [new_history]
                                    
                                    await log_analytics_event(local_db, dialogue, 'reactivation_sent')
                                    MESSAGES_SENT_TOTAL.inc()
                                    await local_db.commit()
                                    return {
                                        "result": "sent",
                                        "dialogue_id": dialogue.id,
                                        "hh_response_id": dialogue.hh_response_id,
                                        "reason": None
                                    }
                                
                                elif status == 403:
                                    dialogue.status = 'vacancy_closed_or_resume_not_found'
                                    dialogue.active_mailing_task_id = task.id
                                    await local_db.commit()
                                    return {
                                        "result": "skipped",
                                        "dialogue_id": dialogue.id,
                                        "hh_response_id": dialogue.hh_response_id,
                                        "reason": "vacancy_closed_or_resume_not_found"
                                    }
                                local_db.rollback()
                                return {
                                    "result": "error",
                                    "dialogue_id": d_id,  # используем d_id, т.к. dialogue может не загрузиться
                                    "hh_response_id": None,
                                    "reason": str(e)[:100] if e else "unknown_error"  # обрезаем, чтобы не засорять лог
                                }
                            except Exception as e:
                                logger.error(f"Ошибка в процессе отправки диалога {d_id}: {e}")
                                return {
                                    "result": "error",
                                    "dialogue_id": d_id,  # используем d_id, т.к. dialogue может не загрузиться
                                    "hh_response_id": None,
                                    "reason": str(e)[:100] if e else "unknown_error"  # обрезаем, чтобы не засорять лог
                                }

                # 6. ЗАПУСК ПАРАЛЛЕЛЬНОЙ ПАЧКИ
                tasks_to_run = [process_single_candidate(d.id) for d in batch]
                results = await asyncio.gather(*tasks_to_run)

                # 7. ОБНОВЛЕНИЕ СТАТИСТИКИ ЗАДАЧИ
                # Фильтруем только успешные результаты-словари
                valid_results = [r for r in results if isinstance(r, dict)]
                
                sent_count = sum(1 for r in valid_results if r["result"] == "sent")
                skipped_count = sum(1 for r in valid_results if r["result"] == "skipped")
                error_count = sum(1 for r in valid_results if r["result"] == "error")
                
                
                current_task = await db.get(MailingTask, task.id, with_for_update=True)
                # Инкрементим счётчики
                current_task.processed_count += len(valid_results)  # всего обработано в этой пачке
                current_task.messages_sent += sent_count
                current_task.skipped_count += skipped_count
                current_task.error_count += error_count
                
                # Добавляем события в лог (по одному на диалог)
                for res in valid_results:
                    add_to_processing_log(
                        task_obj=current_task,
                        dialogue_id=res["dialogue_id"],
                        hh_id=res["hh_response_id"],
                        action=res["result"],
                        reason=res["reason"]
                    )
                
                # Суммируем затраты (только за успешные отправки)

                cost_per_one = (await db.execute(select(AppSettings.cost_per_reactivation))).scalar() or Decimal('5.00')
                current_task.total_cost = (current_task.total_cost or Decimal('0.00')) + (sent_count * cost_per_one)

                if "out_of_funds" in results:
                    current_task.status = 'paused_no_funds'
                    logger.warning(f"[MailingTask #{task.id}] Остановка: недостаточно средств на балансе Outbound.")
                    # === АЛЕРТ О НЕХВАТКЕ СРЕДСТВ ===
                    urgent_text = (
                        f"🚨 <b>Рассылка #{current_task.id} приостановлена!</b>\n"
                        f"❌ Причина: недостаточно средств на балансе Outbound.\n"
                        # f"📊 Обработано: {current_task.processed_count}/{current_task.candidates_found}\n"
                        # f"💰 Текущий баланс: {settings.balance_outbound if settings else 'N/A'} руб.\n"
                        # f"💸 Стоимость одной отправки: {settings.cost_per_reactivation if settings else 'N/A'} руб."
                    )
                    asyncio.create_task(
                        send_system_alert(urgent_text, alert_type="admin_only")
                    )
                    # =================================

                await db.commit()
                logger.info(
                    f"[MailingTask #{task.id}] Пачка: {len(batch)} диалогов. "
                    f"Отправлено: {sent_count}, Пропущено: {skipped_count}, Ошибки: {error_count}. "
                    f"Прогресс: {current_task.processed_count}/{current_task.candidates_found}"
                )

            except Exception as e:
                logger.error(f"Критическая ошибка MailingTask: {e}", exc_info=True)
                await db.rollback()

        # Маленькая пауза между пачками, чтобы не перегружать CPU
        await asyncio.sleep(5)










































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