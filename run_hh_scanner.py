#run_hh_scanner.py

import asyncio
import time
import argparse
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
from hr_bot.utils.redis_lock import close_redis 
from hr_bot.utils.sentry_config import init_sentry
from hr_bot.utils.metrics import start_metrics_server
from hr_bot.utils.logger_config import setup_logging
from hr_bot.db.models import SessionLocal, Dialogue, Candidate, Vacancy, NotificationQueue, TrackedRecruiter, AppSettings, InactiveNotificationQueue, RejectedNotificationQueue, InterviewReminder, LlmUsageLog
from hr_bot.services import hh_api_real as hh_api
from hr_bot.services import knowledge_base
from hr_bot.services import llm_handler
from hr_bot.db import statistics_manager

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
from hr_bot.utils.metrics import start_metrics_server
# Если в metrics.py еще нет этих метрик, их лучше добавить туда.
# Но для простоты предположим, что мы добавляем их или используем универсальные.
# Давай добавим специфичные метрики для сканера:
from prometheus_client import Counter, Gauge
# ... остальные импорты

logger = logging.getLogger(__name__)

VACANCIES_ACTIVE_GAUGE = Gauge('hh_bot_vacancies_active_count', 'Number of active vacancies found', ['recruiter_id'])
NEW_RESPONSES_TOTAL = Counter('hh_bot_new_responses_total', 'Total new responses found', ['recruiter_id', 'vacancy_id'])
MESSAGES_RECEIVED_TOTAL = Counter('hh_bot_messages_received_total', 'Total incoming messages from candidates', ['recruiter_id'])

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




def _format_timestamp_to_msk(timestamp_str: str) -> str:
    """
    Преобразует строку времени из формата ISO в читаемую строку по МСК.
    Если строка некорректна, возвращает заглушку.
    """
    try:
        # SPB_TIMEZONE у вас уже определен глобально
        dt_object = datetime.datetime.fromisoformat(timestamp_str)
        msk_dt = dt_object.astimezone(SPB_TIMEZONE)
        return msk_dt.strftime('%Y-%m-%d %H:%M:%S MSK')
    except (ValueError, TypeError):
        return "время не определено"
    
