import asyncio
import logging
import os
import re
import datetime
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from zoneinfo import ZoneInfo
# --- ДОБАВЛЕН НОВЫЙ ИМПОРТ ДЛЯ РАБОТЫ С ФАЙЛАМИ В ПАМЯТИ ---
from aiogram.types import BufferedInputFile
# --- КОНЕЦ НОВОГО ИМПОРТА ---
from hr_bot.utils.sentry_config import init_sentry

from hr_bot.utils.logger_config import setup_logging
from hr_bot.db.models import SessionLocal, SyncSessionLocal, Candidate, NotificationQueue, Dialogue, Vacancy, InactiveNotificationQueue, RejectedNotificationQueue, DeclinedInterviewNotificationQueue

from hr_bot.tg_bot.middlewares import DbSessionMiddleware
from hr_bot.tg_bot.handlers import main_router
from hr_bot.utils.formatters import mask_fio

# Создаем константу для часового пояса
try:
    SPB_TIMEZONE = ZoneInfo("Europe/Moscow")
except Exception:
    SPB_TIMEZONE = datetime.timezone(datetime.timedelta(hours=3))

logger = logging.getLogger(__name__)

# --- КОНСТАНТЫ ДЛЯ ОЧИСТКИ ИСТОРИИ ---
HISTORY_RETENTION_DAYS = 30
CLEANUP_RUN_HOUR_UTC = 3
CLEANUP_CHECK_INTERVAL_SECONDS = 6000
# --- КОНЕЦ КОНСТАНТ ---

# --- КОНСТАНТЫ МОНИТОРИНГА ---
HEALTH_CHECK_INTERVAL = 60  # Проверка раз в минуту
MAX_STUCK_TIME = 600        # Перезапуск, если нет пульса 5 минут
# -----------------------------

class TaskHealthMonitor:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.last_heartbeat = {}
        self.tasks = {}
        self.shutdown = False

    def heartbeat(self, task_name: str):
        self.last_heartbeat[task_name] = datetime.datetime.now()

    async def check_and_restart(self):
        while not self.shutdown:
            try:
                now = datetime.datetime.now()
                for name in ['qualified', 'inactive', 'rejected', 'declined_interview', 'cleanup']:
                    last = self.last_heartbeat.get(name)
                    # Если задача завершена или зависла (нет пульса)
                    if not last or (now - last).total_seconds() > MAX_STUCK_TIME or (name in self.tasks and self.tasks[name].done()):
                        logger.warning(f"♻️ Перезапуск задачи {name}...")
                        if name in self.tasks: self.tasks[name].cancel()
                        
                        if name == 'qualified':
                            self.tasks[name] = asyncio.create_task(check_and_send_notifications(self.bot, self))
                        elif name == 'inactive':
                            self.tasks[name] = asyncio.create_task(check_and_send_inactive_alerts(self.bot, self))
                        elif name == 'rejected':
                            self.tasks[name] = asyncio.create_task(check_and_send_rejected_alerts(self.bot, self))
                        elif name == 'declined_interview':
                            self.tasks[name] = asyncio.create_task(check_and_send_declined_interview_alerts(self.bot, self))
                        elif name == 'cleanup':
                            self.tasks[name] = asyncio.create_task(run_history_cleanup_task(self))
                        
                        self.last_heartbeat[name] = now
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Ошибка монитора: {e}")
                await asyncio.sleep(10)


def is_system_command(content: str) -> bool:
    """Проверяет, является ли сообщение системной командой."""
    return content.startswith("[SYSTEM COMMAND]")

def escape_markdown(text: str) -> str:
    """
    Экранирует специальные символы для Telegram Markdown (старый стиль).
    """
    if not isinstance(text, str):
        text = str(text)

    escape_chars = r'_*`['
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