async def get_all_active_vacancies_for_recruiter(recruiter_id: int, db: AsyncSession) -> list:
    """
    Асинхронно получает список вакансий и синхронизирует их владельца.
    Если вакансия перешла к текущему рекрутеру, переносит на него и активные диалоги.
    """
    function_start_time = time.monotonic()
    current_recruiter = await db.get(TrackedRecruiter, recruiter_id)
    rec_logger = logging.LoggerAdapter(logger, {
        "recruiter_id": recruiter_id, 
        "recruiter_name": current_recruiter.name,
        "worker": "scanner"
    })
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        cache_expiry_time = datetime.timedelta(minutes=VACANCY_CACHE_DURATION_MINUTES)

        

        if not current_recruiter:
            rec_logger.error(f"Рекрутер с ID {recruiter_id} не найден.")
            return []
            
        rec_logger.debug(f"Получение и синхронизация списка активных вакансий для рекрутера {current_recruiter.name}...")
        
        # 1. Проверка кэша (если недавно обновляли, просто берем из БД)
        if current_recruiter.vacancies_last_synced_at:
            time_since_sync = now - current_recruiter.vacancies_last_synced_at
            if time_since_sync < cache_expiry_time:
                rec_logger.debug(f"Используем кэш. Последняя синхронизация: {time_since_sync.total_seconds() / 60:.1f} мин. назад.")
                result = await db.execute(select(Vacancy).filter(Vacancy.recruiter_id == current_recruiter.id))
                cached_vacancies = result.scalars().all()
                return [{"id": v.hh_vacancy_id, "name": v.title, "area": {"name": v.city} if v.city else {}} for v in cached_vacancies]

        rec_logger.debug(f"Кэш устарел. Запрос к API...")
        
        # 2. Запрос к API HH
        me_data = await hh_api._make_request(current_recruiter, db, "GET", "me")
        if not me_data or not me_data.get('employer') or not me_data['employer'].get('id'):
            rec_logger.error(f"Не удалось получить employer_id.")
            return []
        employer_id = me_data['employer']['id']

        all_vacancies_from_api = []
        page = 0
        while True:
            vacancies_page = await hh_api._make_request(
                current_recruiter, db, "GET", f"employers/{employer_id}/vacancies/active",
                params={'page': page, 'per_page': 20}
            )
            if not vacancies_page or not vacancies_page.get('items'):
                break
            all_vacancies_from_api.extend(vacancies_page['items'])
            if page >= vacancies_page.get('pages', 1) - 1:
                break
            page += 1

        vac_count = len(all_vacancies_from_api)
        VACANCIES_ACTIVE_GAUGE.labels(recruiter_id=str(recruiter_id)).set(vac_count)
        
        rec_logger.debug(
            f"Найдено {len(all_vacancies_from_api)} активных вакансий. Синхронизация с БД...[recruiter_id: {recruiter_id}, active_vacancies_count: {vac_count}]"
        )
        active_hh_ids = {str(v["id"]) for v in all_vacancies_from_api}

        # 3. Синхронизация вакансий и диалогов
        for vacancy_data in all_vacancies_from_api:
            hh_vacancy_id = str(vacancy_data.get("id"))
            result = await db.execute(select(Vacancy).filter_by(hh_vacancy_id=hh_vacancy_id))
            vacancy_in_db = result.scalar_one_or_none()

            # Если вакансии нет - создаем
            if not vacancy_in_db:
                new_vacancy = Vacancy(
                    hh_vacancy_id=hh_vacancy_id,
                    title=vacancy_data.get("name", "Без названия"),
                    city=vacancy_data.get("area", {}).get("name"),
                    recruiter_id=current_recruiter.id
                )
                db.add(new_vacancy)
            else:
                # Вакансия есть. Обновляем поля (название, город)
                vacancy_in_db.title = vacancy_data.get("name", "Без названия")
                vacancy_in_db.city = vacancy_data.get("area", {}).get("name")

                # === [ГЛАВНАЯ ЛОГИКА ПЕРЕНОСА] ===
                # Если у вакансии в БД другой рекрутер (или None), а HH говорит, что она теперь наша
                if vacancy_in_db.recruiter_id != current_recruiter.id:
                    rec_logger.info(
                        f"Вакансия {hh_vacancy_id} перешла к рекрутеру {recruiter_id}. "
                        f"Переносим активные диалоги..."
                    )
                    
                    # 1. Меняем владельца вакансии
                    vacancy_in_db.recruiter_id = current_recruiter.id
                    
                    # 2. МАССОВЫЙ UPDATE ДИАЛОГОВ
                    # Одним запросом меняем recruiter_id во всех диалогах этой вакансии,
                    # которые находятся в активных статусах.
                    stmt = (
                        update(Dialogue)
                        .where(Dialogue.vacancy_id == vacancy_in_db.id)
                        .where(Dialogue.status.in_(['new', 'in_progress', 'qualified']))
                        .values(recruiter_id=current_recruiter.id)
                    )
                    await db.execute(stmt)
                    # =================================

        # 4. Отвязываем вакансии, которые больше не активны у этого рекрутера
        # (Например, рекрутер закрыл вакансию или передал другому, и её нет в current active list)
        stale_result = await db.execute(select(Vacancy).filter(
            Vacancy.recruiter_id == current_recruiter.id,
            Vacancy.hh_vacancy_id.notin_(active_hh_ids)
        ))
        for stale_vac in stale_result.scalars().all():
            stale_vac.recruiter_id = None

        current_recruiter.vacancies_last_synced_at = now
        
        # Flush применяет изменения в транзакции (в т.ч. наш update), но не комитит окончательно
        await db.flush() 
        
        rec_logger.debug(f"Синхронизация завершена.")
        return all_vacancies_from_api

    except Exception as e:
        rec_logger.error(f"Ошибка при синхронизации вакансий: {e}", exc_info=True)
        raise 
    finally:
        rec_logger.debug(f"Время выполнения: {time.monotonic() - function_start_time:.2f} sec.")

async def process_new_responses(recruiter_id: int, vacancy_ids: list, db: AsyncSession):
    """Этап 1: Ищет новые отклики по СПИСКУ вакансий."""
    function_start_time = time.monotonic()

    recruiter = None
    recruiter_name_for_logging = f"ID {recruiter_id}"
    recruiter = await db.get(TrackedRecruiter, recruiter_id)
    rec_logger = logging.LoggerAdapter(logger, {
        "recruiter_id": recruiter_id,
        "recruiter_name": recruiter.name if recruiter else "Unknown",
        "worker": "scanner"
    })
    try:
        
        if not recruiter:
            rec_logger.warning(f"process_new_responses: Рекрутер с ID {recruiter_id} не найден.")
            return
        
        
        recruiter_name_for_logging = recruiter.name
        cutoff_date = recruiter.created_at or (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        rec_logger.debug(f"Используем дату старта для рекрутера {recruiter.name}: {cutoff_date}")
        if not vacancy_ids:
            rec_logger.error("Этап 1: Нет активных вакансий для проверки 'Неразобранных'.")
            return

        rec_logger.debug(f"Этап 1: Проверка 'Неразобранных' для {len(vacancy_ids)} вакансий...")

        new_responses_with_vacancy_ids = await hh_api.get_responses_from_folder(
            recruiter, db, 'response', vacancy_ids, since_datetime=cutoff_date
        )

        for resp, associated_vacancy_id_str in new_responses_with_vacancy_ids:
            # Используем SAVEPOINT для каждого кандидата, чтобы ошибка в одном не ломала всю транзакцию
            try:
                response_id = resp.get('id')

                # --- БЕЗОПАСНЫЙ ДОСТУП К ДАННЫМ РЕЗЮМЕ ---
                resume_info = resp.get('resume')
                if not resume_info:
                    rec_logger.warning(f"Отклик {response_id} без резюме. Пропуск.")
                    continue

                candidate_first_name = resume_info.get('first_name', 'Неизвестно')
                candidate_last_name = resume_info.get('last_name', '')
                candidate_full_name = f"{candidate_first_name} {candidate_last_name}".strip()
                candidate_hh_resume_id = resume_info.get('id')
                # -----------------------------------------
                # 2. СОЗДАЕМ ПОЛНЫЙ ЛОГГЕР ДЛЯ КОНКРЕТНОГО ОТКЛИКА
                # Он содержит ID рекрутера, ID отклика, ID вакансии и имя кандидата
                scan_logger = logging.LoggerAdapter(logger, {
                    "recruiter_id": recruiter_id,
                    "recruiter_name": recruiter_name_for_logging,
                    "hh_response_id": response_id,
                    "vacancy_id": associated_vacancy_id_str, # HH ID вакансии
                    "candidate_resume_id": candidate_hh_resume_id,
                    "candidate_name": candidate_full_name,
                    "worker": "scanner",
                    "action": "new_response_found"
                })
                # -----------------------------------------
                if not response_id or (TEST_NEGOTIATION_ID and response_id != TEST_NEGOTIATION_ID):
                    continue

                # Проверка существования
                exists_query = select(func.count()).select_from(Dialogue).filter_by(hh_response_id=response_id)
                result = await db.execute(exists_query)
                if result.scalar() > 0:
                    continue

                # Проверка лимитов
                settings_result = await db.execute(
                    select(AppSettings).filter_by(id=1).with_for_update()
                )
                settings = settings_result.scalar_one_or_none()

                if not settings:
                    scan_logger.error("Настройки AppSettings не найдены в БД!")
                    continue

                # ПРОВЕРКА БАЛАНСА
                if settings.balance < settings.cost_per_dialogue:
                    scan_logger.warning(f"Недостаточно средств на балансе ({settings.balance}). Отклик {response_id} пропущен.")
                    continue

                NEW_RESPONSES_TOTAL.labels(recruiter_id=str(recruiter_id), vacancy_id=str(associated_vacancy_id_str)).inc() # <--- МЕТРИКА

                scan_logger.info(
                    f"Найден новый отклик {response_id} ({candidate_full_name})."
                )

                vacancy_in_db_result = await db.execute(
                    select(Vacancy).filter(Vacancy.hh_vacancy_id == associated_vacancy_id_str)
                )
                vacancy_in_db = vacancy_in_db_result.scalar_one_or_none()

                if not vacancy_in_db:
                    scan_logger.error(f"Вакансия {associated_vacancy_id_str} не найдена в БД. Пропуск.")
                    continue

                # Работа с кандидатом
                candidate_result = await db.execute(
                    select(Candidate).filter(Candidate.hh_resume_id == candidate_hh_resume_id)
                )
                candidate = candidate_result.scalar_one_or_none()
                if not candidate:
                    candidate = Candidate(
                        hh_resume_id=candidate_hh_resume_id,
                        full_name=candidate_full_name
                    )
                    db.add(candidate)
                    try:
                        await db.flush() # Пытаемся сохранить, чтобы получить ID
                    except Exception:
                        await db.rollback() # Если кто-то уже создал его секунду назад
                        candidate_result = await db.execute(
                            select(Candidate).filter(Candidate.hh_resume_id == candidate_hh_resume_id)
                        )
                        candidate = candidate_result.scalar_one_or_none()

                await db.flush() # Чтобы получить ID кандидата

                response_created_at_str = resp.get('created_at')
                response_created_at_dt = None
                if response_created_at_str:
                    try:
                        response_created_at_dt = datetime.datetime.fromisoformat(response_created_at_str)
                    except (ValueError, TypeError):
                        scan_logger.warning(f"Не удалось распознать дату отклика: {response_created_at_str}")
                # <<< КОНЕЦ ИСПРАВЛЕНИЯ >>>

                # Создаем диалог
                dialogue = Dialogue(
                    hh_response_id=response_id,
                    candidate_id=candidate.id,
                    vacancy_id=vacancy_in_db.id,
                    recruiter_id=recruiter_id,
                    status='new',
                    dialogue_state='initial_processing',
                    response_created_at=response_created_at_dt # <<< ИСПОЛЬЗУЕМ ОБЪЕКТ DATETIME
                )
                db.add(dialogue)

                # --- КРИТИЧЕСКИЙ МОМЕНТ: ПЕРЕМЕЩЕНИЕ ---
                # Сначала перемещаем, чтобы зафиксировать намерение
                await hh_api.move_response_to_folder(recruiter, db, response_id, 'consider')

                # СПИСАНИЕ СРЕДСТВ + СТАТИСТИКА
                cost = settings.cost_per_dialogue
                settings.balance -= cost
                settings.total_spent_on_dialogues += cost # Увеличиваем счетчик диалогов

                # Пытаемся получить сообщения, но ошибка здесь НЕ ДОЛЖНА отменять создание диалога
                try:
                    messages_data = await hh_api.get_messages(recruiter, db, resp['messages_url'])
                    messages = [
                        {
                            'message_id': str(m.get('id')),
                            'role': 'user',
                            'content': m['text'],
                            'timestamp_msk': _format_timestamp_to_msk(m.get('created_at')) # <-- ДОБАВЛЕНО
                        }
                        for m in messages_data if m.get('text')
                    ]
                except Exception as msg_err:
                    scan_logger.error(f"Ошибка получения сообщений для {response_id}: {msg_err}. Использую заглушку.")
                    messages = []

                if not messages:
                    # Если сопроводительного нет, берем время самого отклика
                    now_msk = datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK')
                    messages = [{
                        'message_id': f'no_msg_{response_id}',
                        'role': 'user',
                        'content': "[SYSTEM COMMAND] Кандидат откликнулся без сопроводительного письма. Поздоровайся и предложи задать вопросы",
                        'timestamp_msk': _format_timestamp_to_msk(resp.get('created_at', now_msk)) # <-- ДОБАВЛЕНО
                    }]

                dialogue.pending_messages = messages
                dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)

                await statistics_manager.update_stats(db, vacancy_in_db.id, responses=1, started_dialogs=1)

                # --- ВАЖНО: КОММИТИМ СРАЗУ ДЛЯ КАЖДОГО КАНДИДАТА ---
                # Это гарантирует, что если мы перенесли его в consider, он сохранится в БД
                await db.commit()
                scan_logger.info(
                    f"✅ Диалог {response_id} успешно сохранен в БД."
                )

                # Проверка лимита для уведомления
                # УВЕДОМЛЕНИЕ О НИЗКОМ БАЛАНСЕ
                if settings.balance < settings.low_balance_threshold and not settings.low_limit_notified:
                    asyncio.create_task(send_system_alert(
                        f"⚠️ Внимание! Баланс ниже {settings.low_balance_threshold} руб. "
                        f"Текущий остаток: {settings.balance} руб.", alert_type="balance"
                    ))
                    settings.low_limit_notified = True

                # Если баланс пополнили выше порога, сбрасываем флаг (опционально, но удобно)
                if settings.balance >= settings.low_balance_threshold:
                    settings.low_limit_notified = False

            except Exception as e:
                err_logger = locals().get('scan_logger', rec_logger)
                
                err_logger.error(
                    f"Ошибка при обработке отклика: {e}", 
                    exc_info=True,
                    extra={"action": "process_new_response_error"}
                )
                await db.rollback() 
                continue

    except Exception as e:
        rec_logger.error(f"Критическая ошибка в process_new_responses: {e}", exc_info=True)
    finally:
        rec_logger.debug(f"process_new_responses завершено за {time.monotonic() - function_start_time:.2f}s")