async def check_and_send_notifications(bot: Bot, health_monitor: TaskHealthMonitor):
    """
    Фоновая задача, которая проверяет очередь и рассылает уведомления
    в определенный групповой чат, включая прикрепленную историю диалога.
    """
    
    logger.info(f"Фоновый обработчик уведомлений запущен.")
    iteration_count = 0  # Счетчик итераций для отладки
    while True:
        health_monitor.heartbeat('qualified') # Добавили пульс
        iteration_count += 1
        logger.info(f"[Qualified] Итерация #{iteration_count}: начало цикла проверки очереди.")
        
        async with SessionLocal() as db_session:
            logger.info(f"[Qualified] Итерация #{iteration_count}: открыта сессия БД.")
            try:
                result = await asyncio.wait_for(
                    db_session.execute(
                        select(NotificationQueue).filter_by(status='pending').limit(10)
                    ),
                    timeout=30.0  # Таймаут 30 секунд на запрос
                )
                tasks = result.scalars().all()

                if not tasks:
                    await asyncio.sleep(10)
                    continue

                logger.info(f"[Notification Sender] Найдено {len(tasks)} новых уведомлений для отправки.")

                for task in tasks:
                    health_monitor.heartbeat('qualified')
                    dialogue = None
                    candidate = None
                    vacancy = None

                    # --- ИЗМЕНЁННЫЙ БЛОК КОДА: Теперь берем кандидата и его ПОСЛЕДНИЙ ОБНОВЛЕННЫЙ ДИАЛОГ ---
                    candidate_result = await db_session.execute(
                        select(Candidate)
                        .options(
                            selectinload(Candidate.dialogues).selectinload(Dialogue.vacancy),
                            selectinload(Candidate.dialogues).selectinload(Dialogue.recruiter)    # И их вакансии
                        )
                        .filter_by(id=task.candidate_id) # Фильтруем по candidate_id из задачи NotificationQueue
                    )
                    candidate = candidate_result.scalar_one_or_none()

                    if not candidate or not candidate.dialogues:
                        task.status = 'error'
                        logger.error(f"Не найден кандидат или у него нет диалогов для задачи NotificationQueue {task.id}. Candidate ID: {task.candidate_id}")
                        await db_session.commit()
                        continue

                    # Сортируем все диалоги кандидата по last_updated в порядке убывания (самые свежие первыми)
                    # Если last_updated отсутствует, используем минимальное время, чтобы оно оказалось в конце.
                    sorted_dialogues = sorted(
                        candidate.dialogues,
                        key=lambda d: d.last_updated if d.last_updated else datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
                        reverse=True
                    )

                    dialogue = sorted_dialogues[0] # Берем самый свежий диалог
                    vacancy = dialogue.vacancy     # Вакансия, связанная с этим диалогом
                    # --- КОНЕЦ ИЗМЕНЁННОГО БЛОКА КОДА ---
                    recruiter = dialogue.recruiter


                    # --- ПРОВЕРКА НАСТРОЕК РЕКРУТЕРА ---
                    if not recruiter or not recruiter.telegram_chat_id or not recruiter.topic_qualified_id:
                        logger.warning(
                            f"Для рекрутера {recruiter.name if recruiter else 'Unknown'} не настроен чат или топик 'qualified'. "
                            f"Уведомление {task.id} не может быть отправлено."
                        )
                        task.status = 'skipped_no_chat' # Или error, как удобнее
                        await db_session.commit()
                        continue
                    
                    target_chat_id = recruiter.telegram_chat_id
                    target_thread_id = recruiter.topic_qualified_id


                    resume_link = f"https://hh.ru/resume/{candidate.hh_resume_id}"

                    safe_vacancy_title = escape_markdown(vacancy.title)
                    safe_masked_name = escape_markdown(mask_fio(candidate.full_name))
                    safe_age = escape_markdown(candidate.age or 'Не указан')
                    safe_citizenship = escape_markdown(candidate.citizenship or 'Не указано')
                    
                    safe_city = escape_markdown(vacancy.city or 'Не указан')
                    
                    safe_phone_number = escape_markdown(candidate.phone_number or "—")

                    message_text = (
                        f"📌 Новый кандидат по вакансии: ✨*{safe_vacancy_title}*✨\n"
                        f"Город вакансии: 📍*{safe_city}*📍\n\n"
                        f"ФИО: {safe_masked_name}\n"
                        f"Резюме кандидата: [Открыть на HH.ru]({resume_link})\n\n"
                        f"URL: {resume_link}\n\n"
                        f"Возраст: {safe_age}\n"
                        f"Гражданство: {safe_citizenship}\n"
                        
                        
                        f"Номер телефона: {safe_phone_number}\n\n"
                        f"Статус: ✅ Прошёл квалификацию"
                    )

                    chat_transcript_file = None
                    if dialogue.history:
                        formatted_history_lines = []
                        # Улучшенная шапка
                        formatted_history_lines.append(f"=== ИСТОРИЯ ДИАЛОГА ===")
                        formatted_history_lines.append(f"ID отклика: {dialogue.hh_response_id}")
                        # <<< НАЧАЛО НОВОГО БЛОКА >>>
                        if dialogue.response_created_at:
                            # Конвертируем UTC время из БД в МСК
                            response_time_msk = dialogue.response_created_at.astimezone(SPB_TIMEZONE)
                            # Форматируем в красивую строку
                            formatted_time_str = response_time_msk.strftime('%d.%m.%Y в %H:%M:%S')
                            formatted_history_lines.append(f"Время отклика (МСК): {formatted_time_str}")
                        # <<< КОНЕЦ НОВОГО БЛОКА >>>
                        formatted_history_lines.append(f"Кандидат: {safe_masked_name}")
                        formatted_history_lines.append(f"Вакансия: {safe_vacancy_title}, {safe_city}")
                        formatted_history_lines.append("--------------------------------------------------")
                        formatted_history_lines.append("") # Пустая строка после шапки для лучшего отделения

                        for entry in dialogue.history:
                            role = entry.get('role')
                            content = entry.get('content')
                            if not content:  # Пропускаем пустые сообщения
                                continue

                            if is_system_command(content):
                                logger.debug(f"Пропущена системная команда в истории: {content}")
                                continue

                            # Извлекаем и форматируем время
                            timestamp_raw = entry.get('timestamp_msk', '')
                            # Убираем секунды и часовой пояс для краткости
                            timestamp_clean = timestamp_raw.split('.')[0][:-7] if timestamp_raw else ''
                            timestamp_prefix = f"[{timestamp_clean}]" if timestamp_clean else ''

                            formatted_history_lines.append("")  # Пустая строка перед каждым сообщением
                            if role == 'user':
                                formatted_history_lines.append(f"{timestamp_prefix} 👤 Кандидат: {content}")
                            elif role == 'assistant':
                                formatted_history_lines.append(f"{timestamp_prefix} 🤖 Бот: {content}")

                        formatted_history_string = "\n".join(formatted_history_lines)

                        file_name = f"transcription_{dialogue.hh_response_id}.txt"
                        chat_transcript_file = BufferedInputFile(formatted_history_string.encode('utf-8'), filename=file_name)
                        logger.debug(f"Сформирован файл транскрипции '{file_name}' для диалога {dialogue.hh_response_id}")
                    else:
                        logger.debug(f"История диалога для {dialogue.hh_response_id} пуста или отсутствует, файл не будет прикреплен.")

                    try:
                        async with asyncio.timeout(120.0):
                            if chat_transcript_file:
                                await bot.send_document(
                                    chat_id=target_chat_id,          # ИСПОЛЬЗУЕМ ID ИЗ РЕКРУТЕРА
                                    document=chat_transcript_file,
                                    caption=message_text,
                                    parse_mode=ParseMode.MARKDOWN,
                                    message_thread_id=target_thread_id # ИСПОЛЬЗУЕМ ТОПИК ИЗ РЕКРУТЕРА
                                )
                                logger.info(f"Уведомление по кандидату {candidate.id} (документ с текстом) успешно отправлено.")
                            else:
                                # Если по какой-то причине файл транскрибации не был сформирован,
                                # отправляем только текстовое сообщение.
                                await bot.send_message(
                                    chat_id=target_chat_id,
                                    text=message_text,
                                    message_thread_id=target_thread_id
                                )
                                logger.warning(f"Файл транскрипции для кандидата {candidate.id} отсутствовал. Отправлено только текстовое уведомление.")
                                await asyncio.sleep(5)

                            task.status = 'sent'
                            logger.info(f"Уведомление по кандидату {candidate.id} полностью обработано.")

                    except Exception as e:
                        task.status = 'error'
                        logger.error(f"Не удалось отправить уведомление по задаче {task.id}: {e}", exc_info=True)

                    finally:
                        await db_session.commit()

                await asyncio.sleep(5)

            except Exception as e:
                logger.critical(f"Критическая ошибка в фоновом обработчике: {e}", exc_info=True)
                await db_session.rollback()
                await asyncio.sleep(30)


async def check_and_send_inactive_alerts(bot: Bot, health_monitor: TaskHealthMonitor):
    """
    Фоновая задача, которая проверяет очередь InactiveNotificationQueue
    и рассылает уведомления о неактивных кандидатах в групповой чат.
    """
    

    logger.info(f"Фоновый обработчик уведомлений о неактивных кандидатах запущен.")
    iteration_count = 0
    while True:
        health_monitor.heartbeat('inactive')
        iteration_count += 1
        logger.info(f"[Inactive] Итерация #{iteration_count}: начало цикла.")
        
        async with SessionLocal() as db_session:
            logger.info(f"[Inactive] Итерация #{iteration_count}: сессия открыта.")
            
            try:
                # Ищем записи в очереди со статусом 'pending'
                result = await asyncio.wait_for(
                    db_session.execute(
                        select(InactiveNotificationQueue)
                        .filter_by(status='pending')
                        .limit(10)
                    ),
                    timeout=30.0
                )
                tasks = result.scalars().all()

                if not tasks:
                    await asyncio.sleep(15) # Пауза побольше, если нет задач
                    continue

                logger.info(f"[Inactive Alert Sender] Найдено {len(tasks)} новых уведомлений о неактивных кандидатах для отправки.")

                for task in tasks:
                    health_monitor.heartbeat('inactive')
                    # Загружаем диалог, кандидата и вакансию с предзагрузкой связей
                    dialogue_result = await db_session.execute(
                        select(Dialogue)
                        .options(
                            selectinload(Dialogue.candidate),
                            selectinload(Dialogue.vacancy),
                            selectinload(Dialogue.recruiter)
                        )
                        .filter_by(id=task.dialogue_id)
                    )
                    dialogue = dialogue_result.scalar_one_or_none()

                    if not dialogue or not dialogue.candidate or not dialogue.vacancy or not dialogue.recruiter:
                        task.status = 'error'
                        logger.error(f"Не найден диалог, кандидат или вакансия для задачи InactiveNotificationQueue {task.id}.")
                        await db_session.commit()
                        continue
                    recruiter = dialogue.recruiter

                    # --- ПРОВЕРКА НАСТРОЕК РЕКРУТЕРА ---
                    if not recruiter.telegram_chat_id or not recruiter.topic_timeout_id:
                        # Если не настроен канал для молчунов
                        task.status = 'skipped_no_chat'
                        await db_session.commit()
                        continue
                    
                    target_chat_id = recruiter.telegram_chat_id
                    target_thread_id = recruiter.topic_timeout_id

                    candidate = dialogue.candidate
                    vacancy = dialogue.vacancy

                    resume_link = f"https://hh.ru/resume/{candidate.hh_resume_id}"

                    # Используем escape_markdown и mask_fio
                    safe_vacancy_title = escape_markdown(vacancy.title)
                    safe_city = escape_markdown(vacancy.city or 'Не указан')
                    safe_masked_name = escape_markdown(mask_fio(candidate.full_name)) # С маскировкой только отчества
                    safe_phone_number = escape_markdown(candidate.phone_number or "—")
                    message_text = (
                        f"⚠️ Соискатель не отвечает более 2 часов\n\n"
                        f"Вакансия: ✨*{safe_vacancy_title}*✨\n"
                        f"Город: 📍*{safe_city}*📍\n"
                        f"Имя: {safe_masked_name}\n" # Теперь с маскировкой только отчества
                        f"Номер телефона: {safe_phone_number}\n"
                        f"Ссылка на резюме: [Открыть на HH.ru]({resume_link})\n\n"
                        f"URL: {resume_link}" # Добавляем URL отдельно, т.к. disable_web_page_preview не для документов
                    )

                    # Формирование истории чата (повторно используем логику)
                    chat_transcript_file = None
                    if dialogue.history:
                        formatted_history_lines = []
                        formatted_history_lines.append(f"=== ИСТОРИЯ ДИАЛОГА ===")
                        formatted_history_lines.append(f"ID отклика: {dialogue.hh_response_id}")
                        # <<< НАЧАЛО НОВОГО БЛОКА >>>
                        if dialogue.response_created_at:
                            # Конвертируем UTC время из БД в МСК
                            response_time_msk = dialogue.response_created_at.astimezone(SPB_TIMEZONE)
                            # Форматируем в красивую строку
                            formatted_time_str = response_time_msk.strftime('%d.%m.%Y в %H:%M:%S')
                            formatted_history_lines.append(f"Время отклика (МСК): {formatted_time_str}")
                        # <<< КОНЕЦ НОВОГО БЛОКА >>>
                        formatted_history_lines.append(f"Кандидат: {safe_masked_name}")
                        formatted_history_lines.append(f"Вакансия: {safe_vacancy_title}, {safe_city}")
                        formatted_history_lines.append("--------------------------------------------------")
                        formatted_history_lines.append("")

                        for entry in dialogue.history:
                            role = entry.get('role')
                            content = entry.get('content')
                            if not content:  # Пропускаем пустые сообщения
                                continue

                            if is_system_command(content):
                                logger.debug(f"Пропущена системная команда в истории: {content}")
                                continue

                            # Извлекаем и форматируем время
                            timestamp_raw = entry.get('timestamp_msk', '')
                            # Убираем секунды и часовой пояс для краткости
                            timestamp_clean = timestamp_raw.split('.')[0][:-7] if timestamp_raw else ''
                            timestamp_prefix = f"[{timestamp_clean}]" if timestamp_clean else ''

                            formatted_history_lines.append("")  # Пустая строка перед каждым сообщением
                            if role == 'user':
                                formatted_history_lines.append(f"{timestamp_prefix} 👤 Кандидат: {content}")
                            elif role == 'assistant':
                                formatted_history_lines.append(f"{timestamp_prefix} 🤖 Бот: {content}")

                        formatted_history_string = "\n".join(formatted_history_lines)

                        file_name = f"inactive_transcription_{dialogue.hh_response_id}.txt"
                        chat_transcript_file = BufferedInputFile(formatted_history_string.encode('utf-8'), filename=file_name)
                        logger.debug(f"Сформирован файл транскрипции '{file_name}' для неактивного диалога {dialogue.hh_response_id}")
                    else:
                        logger.debug(f"История диалога для неактивного кандидата {dialogue.hh_response_id} пуста или отсутствует, файл не будет прикреплен.")

                    try:
                        async with asyncio.timeout(120.0):
                            if chat_transcript_file:
                                await bot.send_document(
                                    chat_id=target_chat_id,
                                    document=chat_transcript_file,
                                    caption=message_text,
                                    parse_mode=ParseMode.MARKDOWN,
                                    message_thread_id=target_thread_id
                                )
                                logger.info(f"Уведомление о неактивном кандидате (диалог {dialogue.id}) отправлено.")
                                await asyncio.sleep(5)
                            else:
                                await bot.send_message(
                                    chat_id=target_chat_id,
                                    text=message_text,
                                    message_thread_id=target_thread_id
                                )
                                logger.warning(f"Файл транскрипции для неактивного кандидата {dialogue.hh_response_id} отсутствовал. Отправлено только текстовое уведомление.")

                            task.status = 'sent'
                            task.processed_at = datetime.datetime.now(datetime.timezone.utc)


                    except Exception as e:
                        task.status = 'error'
                        task.processed_at = datetime.datetime.now(datetime.timezone.utc)
                        logger.error(f"Не удалось отправить уведомление о неактивном кандидате для задачи InactiveNotificationQueue {task.id}: {e}", exc_info=True)

                    finally:
                        await db_session.commit()

                await asyncio.sleep(5) # Короткая пауза между партиями задач

            except Exception as e:
                logger.critical(f"Критическая ошибка в фоновом обработчике неактивных кандидатов: {e}", exc_info=True)
                await db_session.rollback()
                await asyncio.sleep(30) # Более длительная пауза при критической ошибке