async def process_ongoing_responses(recruiter_id: int, vacancy_ids: list, db: AsyncSession):
    """Этап 2: Ищет новые сообщения в папках 'Подумать' и 'Собеседование'."""
    function_start_time = time.monotonic()

    recruiter = None
    recruiter_name_for_logging = f"ID {recruiter_id}" # Значение по умолчанию на случай, если рекрутер не найден
    # 1. Получаем рекрутера
    recruiter = await db.get(TrackedRecruiter, recruiter_id)
    
    # 2. Создаем логгер уровня Рекрутера
    rec_logger = logging.LoggerAdapter(logger, {
        "recruiter_id": recruiter_id,
        "recruiter_name": recruiter.name if recruiter else "Unknown",
        "worker": "scanner"
    })
    try:
        
        if not recruiter:
            rec_logger.warning(f"process_ongoing_responses: Рекрутер с ID {recruiter_id} не найден.")
            return

        # --- ЗАГРУЗКА ЗДЕСЬ ---
        recruiter_name_for_logging = recruiter.name
        # --- КОНЕЦ ЗАГРУЗКИ ---
        cutoff_date = recruiter.created_at or (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))

        if not vacancy_ids:
            rec_logger.warning("Этап 2: Нет активных вакансий для проверки обновлений.")
            return

        rec_logger.debug(
            f"Этап 2: Проверка обновлений в папках 'Подумать' и 'Собеседование' "
            f"для {len(vacancy_ids)} вакансий..."
        )

        api_get_responses_gather_start = time.monotonic()

        consider_task = hh_api.get_responses_from_folder(
            recruiter, db, 'consider', vacancy_ids,
            since_datetime=cutoff_date,
            check_for_updates=True
        )
        interview_task = hh_api.get_responses_from_folder(
            recruiter, db, 'interview', vacancy_ids,
            since_datetime=cutoff_date,
            check_for_updates=True
        )

        # Выполняем запросы параллельно
        consider_results, interview_results = await asyncio.gather(consider_task, interview_task)
        
        rec_logger.debug(
            f"API calls finished. Found {len(consider_results)} in consider, {len(interview_results)} in interview.",
            extra={"api_duration": time.monotonic() - api_get_responses_gather_start}
        )

        # Объединяем результаты в один список, помечая источник
        # Каждая запись: (название_папки, (данные_отклика, id_вакансии))
        tagged_responses = [('consider', item) for item in consider_results]
        tagged_responses.extend([('interview', item) for item in interview_results])

        for folder_name, (resp, _) in tagged_responses:
            response_id = resp.get('id')

            if not response_id or (TEST_NEGOTIATION_ID and response_id != TEST_NEGOTIATION_ID):
                continue

            dialogue_result = await db.execute(
                select(Dialogue).filter_by(hh_response_id=response_id).with_for_update()
            )
            dialogue = dialogue_result.scalar_one_or_none()
            
            if not dialogue:
                logger.debug(f"Найдено обновление для отклика {response_id}, которого нет в нашей БД. Пропускаем.")
                continue
            # 3. СОЗДАЕМ ЛОГГЕР УРОВНЯ ДИАЛОГА
            # Теперь все логи ниже будут содержать ID диалога и вакансии
            ctx_logger = logging.LoggerAdapter(logger, {
                "recruiter_id": recruiter_id,
                "recruiter_name": recruiter_name_for_logging,
                "dialogue_id": dialogue.id,
                "hh_response_id": response_id,
                "vacancy_id": dialogue.vacancy_id,
                "folder": folder_name,
                "worker": "scanner"
            })
            # --- КРИТИЧЕСКОЕ ИЗМЕНЕНИЕ: ПРОВЕРКА ПАПКИ ИНТЕРВЬЮ ---
            if folder_name == 'interview':
                # Добавляем проверку: не трогать, если это 'forwarded_to_researcher' или уже 'post_qualification_chat'
                if dialogue.dialogue_state not in ['post_qualification_chat', 'forwarded_to_researcher']:
                    ctx_logger.debug(f"[{response_id}] Обнаружен в папке 'interview'. Принудительный стейт: post_qualification_chat.")
                    dialogue.dialogue_state = 'post_qualification_chat'
                    
            # -----------------------------------------------------

            api_get_messages_start = time.monotonic()
            all_messages_from_api = await hh_api.get_messages(recruiter, db, resp['messages_url'])
            
            ctx_logger.debug(
                f"[Recruiter {recruiter.name}, Dialogue {response_id}] "
                f"API get_messages took: {time.monotonic() - api_get_messages_start:.2f} sec."
            )

            saved_message_ids = {str(h.get('message_id')) for h in (dialogue.history or [])}
            pending_message_ids = {
                str(p.get('message_id'))
                for p in (dialogue.pending_messages or [])
                if isinstance(p, dict)
            }
            seen_ids = saved_message_ids.union(pending_message_ids)

            new_msgs = [
                {
                    'message_id': str(msg.get('id')),
                    'role': 'user',
                    'content': msg['text'],
                    'timestamp_msk': _format_timestamp_to_msk(msg.get('created_at'))
                }
                for msg in all_messages_from_api
                if (msg.get('text') and
                    str(msg.get('id')) not in seen_ids and
                    msg.get('author', {}).get('participant_type') == 'applicant')
            ]

            if new_msgs:
                if dialogue.reminder_level > 0:
                    dialogue.reminder_level = 0

                dialogue.pending_messages = (dialogue.pending_messages or []) + new_msgs
                dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)

                msg_count = len(new_msgs)
                MESSAGES_RECEIVED_TOTAL.labels(recruiter_id=str(recruiter_id)).inc(msg_count) # <--- МЕТРИКА

                ctx_logger.info(
                    f"Добавлено {msg_count} новых сообщений в диалог {response_id} 📩📩📩.",
                    extra={"new_messages_count": msg_count, "action": "messages_received"}
                )

        await db.flush()
        await db.commit()
        
    except Exception as e:
        # Используем rec_logger для общих ошибок
        rec_logger.error(
            f"Error in process_ongoing_responses: {e}", 
            exc_info=True,
            extra={"action": "ongoing_scan_error"}
        )
        await db.rollback()
        raise
    finally:
        rec_logger.debug(
            f"process_ongoing_responses finished",
            extra={"total_duration": time.monotonic() - function_start_time}
        )