async def check_and_send_rejected_alerts(bot: Bot, health_monitor: TaskHealthMonitor):
    """
    Фоновая задача, которая проверяет очередь RejectedNotificationQueue
    и рассылает уведомления об отклоненных кандидатах в групповой чат.
    """


    logger.info(f"Фоновый обработчик уведомлений об отклоненных кандидатах запущен.")
    iteration_count = 0
    while True:
        health_monitor.heartbeat('rejected')
        iteration_count += 1
        logger.info(f"[Rejected] Итерация #{iteration_count}: начало цикла.")
        
        async with SessionLocal() as db_session:
            try:
                # Ищем записи в очереди со статусом 'pending'
                logger.info(f"[Rejected] Итерация #{iteration_count}: сессия открыта.")
                
                result = await asyncio.wait_for(
                    db_session.execute(
                        select(RejectedNotificationQueue)
                        .filter_by(status='pending')
                        .limit(10)
                    ),
                    timeout=30.0
                )
                tasks = result.scalars().all()

                if not tasks:
                    await asyncio.sleep(15) # Пауза побольше, если нет задач
                    continue

                logger.info(f"[Rejected Alert Sender] Найдено {len(tasks)} новых уведомлений об отклоненных кандидатах для отправки.")

                for task in tasks:
                    health_monitor.heartbeat('rejected')
                    # Загружаем диалог, кандидата и вакансию с предзагрузкой связей
                    dialogue_result = await db_session.execute(
                        select(Dialogue)
                        .options(
                            selectinload(Dialogue.candidate),
                            selectinload(Dialogue.vacancy),
                            selectinload(Dialogue.recruiter)
                        )
                        .filter_by(id=task.dialogue_id)
                    )
                    dialogue = dialogue_result.scalar_one_or_none()

                    if not dialogue or not dialogue.candidate or not dialogue.vacancy or not dialogue.recruiter:
                        task.status = 'error'
                        logger.error(f"Не найден диалог, кандидат или вакансия для задачи RejectedNotificationQueue {task.id}.")
                        await db_session.commit()
                        continue
                    
                    recruiter = dialogue.recruiter

                    # --- ПРОВЕРКА НАСТРОЕК РЕКРУТЕРА ---
                    if not recruiter.telegram_chat_id or not recruiter.topic_rejected_id:
                        # Если не настроен канал для молчунов
                        task.status = 'skipped_no_chat'
                        await db_session.commit()
                        continue
                    
                    target_chat_id = recruiter.telegram_chat_id
                    target_thread_id = recruiter.topic_rejected_id
                    # -----------------------------------

                    candidate = dialogue.candidate
                    vacancy = dialogue.vacancy

                    resume_link = f"https://hh.ru/resume/{candidate.hh_resume_id}"

                    # Используем escape_markdown и mask_fio
                    safe_vacancy_title = escape_markdown(vacancy.title)
                    safe_city = escape_markdown(vacancy.city or 'Не указан')
                    safe_masked_name = escape_markdown(mask_fio(candidate.full_name)) # С маскировкой только отчества
                    safe_phone_number = escape_markdown(candidate.phone_number or "—")
                    message_text = (
                        f"❌ Кандидату отказано в квалификации\n\n"
                        f"Вакансия: ✨*{safe_vacancy_title}*✨\n"
                        f"Город: 📍*{safe_city}*📍\n"
                        f"Имя: {safe_masked_name}\n" # Теперь с маскировкой только отчества
                        f"Номер телефона: {safe_phone_number}\n"
                        f"Ссылка на резюме: [Открыть на HH.ru]({resume_link})\n\n"
                        f"URL: {resume_link}"
                    )

                    # Формирование истории чата (повторно используем логику)
                    chat_transcript_file = None
                    if dialogue.history:
                        formatted_history_lines = []
                        formatted_history_lines.append(f"=== ИСТОРИЯ ДИАЛОГА ===")
                        formatted_history_lines.append(f"ID отклика: {dialogue.hh_response_id}")
                        # <<< НАЧАЛО НОВОГО БЛОКА >>>
                        if dialogue.response_created_at:
                            # Конвертируем UTC время из БД в МСК
                            response_time_msk = dialogue.response_created_at.astimezone(SPB_TIMEZONE)
                            # Форматируем в красивую строку
                            formatted_time_str = response_time_msk.strftime('%d.%m.%Y в %H:%M:%S')
                            formatted_history_lines.append(f"Время отклика (МСК): {formatted_time_str}")
                        # <<< КОНЕЦ НОВОГО БЛОКА >>>
                        formatted_history_lines.append(f"Кандидат: {safe_masked_name}")
                        formatted_history_lines.append(f"Вакансия: {safe_vacancy_title}, {safe_city}")
                        formatted_history_lines.append("--------------------------------------------------")
                        formatted_history_lines.append("")

                        for entry in dialogue.history:
                            role = entry.get('role')
                            content = entry.get('content')
                            if not content:  # Пропускаем пустые сообщения
                                continue

                            if is_system_command(content):
                                logger.debug(f"Пропущена системная команда в истории: {content}")
                                continue

                            # Извлекаем и форматируем время
                            timestamp_raw = entry.get('timestamp_msk', '')
                            # Убираем секунды и часовой пояс для краткости
                            timestamp_clean = timestamp_raw.split('.')[0][:-7] if timestamp_raw else ''
                            timestamp_prefix = f"[{timestamp_clean}]" if timestamp_clean else ''

                            formatted_history_lines.append("")  # Пустая строка перед каждым сообщением
                            if role == 'user':
                                formatted_history_lines.append(f"{timestamp_prefix} 👤 Кандидат: {content}")
                            elif role == 'assistant':
                                formatted_history_lines.append(f"{timestamp_prefix} 🤖 Бот: {content}")

                        formatted_history_string = "\n".join(formatted_history_lines)

                        file_name = f"rejected_transcription_{dialogue.hh_response_id}.txt"
                        chat_transcript_file = BufferedInputFile(formatted_history_string.encode('utf-8'), filename=file_name)
                        logger.debug(f"Сформирован файл транскрипции '{file_name}' для отклоненного диалога {dialogue.hh_response_id}")
                    else:
                        logger.debug(f"История диалога для отклоненного кандидата {dialogue.hh_response_id} пуста или отсутствует, файл не будет прикреплен.")

                    try:
                        async with asyncio.timeout(120.0):
                            if chat_transcript_file:
                                await bot.send_document(
                                    chat_id=target_chat_id,
                                    document=chat_transcript_file,
                                    caption=message_text,
                                    parse_mode=ParseMode.MARKDOWN,
                                    message_thread_id=target_thread_id
                                )
                                logger.info(f"Уведомление об отклоненном кандидате (диалог {dialogue.id}) отправлено.")
                                await asyncio.sleep(5)
                            else:
                                await bot.send_message(
                                    chat_id=target_chat_id,
                                    text=message_text,
                                    message_thread_id=target_thread_id
                                )
                                logger.warning(f"Файл транскрипции для отклоненного кандидата {dialogue.hh_response_id} отсутствовал. Отправлено только текстовое уведомление.")

                            task.status = 'sent'
                            task.processed_at = datetime.datetime.now(datetime.timezone.utc)

                    except Exception as e:
                        task.status = 'error'
                        task.processed_at = datetime.datetime.now(datetime.timezone.utc)
                        logger.error(f"Не удалось отправить уведомление об отклоненном кандидате для задачи RejectedNotificationQueue {task.id}: {e}", exc_info=True)

                    finally:
                        await db_session.commit()

                await asyncio.sleep(5) # Короткая пауза между партиями задач

            except Exception as e:
                logger.critical(f"Критическая ошибка в фоновом обработчике отклоненных кандидатов: {e}", exc_info=True)
                await db_session.rollback()
                await asyncio.sleep(30) # Более длительная пауза при критической ошибке

async def check_and_send_declined_interview_alerts(bot: Bot, health_monitor: TaskHealthMonitor):
    """
    Фоновая задача, которая проверяет очередь DeclinedInterviewNotificationQueue
    и рассылает уведомления об отказе от собеседования в групповой чат.
    """
    logger.info(f"Фоновый обработчик отказов от собеседования запущен.")
    iteration_count = 0
    while True:
        health_monitor.heartbeat('declined_interview')
        iteration_count += 1
        # Логируем начало цикла не каждый раз, чтобы не спамить, или каждый (как у вас принято)
        if iteration_count % 10 == 0:
            logger.info(f"[Declined Interview] Итерация #{iteration_count}: работаем.")
        
        async with SessionLocal() as db_session:
            try:
                # Ищем записи в очереди со статусом 'pending'
                result = await asyncio.wait_for(
                    db_session.execute(
                        select(DeclinedInterviewNotificationQueue)
                        .filter_by(status='pending')
                        .limit(10)
                    ),
                    timeout=30.0
                )
                tasks = result.scalars().all()

                if not tasks:
                    await asyncio.sleep(15) 
                    continue

                logger.info(f"[Declined Interview Sender] Найдено {len(tasks)} новых отказов от собеседования.")

                for task in tasks:
                    health_monitor.heartbeat('declined_interview')
                    
                    dialogue_result = await db_session.execute(
                        select(Dialogue)
                        .options(
                            selectinload(Dialogue.candidate),
                            selectinload(Dialogue.vacancy),
                            selectinload(Dialogue.recruiter)
                        )
                        .filter_by(id=task.dialogue_id)
                    )
                    dialogue = dialogue_result.scalar_one_or_none()

                    if not dialogue or not dialogue.candidate or not dialogue.vacancy or not dialogue.recruiter:
                        task.status = 'error'
                        logger.error(f"Не полные данные для задачи DeclinedInterviewNotificationQueue {task.id}.")
                        await db_session.commit()
                        continue
                    
                    recruiter = dialogue.recruiter

                    # --- ПРОВЕРКА НАСТРОЕК РЕКРУТЕРА ---
                    # Проверяем наличие chat_id и ID нужного топика
                    if not recruiter.telegram_chat_id or not recruiter.topic_declined_interview_id:
                        task.status = 'skipped_no_chat'
                        logger.warning(
                            f"Рекрутер {recruiter.name} не настроил топик 'declined_interview'. "
                            f"Пропускаем задачу {task.id}."
                        )
                        await db_session.commit()
                        continue
                    
                    target_chat_id = recruiter.telegram_chat_id
                    target_thread_id = recruiter.topic_declined_interview_id
                    # -----------------------------------

                    candidate = dialogue.candidate
                    vacancy = dialogue.vacancy

                    resume_link = f"https://hh.ru/resume/{candidate.hh_resume_id}"

                    safe_vacancy_title = escape_markdown(vacancy.title)
                    safe_city = escape_markdown(vacancy.city or 'Не указан')
                    safe_masked_name = escape_markdown(mask_fio(candidate.full_name))
                    safe_phone_number = escape_markdown(candidate.phone_number or "—")
                    
                    message_text = (
                        f"🙅‍♂️ *Кандидат отказался от собеседования*\n\n"
                        f"Вакансия: ✨*{safe_vacancy_title}*✨\n"
                        f"Город: 📍*{safe_city}*📍\n"
                        f"Имя: {safe_masked_name}\n"
                        f"Номер телефона: {safe_phone_number}\n"
                        f"Ссылка на резюме: [Открыть на HH.ru]({resume_link})\n\n"
                        f"URL: {resume_link}"
                    )

                    # Формирование транскрипции (стандартный блок)
                    chat_transcript_file = None
                    if dialogue.history:
                        formatted_history_lines = []
                        formatted_history_lines.append(f"=== ИСТОРИЯ ДИАЛОГА ===")
                        formatted_history_lines.append(f"ID отклика: {dialogue.hh_response_id}")
                        
                        if dialogue.response_created_at:
                            response_time_msk = dialogue.response_created_at.astimezone(SPB_TIMEZONE)
                            formatted_time_str = response_time_msk.strftime('%d.%m.%Y в %H:%M:%S')
                            formatted_history_lines.append(f"Время отклика (МСК): {formatted_time_str}")
                        
                        formatted_history_lines.append(f"Кандидат: {safe_masked_name}")
                        formatted_history_lines.append(f"Вакансия: {safe_vacancy_title}, {safe_city}")
                        formatted_history_lines.append("--------------------------------------------------")
                        formatted_history_lines.append("")

                        for entry in dialogue.history:
                            role = entry.get('role')
                            content = entry.get('content')
                            if not content: continue
                            if is_system_command(content): continue

                            timestamp_raw = entry.get('timestamp_msk', '')
                            timestamp_clean = timestamp_raw.split('.')[0][:-7] if timestamp_raw else ''
                            timestamp_prefix = f"[{timestamp_clean}]" if timestamp_clean else ''

                            formatted_history_lines.append("")
                            if role == 'user':
                                formatted_history_lines.append(f"{timestamp_prefix} 👤 Кандидат: {content}")
                            elif role == 'assistant':
                                formatted_history_lines.append(f"{timestamp_prefix} 🤖 Бот: {content}")

                        formatted_history_string = "\n".join(formatted_history_lines)
                        file_name = f"declined_interview_{dialogue.hh_response_id}.txt"
                        chat_transcript_file = BufferedInputFile(formatted_history_string.encode('utf-8'), filename=file_name)
                    
                    # Отправка
                    try:
                        async with asyncio.timeout(120.0):
                            if chat_transcript_file:
                                await bot.send_document(
                                    chat_id=target_chat_id,
                                    document=chat_transcript_file,
                                    caption=message_text,
                                    parse_mode=ParseMode.MARKDOWN,
                                    message_thread_id=target_thread_id
                                )
                            else:
                                await bot.send_message(
                                    chat_id=target_chat_id,
                                    text=message_text,
                                    message_thread_id=target_thread_id
                                )
                            
                            task.status = 'sent'
                            task.processed_at = datetime.datetime.now(datetime.timezone.utc)
                            logger.info(f"Уведомление об отказе от интервью (диалог {dialogue.id}) отправлено.")

                    except Exception as e:
                        task.status = 'error'
                        task.processed_at = datetime.datetime.now(datetime.timezone.utc)
                        logger.error(f"Ошибка отправки (declined_interview): {e}", exc_info=True)

                    finally:
                        await db_session.commit()

                await asyncio.sleep(5)

            except Exception as e:
                logger.critical(f"Критическая ошибка в обработчике отказов от интервью: {e}", exc_info=True)
                await db_session.rollback()
                await asyncio.sleep(30)