async def handle_scanner_recruiter(rec_id: int):
    """Только сканирование для одного рекрутера с защитой от параллелизма"""
    # Создаем логгер верхнего уровня для ошибок
    rec_logger = logging.LoggerAdapter(logger, {"recruiter_id": rec_id, "worker": "scanner"})
    
    async with SessionLocal() as db:
        try:
            # Пытаемся заблокировать строку рекрутера. 
            stmt = select(TrackedRecruiter).filter_by(id=rec_id).with_for_update(skip_locked=True)
            result = await db.execute(stmt)
            recruiter = result.scalar_one_or_none()

            if not recruiter:
                # Если рекрутер заблокирован другим процессом, просто выходим
                return

            # Передаем db!
            active_vacancies = await get_all_active_vacancies_for_recruiter(rec_id, db)
            if not active_vacancies:
                return
            vacancy_ids = [v['id'] for v in active_vacancies]
            
            # ВАЖНО: Запускаем последовательно и передаем db!
            # Убираем gather, так как нельзя использовать одну сессию параллельно
            await process_new_responses(rec_id, vacancy_ids, db)
            await process_ongoing_responses(rec_id, vacancy_ids, db)
            
            await db.commit()
            
        except Exception as e:
            # ИСПОЛЬЗУЕМ rec_logger
            rec_logger.error(
                f"Ошибка при блокировке/сканировании рекрутера: {e}",
                exc_info=True,
                extra={"action": "lock_recruiter_error"}
            )
            await db.rollback()