async def run_history_cleanup_task(health_monitor: TaskHealthMonitor):
    logger.info(f"Задача очистки истории запущена.")
    last_run_date = None
    
    while not health_monitor.shutdown:
        # Отправляем пульс часто (раз в итерацию)
        health_monitor.heartbeat('cleanup')
        
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        # Проверяем, наступил ли час очистки и не запускались ли мы сегодня
        if now_utc.hour == CLEANUP_RUN_HOUR_UTC and (last_run_date is None or last_run_date.date() < now_utc.date()):
            logger.info(f"Запуск очистки истории диалогов (UTC: {now_utc}).")
            try:
                async with SessionLocal() as db_session:
                    cutoff_date = now_utc - datetime.timedelta(days=HISTORY_RETENTION_DAYS)
                    stmt = (
                        update(Dialogue)
                        .where(Dialogue.last_updated < cutoff_date)
                        .where(Dialogue.history.is_not(None))
                        .values(history=None)
                    )
                    await asyncio.wait_for(db_session.execute(stmt), timeout=300.0)
                    await asyncio.wait_for(db_session.commit(), timeout=60.0)
                    logger.info("Очистка успешно завершена.")
                    last_run_date = now_utc
            except Exception as e:
                logger.error(f"Ошибка при очистке истории: {e}", exc_info=True)

        # Спим короткими интервалами (например, 60 сек), 
        # чтобы цикл прокручивался и обновлял heartbeat чаще, чем MAX_STUCK_TIME
        await asyncio.sleep(60)