# В начало файла добавь импорт argparse, если его нет (в твоем косте он есть вверху)

async def main(target_recruiter_ids=None): # Добавь аргумент
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    logger.info(f"HH-Scanner запущен для рекрутеров: {target_recruiter_ids if target_recruiter_ids else 'Всех'}")

    try:
        while not shutdown_requested:
            async with SessionLocal() as db:
                # Фильтруем рекрутеров, если переданы ID
                query = select(TrackedRecruiter.id)
                if target_recruiter_ids:
                    query = query.filter(TrackedRecruiter.id.in_(target_recruiter_ids))
                
                result = await db.execute(query)
                recruiter_ids = result.scalars().all()

            if not recruiter_ids:
                logger.debug("Нет рекрутеров для сканирования. Жду...")
                await asyncio.sleep(10)
                continue

            semaphore = asyncio.Semaphore(MAX_CONCURRENT_RECRUITERS)
            
            async def run_with_sem(r_id):
                async with semaphore:
                    try:
                        await handle_scanner_recruiter(r_id)
                    except Exception as e:
                        logger.error(f"Scanner error for recruiter {r_id}: {e}")

            await asyncio.gather(*[run_with_sem(r_id) for r_id in recruiter_ids])
            
            # Важно: пауза между циклами сканирования, чтобы не спамить API HH
            await asyncio.sleep(0) 
    finally:
        await hh_api_real.close_api_client()
        await close_redis()
        logger.info("HH-Scanner остановлен.")

if __name__ == "__main__":
    setup_logging(log_filename="hh_scanner.log")
    load_dotenv()
    init_sentry()
    start_metrics_server(8000)
    
    parser = argparse.ArgumentParser(description="HH Scanner Service")
    parser.add_argument('--recruiter_ids', nargs='+', type=int, help='List of recruiter IDs')
    args = parser.parse_args()

    try:
        asyncio.run(main(target_recruiter_ids=args.recruiter_ids))
    except (KeyboardInterrupt, SystemExit):
        pass