# --- КОНЕЦ ФУНКЦИИ ОЧИСТКИ ---

async def main():
    """Главная функция запуска бота."""
    setup_logging(log_filename="telegram_bot.log")
    load_dotenv()
    

    bot = Bot(
        token=os.getenv("TELEGRAM_BOT_TOKEN"),
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
    )
    dp = Dispatcher()

    dp.update.middleware(DbSessionMiddleware(session_pool=SyncSessionLocal))

    dp.include_router(main_router)

    logger.info("Управляющий Telegram-бот запускается...")


    health_monitor = TaskHealthMonitor(bot)
    
    # Регистрация и запуск задач через монитор
    health_monitor.tasks['qualified'] = asyncio.create_task(check_and_send_notifications(bot, health_monitor))
    health_monitor.tasks['inactive'] = asyncio.create_task(check_and_send_inactive_alerts(bot, health_monitor))
    health_monitor.tasks['rejected'] = asyncio.create_task(check_and_send_rejected_alerts(bot, health_monitor))
    health_monitor.tasks['declined_interview'] = asyncio.create_task(check_and_send_declined_interview_alerts(bot, health_monitor))
    health_monitor.tasks['cleanup'] = asyncio.create_task(run_history_cleanup_task(health_monitor))
    
    # Запуск самого монитора
    monitor_task = asyncio.create_task(health_monitor.check_and_restart())

    await bot.delete_webhook(drop_pending_updates=True)
    
    try:
        await dp.start_polling(bot)
    finally:
        health_monitor.shutdown = True
        for t in health_monitor.tasks.values(): t.cancel()
        monitor_task.cancel()
        logger.info("Бот остановлен.")

if __name__ == '__main__':
    try:
        init_sentry()
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную.")