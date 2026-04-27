#run_hh_worker.py

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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from hr_bot.utils.analytics import log_analytics_event
from decimal import Decimal
import difflib
from hr_bot.utils.data_verification_notifier import send_cost_alert
import re
from hr_bot.utils.data_verification_notifier import send_date_conflict_alert
from hr_bot.db.models import InterviewFollowup, RescheduledNotificationQueue, DeclinedInterviewNotificationQueue, RejectedNotificationQueue
# ... другие импорты
from hr_bot.utils.data_verification_notifier import send_verification_alert # <--- НОВЫЙ ИМПОРТ
from hr_bot.utils.redis_lock import close_redis 
from hr_bot.utils.sentry_config import init_sentry
from hr_bot.utils.logger_config import setup_logging
from hr_bot.db.models import SessionLocal, Dialogue, Candidate, Vacancy, NotificationQueue, TrackedRecruiter, AppSettings, InactiveNotificationQueue, RejectedNotificationQueue, InterviewReminder, LlmUsageLog, DeclinedInterviewNotificationQueue, InterviewFollowup, RescheduledNotificationQueue
from hr_bot.services import hh_api_real as hh_api
from hr_bot.services import knowledge_base
from hr_bot.services import llm_handler
from hr_bot.db import statistics_manager
from hr_bot.utils.metrics import start_metrics_server
from hr_bot.utils.pii_masker import extract_and_mask_pii
from hr_bot.utils.system_notifier import send_system_alert
from hr_bot.utils.resh_in_code import check_candidate_eligibility, is_candidate_profile_complete
import signal
import sys
from hr_bot.utils.redis_lock import acquire_lock, release_lock
from hr_bot.services.llm_handler import cleanup, LlmValidationError
from sqlalchemy import func, select, delete, update
from hr_bot.services import interview_reminder_manager
from hr_bot.utils.metrics import start_metrics_server, E2E_RESPONSE_LATENCY, MESSAGES_SENT_TOTAL
from sqlalchemy import func, select, delete, and_, case, literal # <--- Добавьте case и literal
# ... остальные импорты

logger = logging.getLogger(__name__)

#CUTOFF_DATE_FOR_RESPONSES = datetime.datetime(2025, 11, 13, 11, 0, 0, tzinfo=datetime.timezone.utc)
#CUTOFF_DATE_FOR_RESPONSES = datetime.datetime(2025, 11, 16, 13, 56, 0, tzinfo=datetime.timezone.utc)
# --- КОНФИГУРАЦИЯ ---
DEBOUNCE_DELAY_SECONDS = 0
CYCLE_PAUSE_SECONDS = 3
TEST_NEGOTIATION_ID = None # Установите в None для боевого режима
MAX_CONCURRENT_RECRUITERS = 5 #одновременно рекрутеров
MAX_CONCURRENT_DIALOGUES = 10 #одновременно диалогов
VACANCY_CACHE_DURATION_MINUTES = 2 # Время кэширования списка вакансий для рекрутера
# Новые константы для окна доставки напоминаний (местное время сервера)
REMINDER_START_HOUR_LOCAL = 9  # Например, 9:00 утра
REMINDER_END_HOUR_LOCAL = 20 # Например, 20:00 вечера (напоминания отправляются до 19:59 включительно)

# Цены за 1 миллион токенов ($)
MODEL_PRICING = {
    "gpt-4o-mini": {
        "input": 0.150,
        "output": 0.600,
    },
    "gpt-4o": {  # Тарифы для "умной" модели (валидация дат)
        "input": 2.500,
        "output": 10.000,
    }
}

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

async def _check_restrictions_via_llm(db: AsyncSession, dialogue: Dialogue, recruiter: TrackedRecruiter, pending_messages: list, history_messages: list, log_extra: dict) -> bool:
    """
    Проверяет историю диалога на наличие критических ограничений (здоровье, язык) через LLM.
    Возвращает True, если найдены ограничения (нужен отказ), иначе False.
    """
    # 1. Формируем чистый текст диалога
    clean_history_lines = []
    for m in history_messages:
        content = m.get('content', '')
        if isinstance(content, str) and content.startswith('[SYSTEM COMMAND]'):
            continue
        role_label = "Кандидат" if m.get('role') == 'user' else "Бот"
        clean_history_lines.append(f"{role_label}: {content}")

    full_text = "\n".join(clean_history_lines)

    restriction_prompt = (
        "Ты — эксперт службы безопасности и охраны труда. Твоя задача — проанализировать диалог и определить, "
        "сообщил ли КАНДИДАТ о наличии серьезных ограничений, которые делают невозможной работу на складе или в магазине.\n\n"
        "КРИТЕРИИ ОТКАЗА (ОГРАНИЧЕНИЯ):\n"
        "1. СЛУХ: Полная глухота, сильная тугоухость, использование слухового аппарата.\n"
        "2. ЗРЕНИЕ: Слепота, критически плохое зрение (слабовидящий).\n"
        "3. РЕЧЬ: Полное отсутствие речи или тяжелые нарушения.\n"
        "4. ЯЗЫК: Кандидат прямо говорит, что не понимает русский язык, не говорит на нем или требует переводчика.\n"
        "5. ЗДОРОВЬЕ: Наличие инвалидности или медицинских противопоказаний к физическому труду.\n\n"

        "ПРАВИЛА АНАЛИЗА:\n"
        "- Игнорируй мелкие жалобы (например, 'устал' или 'болит голова сегодня').\n"
        "- Верни 'has_restrictions': true только если есть ПРЯМОЕ упоминание одного из критериев выше.\n"
        "- Если кандидат говорит 'у меня нет проблем со здоровьем' — это 'has_restrictions': false.\n\n"
        "Ответ строго в формате JSON:\n"
        "{\n"
        "  \"reasoning\": \"краткое обоснование на русском языке\",\n"
        "  \"has_restrictions\": true/false\n"
        "}"
    )

    attempt_tracker = []
    try:
        response = await llm_handler.get_bot_response(
            system_prompt=restriction_prompt,
            dialogue_history=[],
            user_message=f"ИСТОРИЯ ДИАЛОГА:\n{full_text}",
            current_datetime_utc=datetime.datetime.now(datetime.timezone.utc),
            attempt_tracker=attempt_tracker,
            skip_instructions=True,
            extra_context=log_extra
        )

        # Логируем использование (включая ретраи, как в твоем коде)
        if response:
            await _log_llm_usage(db, dialogue, "Restriction_Check", response.get("usage_stats"))
            if len(attempt_tracker) > 1:
                for i in range(len(attempt_tracker) - 1):
                    await _log_llm_usage(db, dialogue, f"Restriction_Check (RETRY #{i+1})")

        parsed = response.get('parsed_response', {})
        return parsed.get("has_restrictions", False)

    except Exception as e:
        # Логируем провальные попытки в БД перед вылетом
        for i in range(len(attempt_tracker)):
            await _log_llm_usage(db, dialogue, f"Restriction_Check (FAILED #{i+1}: {type(e).__name__})")
        
        # --- КРИТИЧЕСКИЙ ОБРАБОТЧИК ---
        await _handle_llm_failure(
            db=db,
            dialogue=dialogue,
            recruiter=recruiter,
            error=e,
            context="Restriction Check LLM",
            pending_msgs=pending_messages,
            ctx_logger=logging.LoggerAdapter(logger, log_extra)
        )
        return None  # Возвращаем None как признак критического сбоя

def _validate_age_in_text(text: str, suggested_age: any) -> bool:
    """
    Проверяет, соответствует ли извлеченный LLM возраст тому, что реально написал пользователь.
    """
    if not suggested_age:
        return False
    
    try:
        age_to_check = int(suggested_age)
    except (ValueError, TypeError):
        return False

    # 1. ИСПРАВЛЕННАЯ проверка.
    # Вместо \b используем (?<!\d) и (?!\d) — это означает "нет цифр слева" и "нет цифр справа".
    # Это позволит найти "51" в строках "51год", "мне 51.", "возраст 51" и т.д., но отсечет "151" или "510".
    if re.search(r'(?<!\d)' + str(age_to_check) + r'(?!\d)', text):
        return True

    # 2. Словарь числительных (оставляем без изменений)
    age_words = {
        14: "четырнадцать", 15: "пятнадцать", 16: "шестнадцать",
        17: "семнадцать", 18: "восемнадцать", 19: "девятнадцать",
        20: "двадцать", 21: "двадцать один", 22: "двадцать два",
        23: "двадцать три", 24: "двадцать четыре", 25: "двадцать пять",
        26: "двадцать шесть", 27: "двадцать семь", 28: "двадцать восемь",
        29: "двадцать девять", 30: "тридцать"
    }

    if age_to_check in age_words:
        word = age_words[age_to_check]
        if word in text.lower():
            return True

    # 3. Дополнительная проверка (оставляем без изменений)
    all_numbers_in_text = re.findall(r'\b(1[4-9]|[2-6][0-9]|70)\b', text)
    
    if all_numbers_in_text and str(age_to_check) not in all_numbers_in_text:
        # Если нашли числа, но нашего среди них нет, пробуем еще раз с мягким поиском
        # (на случай если re.findall выше что-то упустил из-за границ слов)
        if re.search(r'(?<!\d)' + str(age_to_check) + r'(?!\d)', text):
            return True
        logger.warning(f"AGE VALIDATION FAILED: LLM suggested {age_to_check}, but found {all_numbers_in_text} in text.")
        return False

    if not all_numbers_in_text:
        # Если чисел вообще не нашли (например "мне полтинник"), а LLM нашла - это подозрительно, но
        # если мы прошли проверку №1, мы бы уже вернули True.
        return False

    return True

try:
    SPB_TIMEZONE = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    logger.critical("Часовой пояс 'Europe/Moscow' не найден. Убедитесь, что система имеет актуальную базу данных часовых поясов (tzdata).")
    SPB_TIMEZONE = None # Fallback

# Флаг для graceful shutdown
shutdown_requested = False



def signal_handler(sig, frame):
    """Обработчик сигналов для graceful shutdown"""
    global shutdown_requested
    logger.info("Получен сигнал остановки. Завершаем работу...")
    shutdown_requested = True


    
async def _log_llm_usage(db: AsyncSession, dialogue: Dialogue, context: str, usage_stats: dict = None, model_name: str = "gpt-4o-mini"):
    """
    Универсальная функция для подсчета токенов и стоимости.
    Использует текущую сессию БД, чтобы избежать блокировок.
    """
    try:
        # 1. Выбор тарифа и расчеты
        pricing = llm_handler.MODEL_PRICING.get(model_name, llm_handler.MODEL_PRICING["gpt-4o-mini"])
        stats = usage_stats or {}
        p_tokens = stats.get('prompt_tokens', 0)
        c_tokens = stats.get('completion_tokens', 0)
        cached_tokens = stats.get('cached_tokens', 0)
        total_tokens = stats.get('total_tokens', 0)

        non_cached_input = max(0, p_tokens - cached_tokens)
        cost_input_regular = (non_cached_input / 1_000_000) * pricing["input"]
        cost_input_cached = (cached_tokens / 1_000_000) * (pricing["input"] / 2) 
        cost_output = (c_tokens / 1_000_000) * pricing["output"]
        total_call_cost = Decimal(str(cost_input_regular + cost_input_cached + cost_output))

        # 2. Создание записи лога в ТЕКУЩЕЙ сессии
        usage_log = LlmUsageLog(
            dialogue_id=dialogue.id,
            dialogue_state_at_call=f"{context} ({model_name})",
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            cost=total_call_cost
        )
        db.add(usage_log)

        # 3. Обновление счетчиков объекта (в памяти текущей сессии)
        if total_tokens > 0:
            dialogue.total_prompt_tokens = (dialogue.total_prompt_tokens or 0) + p_tokens
            dialogue.total_completion_tokens = (dialogue.total_completion_tokens or 0) + c_tokens
            dialogue.total_cached_tokens = (dialogue.total_cached_tokens or 0) + cached_tokens
            dialogue.total_cost = (dialogue.total_cost or Decimal('0.0')) + total_call_cost

    except Exception as e:
        logger.error(f"Ошибка при подготовке лога LLM ({context}): {e}")

async def _handle_llm_failure(db: AsyncSession, dialogue: Dialogue, recruiter: TrackedRecruiter, error: Exception, context: str, pending_msgs: list, ctx_logger):
    """
    Критический сбой: LLM не ответила за 6 попыток. 
    1. Шлем алерт админам с деталями.
    2. Шлем заглушку кандидату.
    3. Чистим очередь (pending_messages), чтобы не зациклиться.
    """
    ctx_logger.critical(f"🛑 КРИТИЧЕСКИЙ СБОЙ LLM ({context}): {type(error).__name__}: {error}")
    
    # 1. Формируем текст сообщений от кандидата
    messages_text = []
    if pending_msgs:
        for m in pending_msgs:
            content = m.get('content', '') if isinstance(m, dict) else str(m)
            messages_text.append(f"- {content}")
    
    pending_content_str = "\n".join(messages_text) if messages_text else "Нет данных"

    # 2. Шлем алерт в ТГ
    alert_text = (
        f"‼️ **КРИТИЧЕСКИЙ СБОЙ LLM**\n\n"
        f"**Контекст:** {context}\n"
        f"**Диалог ID:** `{dialogue.id}`\n"
        f"**HH Response ID:** `{dialogue.hh_response_id}`\n"
        f"**Рекрутер:** {recruiter.name if recruiter else 'Неизвестен'}\n"
        f"**Вакансия:** {dialogue.vacancy.title if dialogue.vacancy else 'Неизвестна'}\n\n"
        f"**Входящие сообщения:**\n{pending_content_str}\n\n"
        f"**Ошибка:** `{type(error).__name__}: {error}`\n\n"
        f"✅ **Действие:** Очередь очищена, кандидату отправлена заглушка. Требуется ручное вмешательство."
    )
    await send_system_alert(alert_text, alert_type="admin_only")

    # 3. Шлем заглушку кандидату (через HH API)
    fallback_msg = "Что-то с hh, вижу, что написали, но сообщение не видно. Передам специалисту. С вами свяжутся"
    try:
        await hh_api.send_message(recruiter, db, dialogue.hh_response_id, fallback_msg)
    except Exception as send_err:
        ctx_logger.error(f"Не удалось отправить заглушку кандидату: {send_err}")

    # 4. Чистим очередь и фиксируем в БД
    dialogue.pending_messages = None
    dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)

    try:
        await db.commit()
        ctx_logger.info("Очередь сообщений успешно очищена после сбоя LLM.")
    except Exception as commit_err:
        ctx_logger.error(f"Ошибка при коммите очистки очереди: {commit_err}")
        try:
            await db.rollback()
            ctx_logger.warning("Транзакция откачена после ошибки коммитa.")
        except Exception as rollback_err:
            ctx_logger.critical(f"Ошибка при rollback: {rollback_err}")
        
        # Шлем дополнительный алерт, если не удалось сохранить очистку
        commit_alert = (
            f"🔴 **КРИТИЧЕСКАЯ ОШИБКА КОММИТА**\n\n"
            f"Не удалось очистить очередь после сбоя LLM.\n"
            f"**Диалог ID:** `{dialogue.id}`\n"
            f"**Ошибка коммита:** `{type(commit_err).__name__}: {commit_err}`\n\n"
            f"⚠️ Требуется ручное вмешательство в БД!"
        )
        await send_system_alert(commit_alert, alert_type="admin_only")

async def _verify_date_audit(db: AsyncSession, dialogue: Dialogue, recruiter: TrackedRecruiter, pending_messages: list, suggested_date: str, history_messages: list, calendar_context: str, log_extra: dict) -> tuple:
    """
    Техническая проверка даты (Аудит). Возвращает исправленную дату в формате YYYY-MM-DD.
    """
    # 1. Фильтруем системные команды и сопоставляем роли для понимания GPT-4o
    clean_history_lines = []
    for m in history_messages:
        content = m.get('content', '')
        # Пропускаем технические команды
        if isinstance(content, str) and content.startswith('[SYSTEM COMMAND]'):
            continue
        
        # Определяем человекочитаемую роль
        role_label = "Кандидат" if m.get('role') == 'user' else "Бот"
        clean_history_lines.append(f"{role_label}: {content}")

    # 2. Берем последние 10 сообщений для анализа даты
    recent_text = "\n".join(clean_history_lines[-5:])
    
    verify_prompt = (
        f"Ты — строгий технический аудитор системы записи. Твоя задача — проверить, соответствует ли дата, предложенная первой моделью, ЖЕЛАНИЮ КАНДИДАТА.\n\n"
        f"[ДИНАМИЧЕСКИЙ КАЛЕНДАРЬ]\n{calendar_context}\n\n"
        f"[ВВОДНЫЕ ДАННЫЕ]\n"
        f"Первая модель предлагает записать на: {suggested_date}\n\n"
        f"⚠️ КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА АНАЛИЗА (ЧИТАЙ ВНИМАТЕЛЬНО):\n"
        f"1. АБСОЛЮТНЫЙ ПРИОРИТЕТ — у ПОСЛЕДНЕГО сообщения кандидата.\n"
        f"   - Если ранее договорились на одну дату, но в конце кандидат спросил 'А можно завтра?' или 'Давайте в пятницу' — значит, он ПЕРЕДУМАЛ.\n"
        f"   - В этом случае ты ОБЯЗАН вернуть новую дату (завтра/пятницу), а не ту, что была согласована ранее.\n\n"
        f"2. Сверь дату с календарем:\n"
        f"   - Если кандидат говорит например 'завтра' — ищи в календаре строку с меткой ЗАВТРА.\n"
        f"   - Если кандидат говорит день недели (напр. Вторник) — бери ближайший Вторник из таблицы (если не сказано 'следующий').\n\n"
        f"3. Вердикт:\n"
        f"   - Если {suggested_date} совпадает с ПОСЛЕДНИМ желанием кандидата — верни её.\n"
        f"   - Если первая модель проигнорировала смену даты кандидатом — верни ПРАВИЛЬНУЮ дату.\n"
        f"   - Если кандидат в диалоге вообще не называл дату или отказался называть — верни 'none'.\n\n"
        f"Ответ строго в формате JSON:\n"
        f"{{\n"
        f'  "reasoning": "Обоснование. Пример: Кандидат сначала согласился на 20-е число, но в последнем сообщении спросил про завтра (21-е). Первая модель ошиблась, оставив 20-е. Не более 2 предложений, пиши кратко",\n'
        f'  "correct_date": "YYYY-MM-DD или none"\n'
        f"}}"
    )

    verify_attempts = []
    try:
        # Вызываем LLM через твой обработчик
        response = await llm_handler.get_smart_bot_response(
            system_prompt=verify_prompt,
            dialogue_history=[],
            user_message=f"ИСТОРИЯ ДИАЛОГА:\n{recent_text}",
            current_datetime_utc=datetime.datetime.now(datetime.timezone.utc),
            attempt_tracker=verify_attempts,
            skip_instructions=True,
            extra_context=log_extra
        )

        if response and 'usage_stats' in response:
            await _log_llm_usage(db, dialogue, "Date_Audit_Call", response['usage_stats'], model_name="gpt-4o")

        parsed = response.get('parsed_response', {})
        return parsed.get("correct_date", suggested_date), parsed.get("reasoning", "Без обоснования")
    except Exception as e:
        # Логируем провальные попытки
        for i in range(len(verify_attempts)):
            await _log_llm_usage(db, dialogue, f"Date_Audit (FAILED #{i+1}: {type(e).__name__})", model_name="gpt-4o")
        
        # --- КРИТИЧЕСКИЙ ОБРАБОТЧИК ---
        await _handle_llm_failure(
            db=db,
            dialogue=dialogue,
            recruiter=recruiter,
            error=e,
            context="Date Audit LLM",
            pending_msgs=pending_messages,
            ctx_logger=logging.LoggerAdapter(logger, log_extra)
        )
        return None, None # Возвращаем кортеж с None
    

def _log_missing_vacancy(title: str, city: str):
    """
    Записывает ненайденную вакансию в файл missing_vacancies.txt.
    Проверяет файл на наличие дубликатов перед записью.
    """
    file_path = "missing_vacancies.txt"
    # Формируем строку для записи
    entry = f"{title} | {city}"
    
    try:
        # Читаем существующие записи, чтобы избежать дублей
        existing_lines = set()
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                # Собираем set очищенных строк
                existing_lines = {line.strip() for line in f}
        except FileNotFoundError:
            # Если файла нет, просто создадим его при записи
            pass

        # Если такой записи еще нет, добавляем
        if entry not in existing_lines:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(f"{entry}\n")
                
    except Exception as e:
        # Логируем ошибку, но не ломаем работу бота
        logger.error(f"Ошибка при записи в missing_vacancies.txt: {e}")


def _find_relevant_vacancy(prompt_library: dict, vacancy_title: str, vacancy_city: str, context_logger=None) -> str:
    """
    Поиск вакансии по принципу BEST MATCH с логикой исключения по критическим словам.
    """
    local_log = context_logger or logger
    def normalize_text(text: str) -> str:
        if not text: 
            return ""
        text = text.lower().replace("ё", "е")
        text = re.sub(r"[^\w\s]", " ", text)
        return " ".join(text.split())

    def get_title_similarity(input_str: str, db_str: str) -> float:
        input_words = set(normalize_text(input_str).split())
        db_words = set(normalize_text(db_str).split())

        intersection = input_words.intersection(db_words)
        if not intersection:
            return 0.0

        critical_words = {
            'старший', 'младший', 'ночной', 'неполный', 'мобильный', 
            'администратор', 'директор', 'товаровед', 'универсал', 
            'пекарь', 'повар', 'кафе', 'бариста', 'кухни', 'сборщик', 'кассир'
        }

        extra_in_db = db_words - input_words
        for word in extra_in_db:
            if word in critical_words:
                return 0.0

        extra_in_input = input_words - db_words
        for word in extra_in_input:
             if word in critical_words:
                return 0.0

        recall = len(intersection) / len(db_words)
        precision = len(intersection) / len(input_words)
        
        return (recall * 0.4) + (precision * 0.6)

    # Логика обработки городов
    norm_input_city = normalize_text(vacancy_city)
    city_synonyms = {
        "спб": "санкт петербург",
        "питер": "санкт петербург",
        "ленобласть": "ленинградская область"
    }
    for short, full in city_synonyms.items():
        if short in norm_input_city:
            norm_input_city = norm_input_city.replace(short, full)

    best_match_vacancy = None
    best_match_score = 0.0
    matched_city_name = "не определен" # Переменная для хранения города из БД

    # Перебираем вакансии из библиотеки
    for vacancy in prompt_library.get("vacancies", []):
        
        # --- ПРОВЕРКА ГОРОДА ---
        city_match = False
        current_vacancy_matched_city = None
        
        db_cities_raw = vacancy.get("cities", [])
        # Создаем список нормализованных городов для сравнения
        db_cities_norm = [normalize_text(c) for c in db_cities_raw]
        
        for i, db_city_norm in enumerate(db_cities_norm):
            if norm_input_city in db_city_norm or db_city_norm in norm_input_city:
                city_match = True
                # Запоминаем оригинальное название города из БД (например, "Бугры (СПб)")
                current_vacancy_matched_city = db_cities_raw[i]
                break
        
        if not city_match:
            continue

        # --- ПРОВЕРКА НАЗВАНИЯ ---
        max_title_score = 0.0
        for db_title in vacancy.get("titles", []):
            score = get_title_similarity(vacancy_title, db_title)
            if score > max_title_score:
                max_title_score = score

        if max_title_score < 0.4:
            continue

        # Запоминаем лучший результат
        if max_title_score > best_match_score:
            best_match_score = max_title_score
            best_match_vacancy = vacancy
            matched_city_name = current_vacancy_matched_city

    # --- РЕЗУЛЬТАТ С РАСШИРЕННЫМ ЛОГОМ ---
    if best_match_vacancy:
        local_log.info(f"✅ ВЫБРАНО ОПИСАНИЕ | HH: [{vacancy_title}]...")
        return best_match_vacancy.get("description", "")

    # Если ничего не нашли
    local_log.warning(
        "🤡 Описание не найдено | HH title='%s' | HH city='%s'",
        vacancy_title,
        vacancy_city,
    )
    _log_missing_vacancy(vacancy_title, vacancy_city)

    return (
        "ОПИСАНИЕ ВАКАНСИИ НЕ НАЙДЕНО. "
        "Отвечай на вопросы кандидата на основе общей информации из FAQ."
    )

def _generate_calendar_context() -> str:
    """
    Генерирует текстовый блок с календарем и правилами работы с датами.
    """
    moscow_tz = ZoneInfo("Europe/Moscow")
    current_datetime_utc = datetime.datetime.now(moscow_tz)
    
    weekdays_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    months_ru = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря"
    ]
    
    # Склонения для "следующий"
    weekday_next_form = {
        "Понедельник": "Следующий понедельник",
        "Вторник": "Следующий вторник",
        "Среда": "Следующая среда",
        "Четверг": "Следующий четверг",
        "Пятница": "Следующая пятница",
        "Суббота": "Следующая суббота",
        "Воскресенье": "Следующее воскресенье"
    }

    current_weekday = weekdays_ru[current_datetime_utc.weekday()]
    current_date_str = current_datetime_utc.strftime("%Y.%m.%d")
    current_time_str = current_datetime_utc.strftime("%H:%M")

    calendar_context_lines = []
    
    # Генерируем на 21 день (3 недели)
    for i in range(21):
        date_cursor = current_datetime_utc + datetime.timedelta(days=i)
        wd_idx = date_cursor.weekday()
        wd_name = weekdays_ru[wd_idx]
        
        day = date_cursor.day
        month_name = months_ru[date_cursor.month - 1]
        date_dotted = date_cursor.strftime("%Y.%m.%d")
        
        # Определяем статус (сегодня/завтра/послезавтра или просто день)
        if i == 0:
            status = "сегодня "
            label = wd_name
        elif i == 1:
            status = "завтра "
            label = wd_name
        elif i == 2:
            status = "послезавтра "
            label = wd_name
        elif 7 <= i < 14:
            # Вторая неделя
            status = ""
            label = weekday_next_form[wd_name]
        elif i >= 14:
            # Третья неделя
            status = ""
            label = f"Через неделю в {wd_name.lower()}"
        else:
            status = ""
            label = wd_name
        
        # Формируем строку по вашему шаблону
        # сегодня Понедельник - 26 января - дата: 2026.01.26
        line = f"{status}{label} - {day} {month_name} - дата: {date_dotted}"
        
        # Добавляем маркер текущего дня для модели
        if i == 0:
            line += " ← ТЫ ЗДЕСЬ"
            
        calendar_context_lines.append(line)

    calendar_string = "\n".join(calendar_context_lines)

    calendar_context = (
        f"\n\n[CRITICAL CALENDAR CONTEXT]\n"
        f"ТЕКУЩАЯ ДАТА И ВРЕМЯ (МСК): {current_datetime_utc.strftime('%Y-%m-%d %H:%M')}\n"
        f"СЕГОДНЯ: {current_weekday}, {current_date_str}\n\n"
        f"СЕЙЧАС: {current_time_str} (МСК)\n"
        f"⚠️ ВАЖНО: Ты ОЧЕНЬ ПЛОХО считаешь даты в уме. НИКОГДА НЕ ВЫЧИСЛЯЙ ДАТЫ САМОСТОЯТЕЛЬНО!\n"
        f"Используй ТОЛЬКО эту таблицу (таблица начинается с СЕГОДНЯ и идет на 14 дней вперед):\n\n"
        f"{calendar_string}\n\n"
        f"ПРАВИЛА РАБОТЫ С ДАТАМИ:\n"
        f"1. Кандидат говорит просто день недели ('понедельник', 'вторник'):\n"
        f"   → Найди ПЕРВУЮ строку с этим днем (без слова 'Следующий')\n"
        f"   → Скопируй дату из этой строки\n\n"
        f"2. Если кандидат говорит 'СЛЕДУЮЩИЙ [день недели]' (например, 'следующий понедельник'):\n"
        f"   → Бери такой день из списка выше, где написано 'СЛЕДУЮЩИЙ [день недели]' (например, 'следующий понедельник')\n\n"
        f"   → Скопируй дату из этой строки\n\n"
        f"3. Если кандидат называет день недели, который совпадает с СЕГОДНЯ:\n"
        f"   → ОБЯЗАТЕЛЬНО уточни: 'Вы имеете в виду сегодня или через неделю?'\n\n"
        f"4. Если кандидат говорит 'сегодня', 'завтра', 'послезавтра':\n"
        f"   → Ищи в списке пометку 'СЕГОДНЯ', 'ЗАВТРА' или 'ПОСЛЕЗАВТРА'\n\n"
        f"5. Если кандидат называет дату:\n"
        f"   → Не отказывай ему, ищи в списке эту дату и бери ее\n\n"
        f"6. ВСЕГДА копируй дату ТОЧНО из таблицы в формате YYYY-MM-DD\n"
        f"7. НИКОГДА не изобретай даты сам - только из этой таблицы!\n"
        f"═══════════════════════════════════════════════════════════\n"
        f"ПРИМЕРЫ:\n"
        f"═══════════════════════════════════════════════════════════\n"
        f"Кандидат: 'понедельник' → Ты ищешь 'Понедельник'\n"
        f"Кандидат: 'следующий понедельник' → Ты ищешь строчку 'Следующий понедельник'\n"
        f"Кандидат: 'завтра' → Ты ищешь строчку с пометкой '(ЗАВТРА)'\n"
    )
    return calendar_context




def _generate_calendar_context_2() -> str:
    """
    Генерирует текстовый блок с календарем и правилами работы с датами.
    
    Генерирует расширенный текстовый блок с календарем на 3 недели.
    Формат: сегодня Понедельник - 26 января - дата: 2026.01.26
    """
    moscow_tz = ZoneInfo("Europe/Moscow")
    current_datetime_utc = datetime.datetime.now(moscow_tz)
    
    weekdays_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    months_ru = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря"
    ]


    
    # Склонения для "следующий"
    weekday_next_form = {
        "Понедельник": "Следующий понедельник",
        "Вторник": "Следующий вторник",
        "Среда": "Следующая среда",
        "Четверг": "Следующий четверг",
        "Пятница": "Следующая пятница",
        "Суббота": "Следующая суббота",
        "Воскресенье": "Следующее воскресенье"
    }

    current_weekday = weekdays_ru[current_datetime_utc.weekday()]
    current_date_str = current_datetime_utc.strftime("%Y.%m.%d")
    current_time_str = current_datetime_utc.strftime("%H:%M")

    calendar_context_lines = []

# Заголовок таблицы (очень важно для GPT)
    calendar_context_lines.append(
        "IDX | DATE | WEEKDAY | RELATIVE | HUMAN_LABEL | MARKER"
    )

    for i in range(21):
        date_cursor = current_datetime_utc + datetime.timedelta(days=i)
        wd_idx = date_cursor.weekday()
        wd_name = weekdays_ru[wd_idx]

        day = date_cursor.day
        month_name = months_ru[date_cursor.month - 1]
        date_dotted = date_cursor.strftime("%Y.%m.%d")

        # Определяем статус
        if i == 0:
            relative = "СЕГОДНЯ"
            human_label = f"сегодня {wd_name} - {day} {month_name}"
        elif i == 1:
            relative = "ЗАВТРА"
            human_label = f"завтра {wd_name} - {day} {month_name}"
        elif i == 2:
            relative = "ПОСЛЕЗАВТРА"
            human_label = f"послезавтра {wd_name} - {day} {month_name}"
        elif 7 <= i < 14:
            relative = "СЛЕДУЮЩАЯ_НЕДЕЛЯ"
            human_label = f"{weekday_next_form[wd_name]} - {day} {month_name}"
        elif i >= 14:
            relative = "ЧЕРЕЗ_НЕДЕЛЮ"
            human_label = f"Через неделю в {wd_name.lower()} - {day} {month_name}"
        else:
            relative = ""
            human_label = f"{wd_name} - {day} {month_name}"

        marker = "ТЫ_ЗДЕСЬ" if i == 0 else ""

        line = (
            f"{i} | "
            f"{date_dotted} | "
            f"{wd_name} | "
            f"{relative} | "
            f"{human_label} | "
            f"{marker}"
        )

        calendar_context_lines.append(line)

    calendar_string = "\n".join(calendar_context_lines)

    calendar_context = (
        f"\n\n[CRITICAL CALENDAR CONTEXT]\n"
        f"ТЕКУЩАЯ ДАТА И ВРЕМЯ (МСК): {current_datetime_utc.strftime('%Y-%m-%d %H:%M')}\n"
        f"СЕГОДНЯ: {current_weekday}, {current_date_str}\n\n"
        #f"СЕЙЧАС: {current_time_str} (МСК)\n"
        f"⚠️ ВАЖНО: Ты ОЧЕНЬ ПЛОХО считаешь даты в уме. НИКОГДА НЕ ВЫЧИСЛЯЙ ДАТЫ САМОСТОЯТЕЛЬНО!\n"
        f"Используй ТОЛЬКО эту таблицу (таблица начинается с СЕГОДНЯ и идет на 21 дней вперед):\n\n"
        f"{calendar_string}\n\n"

        f"ОПИСАНИЕ КОЛОНОК:\n"
        f"IDX — порядковый номер строки\n"
        f"DATE — дата (ЕДИНСТВЕННЫЙ источник истины)\n"
        f"WEEKDAY — день недели\n"
        f"RELATIVE — относительный статус дня\n"
        f"HUMAN_LABEL — человекочитаемая подпись\n"
        f"MARKER — специальные метки (например, ТЫ_ЗДЕСЬ)\n\n"

        f"ПРАВИЛА РАБОТЫ С ДАТАМИ:\n"
        f"1. Если кандидат говорит ТОЛЬКО день недели ('понедельник', 'вторник'):\n"
        f"   → Найди ПЕРВУЮ строку, где WEEKDAY совпадает\n"
        f"   → И поле RELATIVE пустое\n"
        f"   → Скопируй DATE\n\n"

        f"2. Если кандидат говорит 'СЛЕДУЮЩИЙ <день недели>' (например, 'следующий понедельник'):\n"
        f"   → Найди строку, где WEEKDAY совпадает\n"
        f"   → И RELATIVE = СЛЕДУЮЩАЯ_НЕДЕЛЯ\n"
        f"   → Скопируй DATE\n\n"

        f"3. Если кандидат говорит 'сегодня':\n"
        f"   → Найди строку, где RELATIVE = СЕГОДНЯ\n"
        f"   → Скопируй DATE\n\n"

        f"4. Если кандидат говорит 'завтра':\n"
        f"   → Найди строку, где RELATIVE = ЗАВТРА\n"
        f"   → Скопируй DATE\n\n"

        f"5. Если кандидат говорит 'послезавтра':\n"
        f"   → Найди строку, где RELATIVE = ПОСЛЕЗАВТРА\n"
        f"   → Скопируй DATE\n\n"

        f"6. Если кандидат называет дату:\n"
        f"   → Найди строку, где DATE совпадает\n"
        f"   → Используй эту DATE\n\n"

        f"7. Если кандидат называет день недели, совпадающий с сегодняшним, но НЕ говорит 'сегодня':\n"
        f"   → Найди строку, где WEEKDAY совпадает\n"
        f"   → И RELATIVE = СЛЕДУЮЩАЯ_НЕДЕЛЯ\n"
        f"   → Скопируй DATE\n\n"

        f"8. ВСЕГДА используй ТОЛЬКО значение из колонки DATE в формате YYYY-MM-DD\n"
        f"9. НИКОГДА не вычисляй даты вручную\n"
        f"10. КАЖДАЯ СТРОКА ТАБЛИЦЫ = ОДИН КАЛЕНДАРНЫЙ ДЕНЬ\n"
        f"11. НЕ ОБЪЕДИНЯЙ СТРОКИ И НЕ СОЗДАВАЙ НОВЫЕ ДАТЫ\n"
        f"═══════════════════════════════════════════════════════════\n"
        f"ПРИМЕРЫ:\n"
        f"═══════════════════════════════════════════════════════════\n"
        f"Кандидат: 'понедельник' → WEEKDAY=Понедельник, RELATIVE пусто → DATE\n"
        f"Кандидат: 'следующий понедельник' → WEEKDAY=Понедельник, RELATIVE=СЛЕДУЮЩАЯ_НЕДЕЛЯ → DATE\n"
        f"Кандидат: 'завтра' → RELATIVE=ЗАВТРА → DATE\n"
    )

    return calendar_context

def _assemble_dynamic_prompt(prompt_library: dict, dialogue_state: str, user_message: str, vacancy_description: str, city_name: str, full_address: str, vacancy_title: str) -> str:
    """Собирает динамический системный промпт из блоков библиотеки (упрощенная версия с единым FAQ)."""

    required_blocks = ['#ROLE_AND_STYLE#']
    # --- ЛОГИКА ВЫБОРА БЛОКА АДРЕСА ---
    target_cities = ['санкт-петербург', 'мурино', 'кудрово']
    is_spb_city = any(city in (city_name or "").lower() for city in target_cities)
    
    current_title_lower = (vacancy_title or "").lower()

    # Список 1: Исключения для Питера (идут в #ADRESS_OTHER#)
    excluded_vacancies = [
        'повар-пекарь', 'повар неполный день', 'повар', 'бариста', 
        'уборщик', 'уборщица', 'помошник повара'
    ]
    
    # Список 2: Сборщики для Питера (идут в #ADRESS_SPB_1#)
    sborshik_vacancies = [
        'сборщик', 'комплектовщик', 'упаковщик'
    ]

    is_excluded_vacancy = any(phrase in current_title_lower for phrase in excluded_vacancies)
    is_sborshik_vacancy = any(phrase in current_title_lower for phrase in sborshik_vacancies)

    if not is_spb_city or is_excluded_vacancy:
        # Если не Питер ИЛИ это исключение (повар и т.д.)
        required_blocks.append('#ADRESS_OTHER#')
    elif is_sborshik_vacancy:
        # Если Питер И это сборщик/комплектовщик/упаковщик
        required_blocks.append('#ADRESS_SPB_1#')
    else:
        # Если Питер И любая другая вакансия (например, продавец)
        required_blocks.append('#ADRESS_SPB_2#')
    # --------------------------------------------------------------
    state_specific_blocks = {
        'initial_processing': ['#QUALIFICATION_RULES#', '#DECLINED_VAC#'],
        'awaiting_questions': ['#QUALIFICATION_RULES#', '#DECLINED_VAC#'],
        'awaiting_phone': ['#QUALIFICATION_RULES#', '#DECLINED_VAC#'],
        'awaiting_city': ['#QUALIFICATION_RULES#', '#DECLINED_VAC#'],
        'awaiting_readiness': ['#QUALIFICATION_RULES#', '#DECLINED_VAC#'],
        'awaiting_citizenship': ['#QUALIFICATION_RULES#', '#DECLINED_VAC#'],
        'clarifying_citizenship': ['#QUALIFICATION_RULES#','#CLARI#', '#DECLINED_VAC#'],
        'awaiting_age': ['#QUALIFICATION_RULES#', '#DECLINED_VAC#'],
        'clarifying_anything': ['#QUALIFICATION_RULES#', '#DECLINED_VAC#'],
        'clarifying_declined_vacancy': ['#QUALIFICATION_RULES#', '#DECLINED_VAC#'],

        'qualification_complete': ['#QUALIFICATION_RULES#', '#DECLINED_VAC#'],
        'call_later': ['#QUALIFICATION_RULES#', '#DECLINED_VAC#'],

        'init_scheduling_spb': ['#SCHEDULING_ALGORITHM#'],
        #'post_qualification_chat': ['#SCHEDULING_ALGORITHM#'],
        'scheduling_spb_day': ['#SCHEDULING_ALGORITHM#'],
        'scheduling_spb_time': ['#SCHEDULING_ALGORITHM#'],
        'interview_scheduled_spb': ['#SCHEDULING_ALGORITHM#'],
        'forwarded_to_researcher': ['#POSTCVALTWO#'],
    }
    required_blocks.extend(state_specific_blocks.get(dialogue_state, []))

    if dialogue_state in ['forwarded_to_researcher', 'post_qualification_chat', 'awaiting_questions', 'initial_processing', 'call_later']:
        required_blocks.append('#FAQ#')

    final_block_keys = list(dict.fromkeys(required_blocks))

    prompt_pieces = [prompt_library.get(key, '') for key in final_block_keys]
    
    

    # +++ КЛЮЧЕВОЕ ИЗМЕНЕНИЕ +++
    # Определяем состояния, для которых нужен календарь
    SCHEDULING_STATES = ['init_scheduling_spb', 'scheduling_spb_day', 'scheduling_spb_time', 'post_qualification_chat', 'interview_scheduled_spb']

    # Если текущее состояние требует календаря, генерируем и добавляем его
    if dialogue_state in SCHEDULING_STATES:
        calendar_block = _generate_calendar_context_2()
        prompt_pieces.append(calendar_block)
    # +++ КОНЕЦ ИЗМЕНЕНИЯ +++

    POST_QUALIFICATION_STATES = ['interview_scheduled_spb', 'post_qualification_chat']
    
    if dialogue_state in POST_QUALIFICATION_STATES:
        post_qual_block = prompt_library.get('#POSTCVAL#', '')
        if post_qual_block:
            prompt_pieces.append(post_qual_block)

    vacancy_context = (
        "[CRITICAL CONTEXT] Ниже представлено описание вакансии той, на которую откликнулся кандидат. "
        "Важно!!!У этой информации приоритет перед информацией в разделе '#FAQ (Частые вопросы кандидатов)'\n" +
        vacancy_description
    )
    prompt_pieces.insert(1, vacancy_context)
    

    # Склеиваем всё в один текст
    full_prompt_text = "\n\n".join(prompt_pieces)

    # --- ГЛОБАЛЬНАЯ ЗАМЕНА ПЛЕЙСХОЛДЕРА АДРЕСА ---
    # Заменяем [адрес вакансии] на реальные данные из БД
    final_prompt = full_prompt_text.replace("[адрес вакансии]", full_address or city_name)

    return final_prompt

async def _process_followup_logic(db: AsyncSession, dialogue: Dialogue, recruiter: TrackedRecruiter, prompt_library: dict, ctx_logger):
    """
    Обработчик сообщений для режима Follow-up.
    ИСПРАВЛЕННАЯ ВЕРСИЯ: Атомарное обновление истории и pending_messages.
    """
    ctx_logger.info("⚡ Запуск логики Follow-up (v4 - Fixed Atomicity)...")

    # 1. Отмена будущих напоминаний (Clean up)
    
    await db.execute(
        update(InterviewFollowup)
        .where(InterviewFollowup.dialogue_id == dialogue.id)
        .where(InterviewFollowup.status == 'pending')
        .values(status='cancelled', processed_at=datetime.datetime.now(datetime.timezone.utc))
    )

    
    # 2. Сбор и Дедупликация сообщений (БЕЗ записи в БД пока что)
    pending_messages = dialogue.pending_messages or []
    if not pending_messages:
        return

    # Загружаем текущую историю (immutable копия для логики)
    current_history = list(dialogue.history) if dialogue.history else []
    
    # Собираем ID, которые УЖЕ есть в истории
    existing_ids = set()
    for h in current_history:
        mid = h.get('message_id')
        if mid:
            existing_ids.add(str(mid))

    user_text_parts = []
    user_entries_to_history = [] # Временный список для новых сообщений
    
    # Обрабатываем новые сообщения
    for pm in pending_messages:
        # Извлекаем ID и контент
        if isinstance(pm, dict):
            content = pm.get('content', '')
            msg_id = str(pm.get('message_id', f'unknown_{time.time()}'))
            msg_timestamp = pm.get('timestamp_msk', datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK'))
        else:
            content = str(pm)
            msg_id = f'legacy_{hash(content)}'
            msg_timestamp = datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK')

        # Дедупликация: пропускаем, если ID уже есть в истории
        if msg_id in existing_ids:
            continue
        
        # Запоминаем ID, чтобы не добавить дубль внутри одной пачки
        existing_ids.add(msg_id)
        
        user_text_parts.append(content)
        
        # Формируем запись для истории, но НЕ добавляем в dialogue.history прямо сейчас
        new_hist_entry = {
            'message_id': msg_id,
            'role': 'user', 
            'content': content, 
            'timestamp_msk': msg_timestamp
        }
        user_entries_to_history.append(new_hist_entry)

    # Если после фильтрации сообщений нет (все дубли) — чистим очередь и выходим
    if not user_text_parts:
        ctx_logger.info("Все сообщения из pending уже есть в истории. Очистка очереди.")
        dialogue.pending_messages = None
        await db.commit()
        return

    full_user_text = "\n".join(user_text_parts)
    
    # ВАЖНО: Формируем "полную" историю только В ПАМЯТИ для контекста LLM.
    # В БД пока ничего не пишем.
    history_context_for_llm = current_history + user_entries_to_history

    # 3. Формирование Промпта
    followup_logic_block = prompt_library.get('#FOLLOWUP_LOGIC#', '')
    scheduling_logic = prompt_library.get('', '')
    calendar_context = _generate_calendar_context_2() 
    # Берем данные из HH
    hh_description = (dialogue.vacancy.extracted_info or {}).get('unified_description', "")
    
    # Берем данные из блоков (Google Docs)
    blocks_description = _find_relevant_vacancy(prompt_library, dialogue.vacancy.title, dialogue.vacancy.city, ctx_logger)

    # Склеиваем (HH + Блоки)
    if hh_description:
        vacancy_desc = f"{hh_description}\n\n--- ДОПОЛНИТЕЛЬНАЯ ИНФОРМАЦИЯ ---\n{blocks_description}"
    else:
        vacancy_desc = blocks_description

    system_prompt = (
        f"{followup_logic_block}\n\n"
        f"{scheduling_logic}\n\n"
        f"{calendar_context}\n\n"
        f"[ОПИСАНИЕ ВАКАНСИИ]:\n{vacancy_desc}"
    )

    # 4. Запрос к LLM
    try:
        llm_response = await llm_handler.get_bot_response(
            system_prompt=system_prompt,
            dialogue_history=history_context_for_llm[-10:], # Используем склеенную в памяти историю
            user_message=full_user_text,
            current_datetime_utc=datetime.datetime.now(datetime.timezone.utc),
            extra_context={"dialogue_id": dialogue.id, "action": "followup_llm_request"}
        )
        
        parsed = llm_response.get('parsed_response', {})
        usage = llm_response.get('usage_stats')
        
        if usage:
            await _log_llm_usage(db, dialogue, "Followup_Response", usage)

    except Exception as e:
        # 1. Сначала используем существующий логгер
        ctx_logger.error(f"Ошибка LLM в Follow-up (после всех попыток): {e}", exc_info=True)
        
        # 2. Вызываем наш новый обработчик сбоев
        await _handle_llm_failure(
            db=db, 
            dialogue=dialogue, 
            recruiter=recruiter, 
            error=e, 
            context="Follow-up Logic", 
            pending_msgs=pending_messages, 
            ctx_logger=ctx_logger
        )
        return 

    # 5. Парсинг данных
    response_text = parsed.get('response_text')
    extracted_data = parsed.get('extracted_data', {})
    intent = parsed.get('intent') 
    
    ctx_logger.info(f"LLM Intent: {intent}")

    # Сохранение результатов анализа (в объект dialogue, но коммит будет в конце)
    res_interview = extracted_data.get('interview_result')
    res_category = extracted_data.get('no_show_reason_category')
    res_text = extracted_data.get('no_show_reason_text')

    if res_interview:
        dialogue.interview_result = res_interview
    elif res_category:
        dialogue.interview_result = res_category

     # --- [ANALYTICS] ФИНАЛЬНЫЕ ТОЧКИ FOLLOW-UP ---
    if res_interview == 'success':
        await log_analytics_event(db, dialogue, 'followup_success')
    
    elif res_interview in ['refusal', 'self_refusal']:
        await log_analytics_event(db, dialogue, 'followup_refusal')
    
    elif res_category or intent == 'result_no_show':
        # Фиксируем подтвержденную неявку
        await log_analytics_event(db, dialogue, 'followup_no_show')
    
    # if intent == 'reschedule_process':
    #     # Фиксируем запрос на перенос
    #     await log_analytics_event(db, dialogue, 'followup_rescheduled')
    # ---------------------------------------------


    # -------------------------------
    if res_text:
        dialogue.no_show_reason = res_text

    # --- ОБРАБОТКА ПЕРЕХОДА НА ЗАПИСЬ (RESCHEDULE) ---
    if intent == 'reschedule_process':
        ctx_logger.info("Follow-up: Кандидат согласился на перезапись. Переключаем на основной алгоритм.")
        await log_analytics_event(db, dialogue, 'followup_rescheduled')
        dialogue.is_followup = False
        # dialogue.current_mode = 'standard'
        dialogue.need_follow_up_resheduled_sent = True
        dialogue.dialogue_state = 'init_scheduling_spb' # Было .state (исправлено)
        # 2. Сохраняем реальные сообщения КАНДИДАТА в историю
        # (Мы не добавляем сюда системную команду, чтобы избежать дублей в следующем цикле)
        dialogue.history = (current_history + user_entries_to_history)
        # 3. Формируем системную команду в pending (от имени user)
        system_command = {
            'message_id': f'sys_cmd_reschedule_{time.time()}',
            'role': 'user',
            'content': '[SYSTEM COMMAND] Кандидат хочет перенести собеседование (пришел из Follow-up). Начни запись на собеседование в Санкт-Петербурге (предложи выбрать день).'
        }
        
        # 4. Кладем команду в очередь и обновляем время
        dialogue.pending_messages = [system_command]
        dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
        
        await db.commit()
        ctx_logger.info("Follow-up: Переход в режим записи выполнен. Команда в очереди.")
        return 
    # -------------------------------------------------

    # 6. Отправка и АТОМАРНОЕ сохранение
    # Если есть текст ответа - отправляем
    if response_text:
        send_res = await hh_api.send_message(recruiter, db, dialogue.hh_response_id, response_text)
        
        if send_res == 200:
            # УСПЕХ: Теперь формируем финальную историю и сохраняем всё разом
            new_bot_hist = {
                'message_id': f'bot_followup_{time.time()}',
                'role': 'assistant',
                'content': response_text,
                'timestamp_msk': datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK'),
                'extracted_data': extracted_data, # Добавлено
                'state': 'followup'               # Добавлено
            }
            
            # Соединяем: Старая история + Новые сообщения юзера + Ответ бота
            final_history = current_history + user_entries_to_history + [new_bot_hist]
            dialogue.history = final_history[-350:]
            
            # Чистим очередь
            dialogue.pending_messages = None
            dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
            
            await db.commit() # <--- ЕДИНСТВЕННЫЙ COMMIT
            ctx_logger.info("Follow-up диалог успешно обновлен и сохранен.")
            
        elif send_res == 403:
            ctx_logger.error(f"Не удалось отправить ответ (403). Закрываем диалог.")
            dialogue.pending_messages = None
            dialogue.status = 'vacancy_closed_or_resume_not_found'
            await db.commit()
            
    else:
        # Если LLM решила промолчать (странно для follow-up, но допустим)
        # Мы все равно должны сохранить сообщения пользователя, чтобы не застрять
        ctx_logger.info("LLM не вернула текст ответа. Сохраняем только сообщения пользователя.")
        
        final_history = current_history + user_entries_to_history
        dialogue.history = final_history[-350:]
        dialogue.pending_messages = None
        dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
        
        await db.commit()

    

async def _process_single_dialogue(dialogue_id: int, recruiter_id: int, prompt_library: dict, db: AsyncSession):
    """Исправленная версия с правильной работой с ORM"""
    dialogue_processing_start_time = time.monotonic()

    # 1. Создаем словарь с контекстом. Сначала только то, что пришло в аргументах.
    log_context = {
        "dialogue_id": dialogue_id,
        "recruiter_id": recruiter_id,
        "worker": "handler"
    }
    # 2. Создаем адаптер. Теперь вместо logger.info будем везде использовать ctx_logger.info
    ctx_logger = logging.LoggerAdapter(logger, log_context)

    # === БЛОКИРОВКА ЧЕРЕЗ REDIS ===
    lock_key = f"dialogue_lock:{dialogue_id}"
    lock_acquired = await acquire_lock(lock_key, timeout=120)  # 2 минуты на обработку
    
    if not lock_acquired:
        ctx_logger.debug(f"Dialogue {dialogue_id} is already being processed by another worker. Skipping.")
        return
    # ==============================

    
    dialogue = None
    recruiter = None
    # --- ЗАГРУЗКА ЗДЕСЬ (значение по умолчанию) ---
    log_dialogue_hh_response_id = f"ID {dialogue_id}"
    # --- КОНЕЦ ЗАГРУЗКИ ---

    try:
        # Проверка активности сессии
        if not db.is_active:
            ctx_logger.error(f"Session is not active for dialogue {dialogue_id}")
            return

        db_fetch_start = time.monotonic()

        # --- ИЗМЕНЕНИЕ: ЗАГРУЗКА С БЛОКИРОВКОЙ ---
        # Мы пытаемся захватить строку. Если она занята другим воркером, skip_locked=True вернет None.
        stmt = (
            select(Dialogue)
            .filter_by(id=dialogue_id)
            .options(
                selectinload(Dialogue.vacancy),
                selectinload(Dialogue.candidate),
                selectinload(Dialogue.rejected_alerts),
                selectinload(Dialogue.inactive_alerts),
                selectinload(Dialogue.declined_interview_alerts),
                selectinload(Dialogue.followups),
                selectinload(Dialogue.rescheduled_alerts)
            )
            .with_for_update(skip_locked=True)
        )
        
        result = await db.execute(stmt)
        dialogue = result.scalar_one_or_none()

        # Если диалог занят другим процессом или удален
        if not dialogue:
            ctx_logger.debug(f"Dialogue {dialogue_id} is locked by another worker or not found. Skipping.")
            return
        # -----------------------------------------
        
        # Загружаем recruiter
        recruiter = await db.get(TrackedRecruiter, recruiter_id)
        if not recruiter:
            ctx_logger.error(f"Recruiter {recruiter_id} not found")
            return

        # 3. ОБНОВЛЯЕМ КОНТЕКСТ. Теперь логгер будет знать ВООБЩЕ ВСЁ.
        log_context.update({
            "hh_response_id": dialogue.hh_response_id,
            "recruiter_name": recruiter.name,  # Добавили имя рекрутера
            "vacancy_id": dialogue.vacancy_id,
            "vacancy_title": dialogue.vacancy.title, # Добавили название вакансии
            "candidate_id": dialogue.candidate_id
        })
        # Пересоздаем адаптер с полным набором данных
        ctx_logger = logging.LoggerAdapter(logger, log_context)
        
        # === НОВЫЙ БЛОК: КОНТРОЛЬ СТОИМОСТИ И АЛЕРТОВ (ЧЕРЕЗ REDIS) ===
        try:
            # Получаем актуальные данные о расходах из Redis
            cost_info = await llm_handler.get_dialogue_cost_info(dialogue.id)
            total_cost_rub = cost_info.get("rub", 0.0)
            total_cost_usd = cost_info.get("usd", 0.0)

            # 1. АЛЕРТ (порог 10 руб)
            if total_cost_rub >= 10.0:
                alert_key = f"cost_alert_sent:{dialogue.id}"
                # acquire_lock вернет True, если ключа нет (шлем алерт первый раз за 3 дня)
                if await acquire_lock(alert_key, timeout=259200): 
                    ctx_logger.warning(f"💸 Превышен лимит 10 руб ({total_cost_rub:.2f} руб). Отправляю алерт.")
                    await send_cost_alert(
                        dialogue_id=dialogue.id,
                        hh_response_id=dialogue.hh_response_id,
                        total_cost_rub=total_cost_rub,
                        total_cost_usd=total_cost_usd,
                        history=dialogue.history or [],
                        recruiter_name=recruiter.name if recruiter else "Неизвестен",
                        vacancy_title=dialogue.vacancy.title if dialogue.vacancy else "Неизвестна"
                    )

            # 2. СТОП-КРАН (порог 20 руб) - Защита от бесконечных циклов
            if total_cost_rub >= 20.0:
                ctx_logger.critical(f"🛑 КРИТИЧЕСКИЙ РАСХОД: {total_cost_rub:.2f} руб. Блокирую диалог!")
                dialogue.pending_messages = None  # Останавливаем цикл
                dialogue.dialogue_state = 'error_loop_blocked' # Помечаем для админа
                await db.commit()
                return # Выходим из обработки этого диалога

        except Exception as cost_err:
            ctx_logger.error(f"Ошибка в блоке контроля стоимости (Redis): {cost_err}")
        # === КОНЕЦ БЛОКА ===
        # === КОНЕЦ БЛОКА ===

        if dialogue.is_followup:
            await _process_followup_logic(db, dialogue, recruiter, prompt_library, ctx_logger)
            return # <--- ВЫХОДИМ, ЧТОБЫ ОСНОВНАЯ ЛОГИКА НЕ МЕШАЛА
        # =========================================================================


        # Теперь этот лог выйдет со всеми ID и именами:

        ctx_logger.debug(f"Processing dialogue {dialogue.hh_response_id}...")

        ctx_logger.debug(f"[Dialogue {dialogue_id}] DB fetch took: {time.monotonic() - db_fetch_start:.4f} sec.")

        if not dialogue or not recruiter:
            ctx_logger.error(f"Dialogue {dialogue_id} or recruiter {recruiter_id} not found")
            return
        # --- ЗАГРУЗКА ЗДЕСЬ ---
        log_dialogue_hh_response_id = dialogue.hh_response_id
        # --- КОНЕЦ ЗАГРУЗКИ ---
        # Принудительно загружаем связанные объекты в сессию
        await db.refresh(dialogue.candidate)
        await db.refresh(dialogue.vacancy)

        ctx_logger.debug(
            f"Processing dialogue {dialogue.hh_response_id}...",
            extra={"action": "start_processing"}
        )

        # --- [START] ОЖИВЛЕНИЕ МОЛЧУНОВ ---
        # Если диалог был в статусе "timed_out" (молчание), но пришли новые сообщения,
        # возвращаем его в работу, сбрасываем уровень напоминаний и отменяем алерт админу.
        if dialogue.status == 'timed_out':
            ctx_logger.info(
                f"[{dialogue.hh_response_id}] 🔔 Кандидат ответил! Снимаем статус 'timed_out' и сбрасываем напоминания.",
                extra={"action": "revive_timed_out_dialogue"}
            )
            dialogue.status = 'in_progress'
            dialogue.reminder_level = 0
            
            # Если висит неотправленное уведомление о молчании в очереди - отменяем его
            # (мы подгрузили inactive_alerts через selectinload выше, так что доступ быстрый)
            if dialogue.inactive_alerts and dialogue.inactive_alerts.status == 'pending':
                dialogue.inactive_alerts.status = 'cancelled'
                dialogue.inactive_alerts.processed_at = datetime.datetime.now(datetime.timezone.utc)
                ctx_logger.info(f"[{dialogue.hh_response_id}] Отменено уведомление InactiveNotificationQueue.")
        # --- [END] ОЖИВЛЕНИЕ МОЛЧУНОВ ---


        # --- [ANALYTICS] ОТВЕТ ПОЛЬЗОВАТЕЛЯ ---
        # Если диалог в режиме реактивации и есть новые сообщения от юзера,
        # проверяем, был ли это первый ответ, и логируем.
        # if dialogue.pending_messages:
        #     # Проверяем, есть ли там реальные сообщения от юзера, а не наши system commands
        #     has_user_msg = any(
        #         (isinstance(m, dict) and m.get('role') == 'user' and not m.get('content', '').startswith('[SYSTEM COMMAND]'))
        #         for m in dialogue.pending_messages
        #     )
        #     if has_user_msg:
        #         # check_duplicates=True гарантирует, что мы запишем только ПЕРВЫЙ ответ в этой рассылке
        #         await log_analytics_event(db, dialogue, 'user_reply', check_duplicates=True)
        # --------------------------------------


        pending_messages = dialogue.pending_messages or []
        if not pending_messages:
            ctx_logger.debug(f"Dialogue {dialogue.id}: no pending messages")
            return

        # *************************************************************************************************************************************
        # СПЕЦИАЛЬНАЯ ОБРАБОТКА ДЛЯ AWAITING_CITIZENSHIP
        # *************************************************************************************************************************************
        if dialogue.dialogue_state == "awaiting_citizenship" and pending_messages:
            all_pending_content = "\n".join([pm.get('content', '') if isinstance(pm, dict) else str(pm) for pm in pending_messages])
            citizenship_analysis_prompt = (
                '''Проанализируй сообщения кандидата и верни ответ\n
                [CRITICAL RULE] Твой ответ ВСЕГДА должен быть в формате JSON.
                Структура JSON должна быть следующей:
                {
                "is": "yes" или "no",
                "citizenship": "ЕАЭС" или "внж рф" или "рвп рф" или название страны или Null,
                }\n

                Если в сообщениях содержится гражданство или название страны то в поле `is` верни `yes`\n
                Если в сообщениях нет инфы о гражданстве (стране) то в поле `is` верни `no`\n
                Если в сообщениях содержится информация, что человек гражданин (или просто указана страна) Россия (РФ) или Беларусь (Республика Беларусь, РБ) или Армения или Киргизия или Казахстан то `ЕАЭС`.\n"
                Если в сообщениях содержится информация, что человек имеет ВНЖ России (РФ) или РВП России (РФ), то верни в "citizenship" строго значение "внж рф" или "рвп рф"
                Если другое то верни в `citizenship` название страны.\n'''
            )
            
            citizenship_attempts = [] 
            llm_citizenship_response = None
            
            try:
                # Вызов LLM с трекером попыток для tenacity
                llm_citizenship_response = await llm_handler.get_bot_response(
                    system_prompt=citizenship_analysis_prompt,
                    dialogue_history=[],
                    user_message=all_pending_content,
                    current_datetime_utc=datetime.datetime.now(datetime.timezone.utc),
                    attempt_tracker=citizenship_attempts, 
                    skip_instructions=True,
                    extra_context=log_context
                )

                if llm_citizenship_response:
                    # 1. Логируем успешный расход (токены и деньги)
                    await _log_llm_usage(
                        db, dialogue, 
                        "Citizenship_Analysis", 
                        llm_citizenship_response.get("usage_stats")
                    )
                    
                    # 2. Логируем скрытые ретраи (если были)
                    total_attempts = len(citizenship_attempts)
                    if total_attempts > 1:
                        ctx_logger.warning(
                            f"[{dialogue.hh_response_id}] Анализ гражданства: выполнено успешно после {total_attempts-1} ретраев.",
                            extra={"action": "citizenship_retry_success", "attempts": total_attempts}
                        )
                        for i in range(total_attempts - 1):
                            await _log_llm_usage(db, dialogue, f"Citizenship_Analysis (RETRY #{i+1})")
                            
                    
                    #await db.commit()
                    await db.flush()
                    await db.refresh(dialogue)

                    # 3. Обработка полученного результата
                    try:
                        parsed_response = llm_citizenship_response.get('parsed_response')
                        if parsed_response and parsed_response.get("is") == "yes":
                            ctx_logger.info(
                                f"[{dialogue.hh_response_id}] Распарсили гражданство: {parsed_response.get('citizenship')}",
                                extra={"action": "citizenship_parsed", "citizenship": parsed_response.get('citizenship')}
                            )
                            citizenship = parsed_response.get("citizenship")
                            system_command_content = None

                            if citizenship == "ЕАЭС":
                                system_command_content = "[SYSTEM COMMAND] Кандидат сообщил что у него гражданство одной из стран ЕАЭС. Установи extracted_data.citizenship='ЕАЭС'. Переходи к следующему этапу (возрасту) и ОБЯЗАТЕЛЬНО задай кандидату вопрос про его возраст."
                                
                            elif citizenship == "внж рф" or  citizenship == "рвп рф":
                                system_command_content = "[SYSTEM COMMAND] Кандидат сообщил что у него РВП РФ или ВНЖ РФ, поставь в поле citizenship строго значение строго значение 'внж рф' или 'рвп рф' соответственно и переходи к следующему этапу анкеты (возрасту) и ОБЯЗАТЕЛЬНО задай кандидату вопрос про его возраст."
                            else:
                                system_command_content = f"[SYSTEM COMMAND] Кандидат сообщил что у него гражданство {citizenship}, уточни есть ли у него РВП или ВНЖ в России."
                                dialogue.dialogue_state = "clarifying_citizenship"

                            if system_command_content:
                                system_command = {
                                    'message_id': f'sys_cmd_citizenship_{time.time()}',
                                    'role': 'user',
                                    'content': system_command_content
                                }
                                # Добавляем системную команду в очередь
                                pending_messages = (pending_messages or []) + [system_command]
                                dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
                                await db.commit()
                                
                                
                        else:
                            ctx_logger.info(
                                f"[{dialogue.hh_response_id}] Информация о гражданстве не найдена в текущем сообщении.",
                                extra={"action": "citizenship_not_found"}
                            )

                    except Exception as parse_err:
                        ctx_logger.error(
                            f"[{dialogue.hh_response_id}] Ошибка разбора JSON гражданства: {parse_err}",
                            extra={"action": "citizenship_json_error"}
                        )

            except Exception as citizenship_err:
                # --- ЛОГИРОВАНИЕ ПОЛНОГО ПРОВАЛА ---
                ctx_logger.error(
                    f"[{dialogue.hh_response_id}] Анализ гражданства ПРОВАЛЕН после {len(citizenship_attempts)} попыток: {citizenship_err}",
                    extra={"action": "citizenship_analysis_failed_total"}
                )
                for i in range(len(citizenship_attempts)):
                    await _log_llm_usage(
                        db, dialogue, 
                        f"Citizenship_Analysis (FAILED #{i+1}: {type(citizenship_err).__name__})"
                    )
                
                # --- ВЫЗОВ FALLBACK ОБРАБОТЧИКА ---
                await _handle_llm_failure(
                    db=db,
                    dialogue=dialogue,
                    recruiter=recruiter,
                    error=citizenship_err,
                    context="Citizenship Analysis",
                    pending_msgs=pending_messages,
                    ctx_logger=ctx_logger
                )
                return  # Вместо raise, чтобы остановить обработку этого диалога успешно

        # *************************************************************************************************************************************
        # КОНЕЦ СПЕЦИАЛЬНОЙ ОБРАБОТКИ ДЛЯ AWAITING_CITIZENSHIP
        # *************************************************************************************************************************************
        # Обработка сообщений
        user_entries_to_history = []
        all_masked_content = []

        for pm in pending_messages:
            original_content = pm.get('content', '') if isinstance(pm, dict) else str(pm)
            masked_content, extracted_fio, extracted_phone = extract_and_mask_pii(original_content)

            # Обновляем candidate (объект уже в сессии после refresh)
            # if extracted_fio:
            #     dialogue.candidate.full_name = extracted_fio

            if extracted_phone:
                dialogue.candidate.phone_number = extracted_phone

            message_id = pm.get('message_id') if isinstance(pm, dict) else f'legacy_{int(time.time())}'
            user_entries_to_history.append({
                'message_id': message_id,
                'role': 'user',
                'content': masked_content,
                'timestamp_msk': pm.get('timestamp_msk', 'время не определено') if isinstance(pm, dict) else 'время не определено' # <-- ДОБАВЛЕНО
            })
            all_masked_content.append(masked_content)

        combined_masked_message = "\n".join(all_masked_content)

        # 1. Достаем нормализованные данные из HH
        vacancy_info = dialogue.vacancy.extracted_info or {}
        hh_description = vacancy_info.get('unified_description', "")

        # 1.1 Достаем описание из блоков (Google Docs, как было раньше)
        blocks_description = _find_relevant_vacancy(
            prompt_library=prompt_library,
            vacancy_title=dialogue.vacancy.title,
            vacancy_city=dialogue.vacancy.city,
            context_logger=ctx_logger
        )

        # 1.2 Склеиваем (сначала HH, потом Блоки)
        if hh_description:
            relevant_vacancy_desc = f"{hh_description}\n\n--- ДОПОЛНИТЕЛЬНАЯ ИНФОРМАЦИЯ ---\n{blocks_description}"
        else:
            relevant_vacancy_desc = blocks_description

        full_address = vacancy_info.get('full_address', dialogue.vacancy.city)

        # !!! ДОБАВЬ ЭТУ СТРОКУ, иначе упадет на context_postfix ниже !!!
        vacancy_title = dialogue.vacancy.title 
        vacancy_city = dialogue.vacancy.city or "город не указан"

        # 2. Передаем всё в сборщик промпта
        system_prompt = _assemble_dynamic_prompt(
            prompt_library=prompt_library,
            dialogue_state=dialogue.dialogue_state,
            user_message=combined_masked_message.lower(),
            vacancy_description=relevant_vacancy_desc,
            city_name=vacancy_city,    
            full_address=full_address,
            vacancy_title=vacancy_title  # <--- ДОБАВЛЕНО
        )

        # Переменные vacancy_title и vacancy_city теперь объявлены и не вызовут ошибку тут:
        context_postfix = (
            f"[CURRENT TASK] Ты общаешься с кандидатом по вакансии '{vacancy_title}' "
            f"в городе '{vacancy_city}'. Текущее состояние: '{dialogue.dialogue_state}'."
        )
        final_system_prompt = system_prompt + "\n\n" + context_postfix

        # LLM запрос
        llm_call_start = time.monotonic()
        llm_data = None
        attempt_tracker = [] # <--- Создаем "ловушку" для попыток

        try:
            # Передаем attempt_tracker в функцию
            llm_data = await llm_handler.get_bot_response(
                system_prompt=final_system_prompt,
                dialogue_history=(dialogue.history or [])[-25:],
                user_message=combined_masked_message,
                current_datetime_utc=datetime.datetime.now(datetime.timezone.utc),
                attempt_tracker=attempt_tracker,
                extra_context=log_context # <--- Передаем список
            )
            
            # --- УСПЕШНЫЙ СЦЕНАРИЙ ---
            # Если мы здесь, значит последняя попытка была успешной.
            # Если в attempt_tracker больше 1 элемента, значит были скрытые ретраи.
            
            total_attempts = len(attempt_tracker)
            failed_attempts = total_attempts - 1 # Все кроме последней (успешной)
            
            if failed_attempts > 0:
                ctx_logger.warning(
                    f"[{dialogue.hh_response_id}] Было {failed_attempts} скрытых ретраев tenacity.",
                    extra={"action": "llm_retries_detected", "retry_count": failed_attempts}
                )
                for i in range(failed_attempts):
                    await _log_llm_usage(db, dialogue, f"{dialogue.dialogue_state} (RETRY #{i+1})")

        except Exception as llm_error:
            # --- СЦЕНАРИЙ ПОЛНОГО ПРОВАЛА ---
            ctx_logger.error(
                f"[{dialogue.hh_response_id}] LLM Request FAILED completely after {len(attempt_tracker)} attempts: {llm_error}",
                extra={"action": "llm_request_failed_total"}
            )
            
            try:
                for i in range(len(attempt_tracker)):
                    await _log_llm_usage(
                        db, dialogue, 
                        f"{dialogue.dialogue_state} (FAILED #{i+1}: {type(llm_error).__name__})"
                    )
            except Exception as log_ex:
                ctx_logger.error(f"Failed to log LLM errors to DB: {log_ex}")

            # --- ВЫЗОВ FALLBACK ОБРАБОТЧИКА ---
            await _handle_llm_failure(
                db=db,
                dialogue=dialogue,
                recruiter=recruiter,
                error=llm_error,
                context=f"Main Dialogue Loop ({dialogue.dialogue_state})",
                pending_msgs=pending_messages,
                ctx_logger=ctx_logger
            )
            return  # Вместо raise

        llm_duration = time.monotonic() - llm_call_start
        ctx_logger.debug(
            f"[{dialogue.hh_response_id}] LLM call: {llm_duration:.2f} sec.",
            extra={"llm_duration": llm_duration, "action": "llm_request_complete"}
        )

        if llm_data is None:
            alert_message = "⚠️ LLM service unavailable!"
            await send_system_alert(alert_message, alert_type="admin_only")
            return

        # Распаковка ответа
        llm_response = llm_data.get("parsed_response")
        usage_stats = llm_data.get("usage_stats")

        # === ЛОГИРОВАНИЕ ТОКЕНОВ ===
        if usage_stats:
            try:
                await _log_llm_usage(
                    db, dialogue, 
                    dialogue.dialogue_state, 
                    llm_data.get("usage_stats")
                )
            except Exception as e:
                ctx_logger.error(f"Error logging tokens for dialogue {dialogue.id}: {e}")
        # ===========================

        # --- СПИСОК РАЗРЕШЕННЫХ СТАТУСОВ ДИАЛОГА ---
        ALLOWED_STATES = {
            'initial_processing',
            'awaiting_questions',
            'awaiting_phone',
            'awaiting_city',
            'awaiting_readiness',
            'awaiting_citizenship',
            'clarifying_citizenship',
            'awaiting_age',
            'clarifying_anything',
            'clarifying_declined_vacancy',
            'qualification_complete',
            'call_later',
            'init_scheduling_spb',
            'post_qualification_chat',
            'scheduling_spb_day',
            'scheduling_spb_time',
            'interview_scheduled_spb',
            'forwarded_to_researcher',
            'qualification_failed',
            'declined_vacancy',
            'declined_interview'
        }

        bot_response_text = llm_response.get("response_text")
        new_state = llm_response.get("new_state", "error_state")
        extracted_data = llm_response.get("extracted_data")




        # --- [START] ВАЛИДАЦИЯ СТАТУСА ---
        # --- [START] ВАЛИДАЦИЯ СТАТУСА ---
        if new_state not in ALLOWED_STATES:
            ctx_logger.error(
                f"[{dialogue.hh_response_id}] CRITICAL: LLM hallucinated invalid state: '{new_state}'.",
                extra={"action": "invalid_state_detected", "invalid_state": new_state}
            )
            
            # 1. Формируем текст замечания для модели
            hallucination_corr_cmd = {
                'message_id': f'sys_state_hallucination_{time.time()}',
                'role': 'user',
                'content': (
                    f"[SYSTEM COMMAND] В твоем последнем ответе произошла техническая ошибка: "
                    f"ты вернул недопустимое состояние (new_state) '{new_state}'. "
                    f"Такого состояния НЕ СУЩЕСТВУЕТ в твоей инструкции. "
                    f"Пожалуйста, будь внимательнее, проанализируй контекст заново и выбери "
                    f"корректное состояние (new_state) СТРОГО по инструкциям"
                )
            }

            # 2. Сохраняем историю (чтобы бот видел, на что он отвечал) 
            # и добавляем системную команду в очередь (pending_messages)
            dialogue.history = (dialogue.history or []) + user_entries_to_history
            dialogue.pending_messages = [hallucination_corr_cmd]
            dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)

            # 3. Фиксируем изменения в базе и выходим. 
            # В следующем цикле воркер придет с этой командой к LLM.
            await db.commit()
            ctx_logger.info(f"[{dialogue.hh_response_id}] Отправлено на исправление галлюцинации стейта: {new_state}")
            return 
        
        # --- [END] ВАЛИДАЦИЯ СТАТУСА ---



        # [RESCHEDULE PROTECTION] Отмена напоминаний при начале переноса
        # =====================================================================
        if new_state == 'init_scheduling_spb' and dialogue.interview_datetime_utc is not None:
            ctx_logger.info(
                f"[{dialogue.hh_response_id}] Обнаружено начало процесса переноса. "
                f"Отменяю все активные напоминания и фоллоу-апы (старое время: {dialogue.interview_datetime_utc}).",
                extra={"action": "reschedule_cleanup_started"}
            )
            
            # 1. Отменяем обычные напоминания (ДО собеса)
            await db.execute(
                update(InterviewReminder)
                .where(InterviewReminder.dialogue_id == dialogue.id)
                .where(InterviewReminder.status == 'pending')
                .values(
                    status='cancelled', 
                    processed_at=datetime.datetime.now(datetime.timezone.utc)
                )
            )

            # 2. Отменяем фоллоу-апы (ПОСЛЕ собеса)
            await db.execute(
                update(InterviewFollowup)
                .where(InterviewFollowup.dialogue_id == dialogue.id)
                .where(InterviewFollowup.status == 'pending')
                .values(
                    status='cancelled', 
                    processed_at=datetime.datetime.now(datetime.timezone.utc)
                )
            )

        
        # =====================================================================
        # [START] УНИВЕРСАЛЬНАЯ ВАЛИДАЦИЯ ДАТЫ (для Time Selection и Final Booking)
        # =====================================================================
        # Список состояний, в которых нам критично важно, чтобы дата была верной
        DATE_CRITICAL_STATES = ['init_scheduling_spb','scheduling_spb_day','scheduling_spb_time', 'interview_scheduled_spb']

        if new_state in DATE_CRITICAL_STATES:
            interview_date = extracted_data.get("interview_date")
            interview_time = extracted_data.get("interview_time")

            TIME_KEYWORDS = [
                "сегодня", "завтра", "послезавтра", "понедельник", "вторник", "сред", "четверг", 
                "пятниц", "суббот", "воскресен", "январ", "феврал", "март", "апрел", "май", "июн", 
                "июл", "август", "сентябр", "октябр", "ноябр", "декабр", "число", "время", "числа"
            ]
            # Проверяем, есть ли маркеры времени в ответе бота или сообщении юзера
            bot_text_low = (bot_response_text or "").lower()
            user_text_low = combined_masked_message.lower()
            
            has_time_keywords = any(kw in bot_text_low or kw in user_text_low for kw in TIME_KEYWORDS)

            # Входим в блок, если есть дата в JSON ИЛИ если в тексте обсуждают время
            if interview_date or has_time_keywords:

                # --- ШАГ 1: ВАЛИДАЦИЯ ДАТЫ (АУДИТ) ---
                # Сначала проверяем, ту ли вообще дату выбрала модель
                # ---------------------------------------------------------
                already_stored_date_str = None
                if dialogue.interview_datetime_utc:
                    # Конвертируем UTC из базы в дату МСК для корректного сравнения
                    already_stored_date_str = dialogue.interview_datetime_utc.astimezone(SPB_TIMEZONE).strftime('%Y-%m-%d')
                
                # [FIX START] --- Умная логика запуска аудита ---
                run_audit = True # По умолчанию проверяем
                
                # Если даты совпадают, проверяем, не пытается ли пользователь сменить дату текстом
                if already_stored_date_str == interview_date:
                    # Слова-триггеры, указывающие на обсуждение времени
                    time_triggers = [
                        "завтра", "послезавтра", "сегодня", 
                        "понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресен"
                        
                    ]
                    # Берем последнее сообщение пользователя
                    last_user_msg = combined_masked_message.lower()
                    
                    # Если в тексте НЕТ слов о времени, то верим совпадению дат и пропускаем аудит (экономим $)
                    if not any(trigger in last_user_msg for trigger in time_triggers):
                        ctx_logger.info(
                            f"[{dialogue.hh_response_id}] Дата {interview_date} совпадает с БД и нет новых триггеров времени. Пропуск аудита.",
                            extra={"action": "skip_audit_date_match"}
                        )
                        run_audit = False
                    else:
                         ctx_logger.info(
                            f"[{dialogue.hh_response_id}] Дата совпадает, но в тексте найдены временные триггеры ('завтра' и т.п.). ПРИНУДИТЕЛЬНЫЙ АУДИТ.",
                            extra={"action": "force_audit_due_to_keywords"}
                        )
                # [FIX END] -------------------------------------
                
                if run_audit:
                    ctx_logger.info(f"[{dialogue.hh_response_id}] Проверка даты аудитором (в состоянии {new_state}): {interview_date}")
                    
                    cal_context = _generate_calendar_context_2()
                    # История: старая + то, что юзер прислал сейчас
                    full_hist = (dialogue.history or []) + user_entries_to_history
                    
                    verified_date_res = await _verify_date_audit(db, dialogue, recruiter, pending_messages, interview_date, full_hist, cal_context, log_context)
                    if verified_date_res[0] is None:
                        return # Сбой LLM, выходим (очередь уже очищена в _handle_llm_failure)
                    verified_date, audit_reason = verified_date_res

                    # СРАВНИВАЕМ
                    if verified_date != interview_date and verified_date != "none":
                        ctx_logger.warning(f"🚨 ОБНАРУЖЕНА ГАЛЛЮЦИНАЦИЯ! LLM: {interview_date}, Аудитор: {verified_date}")

                        # --- ВЫЧИСЛЯЕМ ДЕНЬ НЕДЕЛИ ---
                        try:
                            v_date_obj = datetime.datetime.strptime(verified_date, '%Y-%m-%d')
                            weekdays_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
                            weekday_idx = v_date_obj.weekday()
                            v_weekday = weekdays_ru[weekday_idx]
                            
                            # Проверка на праздники (Feb 23 и т.д.) и Воскресенье ПРЯМО ЗДЕСЬ
                            holidays = [(2, 23), (5, 1), (5, 2), (5, 3), (5, 9), (5, 10)]
                            is_holiday = (v_date_obj.month, v_date_obj.day) in holidays
                            is_sunday = (weekday_idx == 6)
                            
                        except Exception:
                            v_weekday = "указанный день"
                            is_feb_23 = False
                            is_sunday = False

                        # Отправляем алерт в ТГ
                        user_text_now = " ".join(all_masked_content)
                        full_hist_text = "\n".join([f"{m.get('role')}: {m.get('content')}" for m in full_hist])
                        
                        await send_date_conflict_alert(
                            dialogue_id=dialogue.id,
                            hh_response_id=dialogue.hh_response_id,
                            user_said=user_text_now,
                            llm_suggested=interview_date,
                            verified_date=f"{verified_date} ({v_weekday})",
                            history_text=full_hist_text,
                            reasoning=audit_reason
                        )

                        # ФОРМИРУЕМ ТЕКСТ КОМАНДЫ (Разрешение конфликта)
                        correction_content = "" # Инициализация
                        
                        if is_sunday or is_holiday:
                            reason_msg = "это воскресенье" if is_sunday else "это праздничный день"
                            
                            correction_content = (
                                f"[SYSTEM COMMAND] Ты ошибся в интерпретации. Пользователь имел в виду {verified_date} ({v_weekday}), но {reason_msg}, когда собеседования НЕ проводятся. "
                                f"1. ОБЯЗАТЕЛЬНО обнови поле 'interview_date' в JSON на null (убери дату совсем). "
                                f"2. В ответе вежливо объясни кандидату, что {reason_msg}, и предложи выбрать другой день."
                            )
                            ctx_logger.info(f"[{dialogue.hh_response_id}] Аудит: Пользователь выбрал запрещенный день ({v_weekday}). Требую сброс даты.")
                            
                        else:
                            # СЦЕНАРИЙ: Аудитор нашел дату, она РАЗРЕШЕНА.
                            # Мы говорим модели: "Исправь JSON на правильную дату".
                            correction_content = (
                                f"[SYSTEM COMMAND] В прошлом шаге ты ошибся и предложил дату {interview_date}. "
                                f"На самом деле пользователь выбрал {v_weekday} ({verified_date}) согласно календарю. "
                                f"Сгенерируй ответ заново, подтвердив ПРАВИЛЬНУЮ дату ({v_weekday}, {verified_date}). "
                                f"ОБЯЗАТЕЛЬНО обнови поле 'interview_date' в JSON на '{verified_date}'."
                            )
                            ctx_logger.info(f"[{dialogue.hh_response_id}] Отправлено на исправление даты ({v_weekday}).")

                        # --- !!! ВОТ ЭТОГО КУСКА НЕ ХВАТАЛО !!! ---
                        # Создаем объект команды с правильным текстом
                        correction_cmd = {
                            'message_id': f'sys_date_corr_{time.time()}',
                            'role': 'user',
                            'content': correction_content
                        }

                        # Кладем в очередь и сохраняем историю
                        dialogue.pending_messages = [correction_cmd]
                        dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
                        dialogue.history = (dialogue.history or []) + user_entries_to_history
                        
                        await db.commit()
                        ctx_logger.info(f"[{dialogue.hh_response_id}] Отправлено на исправление даты ({v_weekday}).")
                        return # <--- ПРЕРЫВАЕМ. К регламенту (времени) перейдем только когда дата будет верной.
                    
                    
                    if verified_date != "none":
                        interview_date = verified_date

                # --- ШАГ 2: ПОДСКАЗКА ДНЯ НЕДЕЛИ И РЕГЛАМЕНТА (HINT) ---
                # Если мы дошли сюда, значит дата либо подтверждена аудитом, либо уже была в БД.
                # Теперь проверяем время для этой даты.
                # ---------------------------------------------------------
                # --- ШАГ 2: ПОДСКАЗКА ДНЯ НЕДЕЛИ И РЕГЛАМЕНТА (HINT) ---
                try:
                    # 1. Получаем текущее время в МСК для сравнения
                    now_msk = datetime.datetime.now(SPB_TIMEZONE)
                    today_str = now_msk.strftime('%Y-%m-%d')
                    current_hour = now_msk.hour

                    # 2. Вычисляем данные о выбранной дате
                    v_date_obj = datetime.datetime.strptime(interview_date, '%Y-%m-%d')
                    holidays = [(2, 23), (5, 1), (5, 2), (5, 3), (5, 9), (5, 10)]
                    is_holiday = (v_date_obj.month, v_date_obj.day) in holidays
                    weekday_idx = v_date_obj.weekday()  # 0 = Пн, 6 = Вс
                    weekdays_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
                    correct_weekday = weekdays_ru[weekday_idx]

                    # Определяем категорию вакансии
                    warehouse_keywords = [
                        'сборщик онлайн заказов', 'сборщик заказов', 'сканировщик qr-кодов', 
                        'сканировщик', 'сборщик', 'комплектовщик', 'упаковщик', 'кассир-комплектовщик'
                    ]
                    current_title_low = (dialogue.vacancy.title or "").lower()
                    current_city_low = (dialogue.vacancy.city or "").lower()
                    
                    target_cities = ['санкт-петербург', 'мурино', 'кудрово']
                    is_spb = any(city in current_city_low for city in target_cities)
                    is_warehouse = any(word in current_title_low for word in warehouse_keywords)

                    # 3. Определяем базовые слоты по регламенту
                    if weekday_idx == 6 or is_holiday:  # Добавили проверку на 23 февраля
                        slots = []
                    elif weekday_idx == 5:  # Суббота
                        slots = ["14:00"]
                    else:  # Будни
                        if is_spb and is_warehouse:
                            slots = ["10:00", "12:00", "15:00", "16:00"]
                        else:
                            slots = ["10:00", "12:00", "16:00"]

                    # --- ЛОГИКА ФИЛЬТРАЦИИ ДЛЯ "СЕГОДНЯ" ---
                    if interview_date == today_str and slots:
                        # Оставляем только те слоты, которые минимум на 1 час позже текущего времени
                        # (Чтобы у кандидата было время доехать)
                        slots = [s for s in slots if int(s.split(':')[0]) > current_hour]
                    # ---------------------------------------

                    # 4. Формируем текст инструкции
                    if is_holiday:
                        hint_content = (
                            f"[SYSTEM COMMAND] Внимание!!! {interview_date} — это праздничный день!!! "
                            f"В праздничные дни офисы закрыты и собеседования не проводятся. Запись невозможна. Предложи другой день."
                        )

                    elif weekday_idx == 6:
                    
                        hint_content = (
                            f"[SYSTEM COMMAND] Внимание!!! {interview_date} это {correct_weekday}!!! "
                            f"По воскресеньям собеседования не проводятся, запись невозможна. Предложи другой день."
                        )
                    elif not slots: # Если это сегодня и все слоты уже прошли
                        hint_content = (
                            f"[SYSTEM COMMAND] Внимание!!! Сегодня ({interview_date}) запись на собеседование уже окончена (текущее время {now_msk.strftime('%H:%M')}). "
                            f"Запись на сегодня невозможна. Предложи кандидату выбрать другой день (например, завтра или в ближайший будний день)."
                        )
                    else:
                        slots_str = ", ".join(slots)
                        hint_content = (
                            f"[SYSTEM COMMAND] Внимание!!! {interview_date} это {correct_weekday}!!! "
                            f"В этот день строго разрешены только следующие слоты: {slots_str}. "
                            f"Ты ОБЯЗАН перечислить ВСЕ эти варианты ({slots_str}) в своем ответе, чтобы кандидат мог выбрать один из них. "
                            f"Не выбирай время за кандидата, предоставь ему полный список доступных слотов из этой команды."
                        )

                    # 5. Проверяем историю, чтобы не зациклиться
                    history_to_check = (dialogue.history or [])[-5:]
                    already_hinted = any(hint_content == m.get('content') for m in history_to_check)

                    if not already_hinted:
                        ctx_logger.info(f"[{dialogue.hh_response_id}] Добавляю регламент дня: {interview_date} (Свободно: {slots})")
                        
                        hint_cmd = {
                            'message_id': f'sys_hint_{time.time()}',
                            'role': 'user',
                            'content': hint_content
                        }
                        
                        dialogue.history = (dialogue.history or []) + [hint_cmd]
                        dialogue.pending_messages = user_entries_to_history 
                        dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
                        
                        await db.commit()
                        return 
                except Exception as e:
                    ctx_logger.error(f"Ошибка в этапе Hint валидации даты и регламента: {e}")
            # Вот тут проверку времени
        # =====================================================================
        # [START] ШАГ 3: ЖЕСТКАЯ ВАЛИДАЦИЯ ВРЕМЕНИ (TIME ENFORCEMENT)
        # =====================================================================
        if new_state in DATE_CRITICAL_STATES:
            

            # Проверяем время только если и дата, и время уже извлечены моделью
            if interview_date and interview_time:
                try:
                    # 1. Снова вычисляем доступные слоты для этой даты (синхронно с логикой Hint)
                    now_msk = datetime.datetime.now(SPB_TIMEZONE)
                    today_str = now_msk.strftime('%Y-%m-%d')
                    current_hour = now_msk.hour

                    v_date_obj = datetime.datetime.strptime(interview_date, '%Y-%m-%d')
                    weekday_idx = v_date_obj.weekday()
                    
                    # Логика определения слотов (повторяем для точности проверки)
                    warehouse_keywords = [
                        'сборщик онлайн заказов', 'сборщик заказов', 'сканировщик qr-кодов', 
                        'сканировщик', 'сборщик', 'комплектовщик', 'упаковщик', 'кассир-комплектовщик'
                    ]
                    is_warehouse = any(word in (dialogue.vacancy.title or "").lower() for word in warehouse_keywords)
                    holidays = [(2, 23), (5, 1), (5, 2), (5, 3), (5, 9), (5, 10)]
                    is_holiday = (v_date_obj.month, v_date_obj.day) in holidays # <--- НОВАЯ СТРОКА
                    if weekday_idx == 6 or is_holiday:
                        allowed_slots = []
                    elif weekday_idx == 5: # Сб
                        allowed_slots = ["14:00"]
                    else: # Будни
                        # Используем ту же логику СПб/Мурино/Кудрово, что и выше
                        target_cities = ['санкт-петербург', 'мурино', 'кудрово']
                        is_spb_area = any(city in (dialogue.vacancy.city or "").lower() for city in target_cities)
                        allowed_slots = ["10:00", "12:00", "15:00", "16:00"] if (is_spb_area and is_warehouse) else ["10:00", "12:00", "16:00"]

                    # Фильтр для "сегодня"
                    if interview_date == today_str:
                        allowed_slots = [s for s in allowed_slots if int(s.split(':')[0]) > current_hour]

                    # 2. СРАВНЕНИЕ: Проверяем, входит ли время от LLM в список разрешенных
                    # Мы используем .strip() и проверяем вхождение, чтобы избежать ошибок формата (например "10:00" vs "10:00 ")
                    clean_time = interview_time.strip()
                    
                    if clean_time not in allowed_slots:
                        ctx_logger.warning(f"🚨 МОДЕЛЬ ОШИБЛАСЬ СО ВРЕМЕНЕМ! Выбрано: {clean_time}, Доступно: {allowed_slots}")

                        # Формируем текст ошибки
                        if not allowed_slots:
                            
                            if is_holiday:
                                error_msg = f"{interview_date} — праздничный день, офисы закрыты, собеседования не проводятся."
                            elif weekday_idx == 6:
                                error_msg = f"По воскресеньям ({interview_date}) собеседования не проводятся."
                            else:
                                error_msg = f"На сегодня ({interview_date}) запись уже окончена."
                        else:
                            error_msg = f"На дату {interview_date} доступно только время: {', '.join(allowed_slots)}. Слот {clean_time} недоступен."

                        time_corr_cmd = {
                            'message_id': f'sys_time_corr_{time.time()}',
                            'role': 'user',
                            'content': (
                                f"[SYSTEM COMMAND] {error_msg} "
                                f"Предложи выбрать из доступных слотов: {', '.join(allowed_slots) if allowed_slots else 'другой день'}."
                                f"ОБЯЗАТЕЛЬНО обнови поле 'interview_time' в JSON на null, пока кандидат не выберет корректный вариант. Когда кандидат определится со временем, верни его в поле 'interview_time'."
                            )
                        }

                        # Сохраняем историю (включая сообщения юзера из этой итерации!) и отправляем на перегенерацию
                        dialogue.history = (dialogue.history or []) + user_entries_to_history + [time_corr_cmd]
                        dialogue.pending_messages = user_entries_to_history + [time_corr_cmd]# Чтобы LLM видела, на что она отвечает
                        dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
                        
                        await db.commit()
                        ctx_logger.info(f"[{dialogue.hh_response_id}] Отправлено на исправление времени: {clean_time} -> {allowed_slots}")
                        return # <--- ПРЕРЫВАЕМ и перегенерируем ответ

                except Exception as e:
                    ctx_logger.error(f"Ошибка в этапе жесткой валидации времени: {e}")

        # =====================================================================
        # [END] КОНЕЦ ШАГА 3
        # =====================================================================    
        # Обновляем статус
        if dialogue.status == 'new':
            dialogue.status = 'in_progress'

        # Обновляем extracted_data (candidate уже в сессии)
        if extracted_data and dialogue.status != 'qualified':
            # Сохраняем текущий стейт (до того, как LLM предложила новый), 
            # чтобы понимать, в каком контексте мы находились.
            current_state_at_update = dialogue.dialogue_state

            # --- 1. ОБРАБОТКА ВОЗРАСТА ---
            raw_age = extracted_data.get("age")
            if raw_age:
                # Разрешенные стейты для записи возраста
                allowed_age_states = ['awaiting_age', 'clarifying_anything']
                
                if current_state_at_update in allowed_age_states:
                    # Берем весь текст текущих входящих сообщений для проверки
                    current_user_text = " ".join(all_masked_content).lower()
                    
                    if dialogue.candidate.age == raw_age:
                        pass # Значение не изменилось
                    elif _validate_age_in_text(current_user_text, raw_age):
                        dialogue.candidate.age = raw_age
                        ctx_logger.debug(
                            f"[{dialogue.hh_response_id}] Возраст {raw_age} записан (State: {current_state_at_update}).",
                            extra={"action": "age_saved"}
                        )
                    else:
                        ctx_logger.warning(
                            f"[{dialogue.hh_response_id}] LLM придумала возраст {raw_age}, но в тексте его нет. Пропуск.",
                            extra={"action": "age_hallucination_detected"}
                        )
                else:
                    logger.debug(
                        f"[{dialogue.hh_response_id}] Игнорируем возраст {raw_age} от LLM, так как стейт '{current_state_at_update}' не подразумевает ввод возраста.",
                        extra={"action": "age_update_blocked_by_state"}
                    )

            # --- 2. ОБРАБОТКА ГРАЖДАНСТВА ---
            if extracted_data.get("citizenship"):
                # Разрешенные стейты для записи гражданства
                allowed_citizenship_states = ['awaiting_citizenship', 'clarifying_citizenship', 'clarifying_anything']
                
                if current_state_at_update in allowed_citizenship_states:
                    dialogue.candidate.citizenship = extracted_data["citizenship"]
                    ctx_logger.debug(
                        f"[{dialogue.hh_response_id}] Гражданство обновлено (State: {current_state_at_update}).",
                        extra={"action": "citizenship_saved"}
                    )
                else:
                    ctx_logger.debug(
                        f"[{dialogue.hh_response_id}] Игнорируем гражданство от LLM, так как стейт '{current_state_at_update}' это запрещает.",
                        extra={"action": "citizenship_update_blocked_by_state"}
                    )
            # if extracted_data.get("citizenship"):
            #     dialogue.candidate.citizenship = extracted_data["citizenship"]
            # if extracted_data.get("city"):
            #     dialogue.candidate.city = extracted_data["city"]
            # if extracted_data.get("readiness_to_start"):
            #     dialogue.candidate.readiness_to_start = extracted_data["readiness_to_start"]
            await db.flush()
        # ==========================================================================================
        # БЛОК ВАЛИДАЦИИ И ПРИНЯТИЯ РЕШЕНИЙ (КОД)
        # ==========================================================================================

        # Проверяем условия только если диалог еще не завершен
        if (dialogue.status not in ['qualified', 'rejected'] and  new_state in ['qualification_complete']
    #and dialogue.dialogue_state not in ['scheduling_spb_day', 'scheduling_spb_time', 'interview_scheduled_spb', 'init_scheduling_spb']  # <-- ДОБАВИТЬ
    #and new_state not in ['scheduling_spb_day', 'scheduling_spb_time', 'interview_scheduled_spb', 'init_scheduling_spb']
        and is_candidate_profile_complete(dialogue.candidate)):
            # === [START] ПРОВЕРКА: ЗАДАВАЛСЯ ЛИ ВОПРОС ПРО ТЕЛЕФОН ===
            # Проверяем только если телефона физически нет в базе
            if not dialogue.candidate.phone_number:
                
                # Ключевые слова, которые бот обязан был использовать
                phone_keywords = ["телефон", "номер"]
                
                was_phone_asked = False
                
                # Пробегаем по истории сообщений БОТА
                history_to_check = dialogue.history or []
                for msg in history_to_check:
                    if msg.get('role') == 'assistant':
                        content_lower = str(msg.get('content', '')).lower()
                        if any(kw in content_lower for kw in phone_keywords):
                            was_phone_asked = True
                            break
                
                # Если вопроса не было -> Прерываем завершение и пинаем бота
                if not was_phone_asked:
                    ctx_logger.warning(
                        f"[{dialogue.hh_response_id}] 🛑 БЛОКИРОВКА ЗАВЕРШЕНИЯ: Бот забыл спросить телефон (ключевые слова не найдены).",
                        extra={"action": "forced_phone_question"}
                    )
                    
                    system_command = {
                        'message_id': f'sys_cmd_ask_phone_force_{time.time()}',
                        'role': 'user',
                        'content': (
                            "[SYSTEM COMMAND] Ты пытаешься завершить анкету (qualification_complete), "
                            "но ты пропустил этап  'awaiting_phone' и не спросил номер телефона. "
                            "Это критическая ошибка. "
                            "Ты ОБЯЗАН спросить номер телефона. Спроси его прямо сейчас."
                        )
                    }

                    # Меняем стейт на ожидание телефона, чтобы промпт подстроился
                    dialogue.dialogue_state = 'clarifying_anything'
                    # Добавляем команду
                    dialogue.pending_messages = (dialogue.pending_messages or []) + [system_command]
                    dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
                    
                    # Сохраняем и выходим, чтобы в следующем цикле бот задал вопрос
                    await db.commit()
                    return
            # === [END] КОНЕЦ ПРОВЕРКИ ТЕЛЕФОНА ===
            # =====================================================================
            # [START] ФИНАЛЬНАЯ ВЕРИФИКАЦИЯ ДАННЫХ ПЕРЕД РЕШЕНИЕМ
            # =====================================================================
            ctx_logger.info(
                f"[{dialogue.hh_response_id}] Запуск финальной верификации данных через LLM...",
                extra={"action": "verification_start"}
            )

            # 1. Объединяем историю из БД и новые сообщения, которые обработали выше
            all_messages_for_verify = (dialogue.history or []) + user_entries_to_history

            # 2. Фильтруем системный мусор и помечаем роли
            verify_history_lines = []
            for m in all_messages_for_verify:
                content = m.get('content', '')
                if isinstance(content, str) and content.startswith('[SYSTEM COMMAND]'):
                    continue
                
                label = "Кандидат" if m.get('role') == 'user' else "Бот"
                verify_history_lines.append(f"{label}: {content}")

            full_history_text = "\n".join(verify_history_lines)

            # 2. Промпт для проверки
            verification_prompt = (
                """[SYSTEM COMMAND] Твоя задача — выполнить роль АУДИТОРА данных.
                Проанализируй ВЕСЬ диалог выше.
                Извлеки финальный возраст и гражданство кандидата.

                ПРАВИЛА НОРМАЛИЗАЦИИ ГРАЖДАНСТВА:
                1. Если кандидат гражданин РФ (России), Беларуси (РБ, Республика Беларусь), Киргизии, Армении или Казахстана -> верни строго "ЕАЭС".
                2. Если другое гражданство, но кандидат сказал, что у него есть именно ВНЖ РФ (другие документы не считаются!) -> верни строго "внж рф".
                3. Если другое гражданство, но кандидат сказал, что у него есть именно РВП РФ (другие документы не считаются!) -> верни строго "рвп рф".
                4. Во всех остальных случаях (именно только когда не подходят пункты 1, 2, 3) верни название страны как есть (например "Узбекистан", "Монголия" и так далее).
                
                Исключения (используй их только в крайних случаях!):
                1. Если кандидат в диалоге прямо наотрез отказался говорить свое гражданство (но вопрос про гражданство ему задали) то верни ЕАЭС.
                2. Если кандидат в диалоге прямо наотрез отказался говорить свой возраст (но вопрос про возраст ему задали) то верни 18

                Важно!!! Если кандидат говорит например, что ему 17, но скоро 18, это значит что ему 17лет и вернуть ты должна 17.
                Важно!!! Виза, студенческая виза, рабочая виза, патент и тд не считаются за гражданство или внж, или рвп, это другие отдельные документы, на них не обращай внимание.

                Кандидат может писать не на русском языке, это не меняет суть, не отвлекайся на это.
                
                Верни ответ ТОЛЬКО в формате JSON:
                {
                    "age": <целое число или null>,
                    "citizenship": "<строка или null>",
                    "reasoning": "<твой краткий анализ и обоснование выбора этих значений, не более двух предложений>"
                }
                """
            )

            verify_attempts = []
            verify_response = None
            
            try:
                # Вызываем LLM (используем существующий механизм)
                verify_response = await llm_handler.get_bot_response(
                    system_prompt=verification_prompt,
                    dialogue_history=[], # Историю мы уже вклеили в user_message или можно передать отдельно, но для аудита лучше подать контекстом
                    user_message=f"ИСТОРИЯ ДИАЛОГА:\n{full_history_text}\n\nПроанализируй и верни JSON.",
                    current_datetime_utc=datetime.datetime.now(datetime.timezone.utc),
                    attempt_tracker=verify_attempts,
                    skip_instructions=True,
                    extra_context=log_context
                )

                # Логируем использование токенов
                if verify_response:
                    await _log_llm_usage(
                        db, dialogue, 
                        "Final_Data_Verification", 
                        verify_response.get("usage_stats")
                    )
                    
                    # Логируем ретраи, если были
                    if len(verify_attempts) > 1:
                        for i in range(len(verify_attempts) - 1):
                            await _log_llm_usage(db, dialogue, f"Final_Data_Verification (RETRY #{i+1})")

                # Парсим ответ
                verified_data = verify_response.get('parsed_response', {}) if verify_response else {}
                verify_age = verified_data.get('age')
                verify_citizenship = verified_data.get('citizenship')
                
                # --- [END] УСПЕШНАЯ ВЕРИФИКАЦИЯ ---

            except Exception as verify_err:
                ctx_logger.error(f"[{dialogue.hh_response_id}] Финальная верификация ПРОВАЛЕНА: {verify_err}")
                for i in range(len(verify_attempts)):
                    await _log_llm_usage(db, dialogue, f"Final_Verify (FAILED #{i+1}: {type(verify_err).__name__})")
                
                # --- КРИТИЧЕСКИЙ ОБРАБОТЧИК ---
                await _handle_llm_failure(
                    db=db,
                    dialogue=dialogue,
                    recruiter=recruiter,
                    error=verify_err,
                    context="Final Data Verification",
                    pending_msgs=pending_messages,
                    ctx_logger=ctx_logger
                )
                return 

                # Сравниваем с БД
                db_age = dialogue.candidate.age
                db_citizenship = dialogue.candidate.citizenship
                
                # Логика сравнения (можно сделать мягче, например lower().strip())
                is_age_ok = (db_age == verify_age)
                
                # Для гражданства делаем нормализацию перед сравнением, если в БД записано не "ЕАЭС", а "РФ"
                # Но вы просили: "проверять совпадает ли". 
                # Если в БД "РФ", а Верификатор вернул "ЕАЭС" - это несовпадение строк, но по смыслу может быть ок.
                # НО! Раз мы хотим перестраховаться, давайте считать любое несовпадение строк инцидентом,
                # чтобы вы увидели глазами, как работает ваша система записи.
                
                # Приводим к нижнему регистру для сравнения
                def norm(s): return str(s).lower().strip() if s else "none"
                is_citizen_ok = (norm(db_citizenship) == norm(verify_citizenship))

                if not is_age_ok or not is_citizen_ok:
                    ctx_logger.warning(
                        f"[{dialogue.hh_response_id}] ВЕРИФИКАЦИЯ: Данные исправлены. Было: {db_age}/{db_citizenship}, Стало: {verify_age}/{verify_citizenship}",
                        extra={"action": "verification_auto_correct"}
                    )
                    # ЗАПИСЫВАЕМ ВЕРНЫЕ ДАННЫЕ В БД
                    if verify_age:
                        dialogue.candidate.age = verify_age
                    if verify_citizenship:
                        dialogue.candidate.citizenship = verify_citizenship
                    
                    await db.flush() # Сохраняем изменения в текущей транзакции

                    # 1. Сохраняем Pending Messages в строку JSON перед удалением
                    # ensure_ascii=False чтобы русские буквы не превратились в кракозябры
                    saved_pending_json = json.dumps(
                        dialogue.pending_messages, 
                        ensure_ascii=False, 
                        indent=2, 
                        default=str
                    )

                    # 2. Отправляем алерт в ТГ
                    await send_verification_alert(
                        dialogue_id=dialogue.id,
                        hh_response_id=dialogue.hh_response_id,
                        db_age=db_age,
                        db_citizenship=db_citizenship,
                        llm_age=verify_age,
                        llm_citizenship=verify_citizenship,
                        history_text=full_history_text,
                        pending_messages_json=saved_pending_json,
                        reasoning=verified_data.get('reasoning', "Обоснование не предоставлено")  # <--- Передаем сохраненный JSON
                    )

                    # # 3. БЛОКИРУЕМ ДИАЛОГ
                    # dialogue.status = 'admin_review' 
                    # dialogue.dialogue_state = 'verification_error' 
                    
                    # # 4. ОСТАНАВЛИВАЕМ ЦИКЛ (Очищаем очередь)
                    # dialogue.pending_messages = None
                    # dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)

                    # await db.commit()
                    # return 

                ctx_logger.info(f"[{dialogue.hh_response_id}] ✅ Верификация пройдена успешно.")

            except Exception as e:
                ctx_logger.error(f"[{dialogue.hh_response_id}] Ошибка процесса верификации: {e}", exc_info=True)
                # Если верификация упала технически - лучше не рисковать и тоже остановить процесс
                return

            # =====================================================================
            # [END] КОНЕЦ БЛОКА ВЕРИФИКАЦИИ. ДАЛЬШЕ ИДЕТ СТАРЫЙ КОД...
            # =====================================================================

            ctx_logger.info(
                f"[{dialogue.hh_response_id}] Анкета собрана полностью. Запускаю проверку критериев.",
                extra={"action": "profile_complete_start_check"}
            )

            # Передаем и кандидата, и название вакансии для проверки возраста
            is_eligible = check_candidate_eligibility(dialogue.candidate, dialogue.vacancy.title)

            # --- НОВАЯ ПРОВЕРКА НА ОГРАНИЧЕНИЯ ---
            # Собираем полную историю (из БД + то что пришло сейчас)
            # --- ЗАМЕНА СТАРОЙ ПРОВЕРКИ (Regex) НА НОВУЮ (LLM) ---
            
            # Собираем полную историю для анализа
            full_history_for_filter = (dialogue.history or []) + user_entries_to_history
            
            # Вместо: has_restrictions = _has_forbidden_restrictions(full_history_for_filter)
            # Используем LLM:
            res_rest = await _check_restrictions_via_llm(
                db=db, 
                dialogue=dialogue, 
                recruiter=recruiter,
                pending_messages=pending_messages,
                history_messages=full_history_for_filter, 
                log_extra=log_context
            )
            if res_rest is None:
                return # Сбой LLM
            has_restrictions = res_rest
            
            if has_restrictions:
                ctx_logger.info(
                    f"[{dialogue.hh_response_id}] ОТКАЗ: LLM подтвердила наличие ограничений (здоровье/язык).",
                    extra={"action": "qualification_failed_by_llm_restrictions"}
                )
                is_eligible = False # Принудительно ставим False, чтобы сработал блок отказа ниже
            # ---------------------------------------------------

            if not is_eligible:
                # --- СЦЕНАРИЙ 1: ОТКАЗ ---
                ctx_logger.info(
                    f"[{dialogue.hh_response_id}] Кандидат НЕ прошел проверку кодом (Age/Citizenship).",
                    extra={"action": "qualification_failed_by_code"}
                )

                # Принудительно меняем состояние и текст (игнорируем то, что написала LLM)
                new_state = 'qualification_failed'
                bot_response_text = "Спасибо! Я передам Вашу анкету для рассмотрения. Если по Вашей анкете будет принято положительное решение с Вами свяжутся в течение трёх рабочих дней."

                # Логика перемещения в папку 'assessment' и создания уведомления сработает ниже,
                # так как мы установили new_state = 'qualification_failed'

            else:
                # --- СЦЕНАРИЙ 2: ПОДХОДИТ ---
                ctx_logger.info(
                    f"[{dialogue.hh_response_id}] Кандидат успешно прошел проверку кодом.",
                    extra={"action": "qualification_passed_by_code"}
                )

                # Проверяем город
                city_lower = (dialogue.vacancy.city or "").lower()
                # --- ИЗМЕНЕНИЕ: Добавлены Мурино и Кудрово ---
                target_cities = ['санкт-петербург', 'мурино', 'кудрово']
                is_spb = any(x in city_lower for x in target_cities)
                # ---------------------------------------------

                if not is_spb:
                    # --- 2.1 НЕ СПБ (Регионы) ---
                    new_state = 'forwarded_to_researcher'
                    bot_response_text = """Спасибо! Я передам Вашу заявку нашим коллегам. Мы свяжемся с Вами в рабочее время, чтобы согласовать время собеседования."""
                    # Логика перемещения в папку 'interview' и смены статуса на 'qualified' сработает ниже



                else:

                    current_title_lower = (vacancy_title or "").lower()

                    # Список фраз, которые ищем в названии
                    # Список фраз, которые ищем в названии (для этих вакансий в СПб автоматическая запись не предлагается)
                    excluded_vacancies = [
                        'повар-пекарь', 'повар неполный день', 'повар', 'бариста', 
                        'уборщик', 'уборщица', 'помошник повара',
                        'сборщик онлайн заказов', 'сборщик заказов', 'сканировщик qr-кодов', 
                        'сканировщик', 'сборщик', 'комплектовщик', 'упаковщик', 'кассир-комплектовщик'
                    ]
                    # Проверяем, входит ли хоть одна фраза из списка в название вакансии
                    if any(phrase in current_title_lower for phrase in excluded_vacancies):
                        ctx_logger.info(
                            f"[{dialogue.hh_response_id}] Вакансия '{vacancy_title}' (СПб) переведена на рекрутера (исключение).",
                            extra={"action": "spb_vacancy_excluded"}
                        )
                        new_state = 'forwarded_to_researcher'
                        bot_response_text = "Спасибо! Я передам Вашу заявку нашим коллегам. Мы свяжемся с Вами в рабочее время, чтобы согласовать время собеседования."

                    else:
                        # --- 2.2 СПБ (Запись на собеседование) ---
                        ctx_logger.info(
                            f"[{dialogue.hh_response_id}] Город СПб. Кандидат подходит. Добавляю команду для LLM на запись.",
                            extra={"action": "spb_scheduling_start"}
                        )


                        # 2. ВАЖНО: Нам нужно сохранить текущие ответы пользователя в историю прямо сейчас.
                        # Так как мы прерываем цикл (return), стандартное сохранение истории в конце функции не сработает.
                        # Если этого не сделать, бот "забудет", что кандидат только что ответил про возраст/гражданство.
                        current_history = list(dialogue.history) if dialogue.history else []
                        # user_entries_to_history мы сформировали в начале функции
                        dialogue.history = (current_history + user_entries_to_history)[-350:]

                        # 3. Формируем скрытую команду для LLM
                        # Используем role='system' или 'user' с пометкой, чтобы направить LLM.
                        system_command = {
                            'message_id': f'sys_cmd_{time.time()}',
                            'role': 'user',
                            'content': '[SYSTEM COMMAND] Кандидат прошел квалификацию. Начни запись на собеседование в Санкт-Петербурге (предложи выбрать день).'
                        }

                        # 4. Кладем команду в pending_messages
                        # Мы перезаписываем очередь, убирая оттуда сообщения пользователя (они уже в истории)
                        # и оставляя только нашу команду.
                        dialogue.pending_messages = [system_command]
                        dialogue.dialogue_state = 'init_scheduling_spb'
                        # 5. Обновляем время (last_updated), чтобы воркер подхватил диалог в следующем цикле мгновенно
                        dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)

                        # 6. Сохраняем и ПРЕРЫВАЕМ текущую обработку.
                        # Мы не отправляем сообщение bot_response_text из этого цикла, так как ждем,
                        # что LLM сгенерирует приглашение в ответ на нашу команду.

                        await db.commit()
                        return
        elif (dialogue.status not in ['qualified', 'rejected'] and  new_state in ['qualification_complete']
    #and dialogue.dialogue_state not in ['scheduling_spb_day', 'scheduling_spb_time', 'interview_scheduled_spb', 'init_scheduling_spb']  # <-- ДОБАВИТЬ
    #and new_state not in ['scheduling_spb_day', 'scheduling_spb_time', 'interview_scheduled_spb', 'init_scheduling_spb']
        and not is_candidate_profile_complete(dialogue.candidate)):


            missing_fields = []
            if not dialogue.candidate.citizenship:
                missing_fields.append("Гражданство")
            if dialogue.candidate.age is None:
                missing_fields.append("Возраст")
            
            missing_str = ", ".join(missing_fields)
            command_content = (
                f"[SYSTEM COMMAND] Анкета кандидата не заполнена полностью. "
                f"В базе данных отсутствуют обязательные поля: {missing_str}. "
                f"1. Проверь историю сообщений: возможно, кандидат уже называл эти данные, тогда просто извлеки их в 'extracted_data'. "
                f"2. Если данных в истории нет, ты ОБЯЗАН задать прямой вопрос кандидату и уточнить: {missing_str}. "
                f"НЕ ПЕРЕХОДИ в состояние qualification_complete, пока не получишь эти данные.")
                

            system_command = {
                'message_id': f'sys_cmd_{time.time()}',
                'role': 'user',
                'content': command_content
            }
            dialogue.history = (dialogue.history or []) + user_entries_to_history
            dialogue.pending_messages =  [system_command]
            dialogue.dialogue_state = 'clarifying_anything'
            # важно обновить last_updated, чтобы воркер как можно скорее обработал это изменение
            dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
            await db.commit()
            ctx_logger.info(f"[{dialogue.hh_response_id}] Анкета не полная. Не хватает: {missing_str}. Отправлено на уточнение.")
            return

            #Сюда надо код, который даст команду llm (просто положит в пендинг сообщение с ролью user что анкета не полная, кандидат не сообщил какие то данные, проверь какие данные и запроси их)
        # ==========================================================================================
        # КОНЕЦ БЛОКА ВАЛИДАЦИИ
        # ==========================================================================================

        #Вот сюда надо написать код, который будет проверять, заполнены ли в БД у кандидата все необходимые поля (Номер телефона, Гражданство, Возраст, Готовность выйти на работу, город)
        # Если да, то вызов функции проверки соответствия кандидата
        # Если не подходит, то перевод в состояние 'qualification_failed' и текст сообщения «Спасибо! Я передам Вашу анкету для рассмотрения. Если по Вашей анкете будет принято положительное решение с Вами свяжутся в течение трёх рабочих дней.»
        # Если подходит и город не СПб, то перевод в состояние 'forwarded_to_researcher' и текст сообщения «Спасибо! Я передам Вашу заявку нашим коллегам. Мы свяжемся с Вами в рабочее время, чтобы согласовать время собеседования».
        # Если подходит и город СПб, то перевод в состояние 'init_scheduling_spb' и запрос к llm с добавкой "Начни запись кандидата на собеседование в Санкт-Петербурге."

        # Обработка квалификации

        # --- НОВЫЙ БЛОК: Обработка состояния call_later ---
        if new_state == 'call_later':
            # Благодаря selectinload(Dialogue.inactive_alerts) в начале функции,
            # мы можем проверить наличие записи через атрибут
            if not dialogue.inactive_alerts:
                db.add(InactiveNotificationQueue(
                    dialogue_id=dialogue.id, 
                    status='pending'
                ))
                ctx_logger.info(
                    f"[{dialogue.hh_response_id}] Переход в state 'call_later'. Добавлена запись в InactiveNotificationQueue.",
                    extra={"action": "state_call_later_added"}
                )
            else:
                ctx_logger.debug(
                    f"[{dialogue.hh_response_id}] State 'call_later', но диалог уже есть в таблице молчунов. Пропускаем.",
                    extra={"action": "state_call_later_skip_duplicate"}
                )
        # --------------------------------------------------

        if new_state in ['forwarded_to_researcher', 'interview_scheduled_spb'] and dialogue.status == 'qualified':
            
            
            if new_state == 'interview_scheduled_spb':
                interview_date = extracted_data.get("interview_date")
                interview_time = extracted_data.get("interview_time")

            # [START] ОБРАБОТКА ПЕРЕНОСА (RESCHEDULE)
            # Если у диалога УЖЕ была записана дата (interview_datetime_utc не пустой), 
            # значит кандидат перезаписывается.
                if dialogue.interview_datetime_utc is not None:

                    # 1. ПРОВЕРКА: Действительно ли дата изменилась?
                    # Конвертируем UTC из БД в Московское время и форматируем в строки для сравнения
                    stored_msk_dt = dialogue.interview_datetime_utc.astimezone(SPB_TIMEZONE)
                    stored_date_str = stored_msk_dt.strftime('%Y-%m-%d')
                    stored_time_str = stored_msk_dt.strftime('%H:%M')
                    
                    # Флаг изменения данных
                    is_date_changed = (stored_date_str != interview_date) or (stored_time_str != interview_time)

                    if is_date_changed:
                        ctx_logger.info(
                            f"[{dialogue.hh_response_id}] 🔄 Обнаружен перенос собеседования. "
                            f"Было: {stored_date_str} {stored_time_str}. Стало: {interview_date} {interview_time}."
                        )

                        # 1. Если это перенос после Follow-up (по флагу) -> RescheduledNotificationQueue
                        if dialogue.need_follow_up_resheduled_sent:
                            ctx_logger.info(
                                f"[{dialogue.hh_response_id}] Флаг need_follow_up_resheduled_sent=True. "
                                f"Создаю уведомление в RescheduledNotificationQueue."
                            )
                        
                            if dialogue.rescheduled_alerts:
                                # Обновляем существующую запись
                                if dialogue.rescheduled_alerts.status != 'pending':
                                    dialogue.rescheduled_alerts.status = 'pending'
                                    dialogue.rescheduled_alerts.processed_at = None
                                    db.add(dialogue.rescheduled_alerts)
                            else:
                                # Создаем новую
                                new_resched_alert = RescheduledNotificationQueue(
                                    dialogue_id=dialogue.id,
                                    status='pending'
                                )
                                db.add(new_resched_alert)

                            dialogue.need_follow_up_resheduled_sent = False
                            dialogue.dialogue_state = 'post_qualification_chat'
                            new_state = 'post_qualification_chat'
                            ctx_logger.info(f"[{dialogue.hh_response_id}] Счетчик переносов увеличен: {dialogue.reschedule_count}")
                            dialogue.reschedule_count = (dialogue.reschedule_count or 0) + 1
                    
                        # 2. Иначе (обычный перенос) -> DeclinedInterviewNotificationQueue
                        else:
                            ctx_logger.info(
                                f"[{dialogue.hh_response_id}] Обычный перенос (флаг False). "
                                f"Создаю уведомление в DeclinedInterviewNotificationQueue."
                            )
                            
                            if dialogue.declined_interview_alerts:
                                # Обновляем существующую запись
                                if dialogue.declined_interview_alerts.status != 'pending':
                                    dialogue.declined_interview_alerts.status = 'pending'
                                    dialogue.declined_interview_alerts.processed_at = None
                                    db.add(dialogue.declined_interview_alerts)
                            else:
                                # Создаем новую
                                reschedule_alert = DeclinedInterviewNotificationQueue(
                                    dialogue_id=dialogue.id,
                                    status='pending'
                                )
                                
                                db.add(reschedule_alert)
                            dialogue.dialogue_state = 'post_qualification_chat'
                            new_state = 'post_qualification_chat'

                        # --- !!! ВАЖНОЕ ДОБАВЛЕНИЕ !!! ---
                        # Перезаписываем напоминания на новое время
                        if interview_date and interview_time:
                            ctx_logger.info(f"[{dialogue.hh_response_id}] Перепланирование напоминаний на НОВУЮ дату...")
                            await interview_reminder_manager.schedule_interview_reminders(
                                dialogue_id=dialogue.id,
                                interview_date_str=interview_date,
                                interview_time_str=interview_time,
                                db_session=db
                            )
                        # ---------------------------------

                        dialogue.dialogue_state = 'post_qualification_chat'
                        new_state = 'post_qualification_chat'

                    else:
                        # Дата НЕ изменилась (кандидат просто подтвердил то же самое или задал вопрос)
                        ctx_logger.info(
                            f"[{dialogue.hh_response_id}] ℹ️ Дата и время собеседования совпадают с БД ({stored_date_str} {stored_time_str}). "
                            f"Логика переноса (Reschedule) ПРОПУЩЕНА."
                        )
                    
                        # Всегда переводим в чат поддержки после обработки (даже если дата не менялась)
                        dialogue.dialogue_state = 'post_qualification_chat'
                        new_state = 'post_qualification_chat'    
                        # [END] ОБРАБОТКА ПЕРЕНОСА
                
                
                else:
                    if interview_date and interview_time:
                        ctx_logger.info(
                            f"Собеседование запланировано для диалога {dialogue.id} на "
                            f"{interview_date} в {interview_time} (СПБ). Планирую напоминания."
                        )
                        await interview_reminder_manager.schedule_interview_reminders(
                            dialogue_id=dialogue.id,
                            interview_date_str=interview_date,
                            interview_time_str=interview_time,
                            db_session=db
                        )
                        dialogue.dialogue_state = 'post_qualification_chat' 
                        new_state = 'post_qualification_chat'  # Переходим в пост-квалификационный чат после планирования
                        
                    else:
                        ctx_logger.error(
                            f"LLM установил 'interview_scheduled_spb', но не предоставил "
                            f"interview_date или interview_time для диалога {dialogue.id}. Напоминания не будут запланированы."
                        )

        # --- КОНЕЦ ДОБАВЛЕНИЯ ---

        if new_state in ['forwarded_to_researcher', 'interview_scheduled_spb'] and dialogue.status != 'qualified':
            dialogue.status = 'qualified'
            await log_analytics_event(db, dialogue, 'interview_assigned')
            # ------------------------------------------------------
            await statistics_manager.update_stats(db, dialogue.vacancy_id, qualified=1)

            # Проверка существования уведомления (оптимизированная)
            exists_query = select(func.count()).select_from(NotificationQueue).filter_by(
                candidate_id=dialogue.candidate.id,
                status='pending'
            )
            result = await db.execute(exists_query)

            if result.scalar() == 0:
                db.add(NotificationQueue(candidate_id=dialogue.candidate.id, status='pending'))

            ctx_logger.info(f"Candidate {dialogue.hh_response_id} qualified 🟢. Moving to 'interview'.")

            api_move_start = time.monotonic()
            await hh_api.move_response_to_folder(recruiter, db, dialogue.hh_response_id, 'interview')
            ctx_logger.debug(
                f"[{dialogue.hh_response_id}] API move: {time.monotonic() - api_move_start:.2f} sec.",
                extra={"action": "api_move_completed", "duration": time.monotonic() - api_move_start}
            )

            # --- ДОБАВИТЬ ЭТОТ БЛОК КОДА ---
            if new_state == 'interview_scheduled_spb':
                interview_date = extracted_data.get("interview_date")
                interview_time = extracted_data.get("interview_time")



                if interview_date and interview_time:
                    ctx_logger.info(
                        f"Собеседование запланировано для диалога {dialogue.id} на "
                        f"{interview_date} в {interview_time} (СПБ). Планирую напоминания."
                    )
                    await interview_reminder_manager.schedule_interview_reminders(
                        dialogue_id=dialogue.id,
                        interview_date_str=interview_date,
                        interview_time_str=interview_time,
                        db_session=db
                    )
                    dialogue.dialogue_state = 'post_qualification_chat'
                    new_state = 'post_qualification_chat'  # Переходим в пост-квалификационный чат после планирования
                    
                else:
                    ctx_logger.error(
                        f"LLM установил 'interview_scheduled_spb', но не предоставил "
                        f"interview_date или interview_time для диалога {dialogue.id}. Напоминания не будут запланированы."
                    )
            # --- КОНЕЦ ДОБАВЛЕНИЯ ---


        elif (new_state == 'qualification_failed' or new_state == 'declined_vacancy' or new_state == 'declined_interview'):

            if new_state == 'declined_vacancy':
                # --- ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА ОТКАЗА ---
                
                # 1. Объединяем историю и текущие (pending) сообщения
                all_msgs = (dialogue.history or []) + (dialogue.pending_messages or [])
                
                # 2. Фильтруем и добавляем метки ролей
                clean_history_with_roles = []
                for m in all_msgs:
                    if not isinstance(m, dict):
                        continue
                        
                    content = m.get('content', '')
                    role = m.get('role', '')

                    # Пропускаем технические команды
                    if content.startswith("[SYSTEM COMMAND]"):
                        continue
                    
                    # Определяем понятную метку роли
                    role_label = "Кандидат" if role == "user" else "Бот"
                    
                    clean_history_with_roles.append(f"{role_label}: {content}")
                
                # 3. Берем последние 20 сообщений
                recent_context_text = "\n".join(clean_history_with_roles[-20:])
                full_context_for_llm = recent_context_text.strip()

                clarification_prompt = (
                    'Проанализируй диалог и определи: действительно ли кандидат чётко отказался от вакансии? '
                    'Смотри только на реплики с пометкой "Кандидат". '
                    'Верни ответ строго в формате JSON: {"answer": "yes" или "no"} '
                    'Ответ "yes" — только если кандидат прямо сказал, что вакансия его не интересует или он отказывается. '
                    'Если кандидат задает вопросы или сомневается — верни "no".'
                )

                clarification_attempts = []
                clarification_result = None
                try:
                    clarification_result = await llm_handler.get_bot_response(
                        system_prompt=clarification_prompt,
                        dialogue_history=[], 
                        user_message=f"ИСТОРИЯ ДИАЛОГА (последние реплики):\n{full_context_for_llm}",
                        current_datetime_utc=datetime.datetime.now(datetime.timezone.utc),
                        attempt_tracker=clarification_attempts,
                        skip_instructions=True,
                        extra_context=log_context
                    )

                    # === УСПЕШНЫЙ ВЫЗОВ ===
                    total_attempts = len(clarification_attempts)
                    failed_attempts = total_attempts - 1
                    if failed_attempts > 0:
                        ctx_logger.warning(
                            f"[{dialogue.hh_response_id}] Уточнение declined_vacancy: {failed_attempts} скрытых ретраев.",
                            extra={"action": "decline_check_retries"}
                        )
                        for i in range(failed_attempts):
                            await _log_llm_usage(db, dialogue, f"DeclineClarification (RETRY #{i+1})")

                    # === ЛОГИРОВАНИЕ ТОКЕНОВ ===
                    if clarification_result and 'usage_stats' in clarification_result:
                        await _log_llm_usage(
                            db, dialogue, 
                            "DeclineClarification", 
                            clarification_result.get('usage_stats')
                        )

                except Exception as e:
                    # === ПОЛНЫЙ ПРОВАЛ ===
                    ctx_logger.error(
                        f"[{dialogue.hh_response_id}] Ошибка при уточнении 'declined_vacancy' (после всех попыток): {e}",
                        extra={"action": "decline_check_failed"}
                    )
                    
                    for i in range(len(clarification_attempts)):
                        await _log_llm_usage(
                            db, dialogue, 
                            f"DeclineClarification (FAILED #{i+1}: {type(e).__name__})"
                        )
                    
                    # --- КРИТИЧЕСКИЙ ОБРАБОТЧИК ---
                    await _handle_llm_failure(
                        db=db,
                        dialogue=dialogue,
                        recruiter=recruiter,
                        error=e,
                        context="Decline Vacancy Clarification",
                        pending_msgs=pending_messages,
                        ctx_logger=ctx_logger
                    )
                    return 

                is_real_decline = False
                if clarification_result and 'parsed_response' in clarification_result:
                    try:
                        parsed = clarification_result['parsed_response']
                        is_real_decline = (parsed.get('answer') == 'yes')
                    except Exception as e:
                        ctx_logger.warning(
                            f"[{dialogue.hh_response_id}] Ошибка парсинга JSON при уточнении 'declined_vacancy': {e}",
                            extra={"action": "decline_check_json_error"}
                        )

                if not is_real_decline:
                    # Кандидат НЕ отказался → прерываем текущую обработку
                    system_command = {
                        'message_id': f'sys_cmd_recheck_decline_{time.time()}',
                        'role': 'user',
                        'content': '[SYSTEM COMMAND] Сейчас кандидат не отказывается от вакансии и анкетирования, не ставь declined_vacancy, продолжай анкету дальше.'
                    }
                    
                    dialogue.pending_messages = (dialogue.pending_messages or []) + [system_command]
                    dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
                    await db.commit()
                    ctx_logger.info(
                        f"[{dialogue.hh_response_id}] Отказ от вакансии НЕ подтверждён. Отложен системный запрос для повторной обработки.",
                        extra={"action": "decline_not_confirmed"}
                    )
                    return  # ← ВАЖНО: выходим из функции, НЕ переводя в статус 'rejected'
                else:
                    ctx_logger.info(
                        f"[{dialogue.hh_response_id}] Отказ от вакансии подтверждён LLM.",
                        extra={"action": "decline_confirmed"}
                    )
                # --- КОНЕЦ ДОПОЛНИТЕЛЬНОЙ ПРОВЕРКИ ---

            event_type = 'rejected_by_bot' 
            if new_state in ['declined_vacancy', 'declined_interview']:
                event_type = 'refusal_by_user'
        
            # --- [ANALYTICS] ОТКАЗ (МЫ ИЛИ ОН) ---
            # Запишется либо rejected_by_bot, либо refusal_by_user
            await log_analytics_event(db, dialogue, event_type)
            # -------------------------

            dialogue.status = 'rejected'
            # --- ДОБАВЛЕННЫЙ БЛОК: Отмена напоминаний при отказе ---
            if new_state == 'declined_interview':
                
                await db.execute(
                    update(InterviewReminder)
                    .where(InterviewReminder.dialogue_id == dialogue.id)
                    .where(InterviewReminder.status == 'pending')
                    .values(
                        status='cancelled', 
                        processed_at=datetime.datetime.now(datetime.timezone.utc)
                    )
                )
                ctx_logger.info(
                    f"[{dialogue.hh_response_id}] Статус 'declined_interview': все запланированные напоминания отменены.",
                    extra={"action": "reminders_cancelled_due_to_decline"}
                )
            # -------------------------------------------------------

            




            # --- ИСПРАВЛЕННЫЙ БЛОК КОДА ---
            # Проверяем, существует ли уже запись RejectedNotificationQueue для этого диалога.
            # `dialogue.rejected_alerts` будет либо объектом RejectedNotificationQueue, либо None,
            # благодаря `uselist=False` в relationship.
            if dialogue.inactive_alerts:
                ctx_logger.info(
                    f"[{dialogue.hh_response_id}] Кандидат уже в таблице молчунов. Пропускаю запись в отказники.",
                    extra={"action": "skip_rejected_queue_exists_in_inactive"}
                )
            else:
                # СЦЕНАРИЙ 1: Отказ от собеседования -> ОТДЕЛЬНАЯ ОЧЕРЕДЬ
                if new_state == 'declined_interview':
                    if dialogue.declined_interview_alerts:
                         # Запись уже существует
                        if dialogue.declined_interview_alerts.status != 'pending':
                            dialogue.declined_interview_alerts.status = 'pending'
                            dialogue.declined_interview_alerts.processed_at = None
                            db.add(dialogue.declined_interview_alerts)
                    else:
                        # Создаем новую запись
                        new_declined_alert = DeclinedInterviewNotificationQueue(
                            dialogue_id=dialogue.id,
                            status='pending'
                        )
                        db.add(new_declined_alert)
                        ctx_logger.info(
                            f"Добавлено уведомление 'Отказ от собеседования' (диалог {dialogue.hh_response_id}) "
                            f"в DeclinedInterviewNotificationQueue."
                        )
                
                # СЦЕНАРИЙ 2: Обычный отказ или провал квалификации -> REJECTED QUEUE
                else: 
                    if dialogue.rejected_alerts:
                        if dialogue.rejected_alerts.status != 'pending':
                            dialogue.rejected_alerts.status = 'pending'
                            dialogue.rejected_alerts.processed_at = None
                            db.add(dialogue.rejected_alerts)
                    else:
                        new_rejected_alert = RejectedNotificationQueue(
                            dialogue_id=dialogue.id,
                            status='pending'
                        )
                        db.add(new_rejected_alert)
                        ctx_logger.info(
                            f"Добавлено уведомление об отклоненном кандидате (диалог {dialogue.hh_response_id}) "
                            f"в RejectedNotificationQueue."
                        )
            # --- КОНЕЦ НОВОЙ ЛОГИКИ ---



            ctx_logger.info(f"Candidate {dialogue.hh_response_id} rejected 🔴. Moving to 'assessment'.")

            api_move_start = time.monotonic()
            await hh_api.move_response_to_folder(recruiter, db, dialogue.hh_response_id, 'assessment')
            ctx_logger.debug(
                f"[{dialogue.hh_response_id}] API move: {time.monotonic() - api_move_start:.2f} sec.",
                extra={"action": "api_move_completed", "duration": time.monotonic() - api_move_start}
            )

        # Если LLM не вернула текст, не отправляем сообщение
        # Если LLM не вернула текст
        if bot_response_text is None or bot_response_text.strip() == "":
            
            # СЦЕНАРИЙ 1: ШТАТНОЕ МОЛЧАНИЕ
            # Согласно твоему промпту, при qualification_complete response_text должен быть null.
            # Если мы попали сюда с этим стейтом, значит валидация (is_candidate_profile_complete) 
            # прошла, но не перевела кандидата в forwarded/failed (например, ждем чего-то еще).
            # В этом случае молчание допустимо.
            if new_state == 'qualification_complete':
                ctx_logger.info(
                    f"[{dialogue.hh_response_id}] LLM вернула пустой ответ для 'qualification_complete'. Это соответствует правилам.",
                    extra={"action": "empty_response_allowed"}
                )
                
                # Сохраняем историю и стейт, но не отправляем сообщение
                new_history = (dialogue.history or []) + user_entries_to_history
                dialogue.history = new_history[-350:]
                dialogue.dialogue_state = new_state
                dialogue.pending_messages = None
                dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)

                await db.commit()
                return

            # СЦЕНАРИЙ 2: ОШИБОЧНОЕ МОЛЧАНИЕ
            # Если стейт подразумевает диалог (awaiting_age, awaiting_phone, scheduling и т.д.),
            # а бот молчит — это ошибка генерации.
            else:
                
                ctx_logger.warning(
                    f"[{dialogue.hh_response_id}] LLM вернула пустоту для стейта '{new_state}'. Отправляю системную команду на перегенерацию.",
                    extra={"action": "empty_response_fix_injected", "state": new_state}
                )
                
                # 1. Сначала сохраняем то, что пользователь написал в этом цикле, 
                # чтобы эти сообщения не пропали и бот их "увидел" в истории.
                dialogue.history = (dialogue.history or []) + user_entries_to_history

                # 2. Формируем корректирующую команду
                correction_cmd = {
                    'message_id': f'sys_empty_corr_{time.time()}',
                    'role': 'user',
                    'content': (
                        f"[SYSTEM COMMAND] В своем последнем ответе ты хотел оставить пустым поле 'response_text', "
                        f"Для состояния {new_state} ТЕКСТ ОТВЕТА ОБЯЗАТЕЛЕН."
                    )
                }

                # 3. Передаем команду в pending_messages
                dialogue.pending_messages = [correction_cmd]
                
                # 4. Обновляем время, чтобы воркер сразу подхватил диалог снова
                dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)

                # 5. Фиксируем изменения. Теперь входные данные для LLM изменятся!
                await db.commit()
                return 

        # Отправка сообщения
        message_sent = await hh_api.send_message(recruiter, db, dialogue.hh_response_id, bot_response_text)

        if message_sent == 200:
            # Генерируем время ответа бота по МСК
            bot_response_time_msk = datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK') # <-- ДОБАВЛЕНО
            bot_message_entry = {
                'message_id': f'bot_{time.time()}',
                'role': 'assistant',
                'content': bot_response_text,
                'timestamp_msk': bot_response_time_msk, # <-- ДОБАВЛЕНО
                'extracted_data': extracted_data,
                'state': new_state
            }

            # Ограничиваем размер истории
            MAX_HISTORY_LENGTH = 150
            new_history = (dialogue.history or []) + user_entries_to_history + [bot_message_entry]
            dialogue.history = new_history[-MAX_HISTORY_LENGTH:]

            dialogue.dialogue_state = new_state
            dialogue.pending_messages = None
            dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
            # === ПРОВЕРКА АНОМАЛЬНОЙ СТОИМОСТИ (ЧЕРЕЗ REDIS) ===
            try:
                cost_info = await llm_handler.get_dialogue_cost_info(dialogue.id)
                total_rub = cost_info.get("rub", 0.0)
                total_usd = cost_info.get("usd", 0.0)
                
                if total_rub >= 10.0:
                    # Ключ в редисе, который скажет нам, что мы УЖЕ слали алерт по этому диалогу
                    # Ставим время жизни 3 дня (259200 сек), чтобы ключ сам удалился
                    alert_key = f"cost_alert_sent:{dialogue.id}"

                    # acquire_lock вернет True, если ключа нет (и создаст его),
                    # или False, если ключ уже есть.
                    already_sent = not await acquire_lock(alert_key, timeout=259200)

                    if not already_sent:
                        ctx_logger.warning(f"💸 Стоимость диалога {total_rub:.2f} руб. Отправляю алерт.")
                        await send_cost_alert(
                            dialogue_id=dialogue.id,
                            hh_response_id=dialogue.hh_response_id,
                            total_cost_rub=total_rub,
                            total_cost_usd=total_usd,
                            history=dialogue.history or [],
                            recruiter_name=recruiter.name if recruiter else "Неизвестен",
                            vacancy_title=dialogue.vacancy.title if dialogue.vacancy else "Неизвестна"
                        )
                    else:
                        ctx_logger.debug("Алерт стоимости уже отправлялся ранее, пропускаем.")
            except Exception as cost_alert_err:
                ctx_logger.error(f"Ошибка в блоке алерта стоимости (Redis): {cost_alert_err}")

            # Flush для проверки constraint violations перед commit
            await db.flush()
            await db.commit()

            ctx_logger.info(
                f"Dialogue {dialogue.hh_response_id} processed successfully",
                extra={"action": "dialogue_processed_success", "new_state": new_state}
            )
        elif message_sent == 403:
            ctx_logger.warning(f"Failed to send message for dialogue {dialogue.hh_response_id}. Clearing pending messages to avoid loop.")
            dialogue.pending_messages = None
            dialogue.status = 'vacancy_closed_or_resume_not_found'
            await db.commit() # Сохраняем сброс очереди сообщений
            return
        else:
            ctx_logger.error(f"Failed to send message for dialogue {dialogue.hh_response_id}")
            await db.rollback()
            return

    except Exception as e:
        # ctx_logger уже содержит все нужные ID, если ошибка случилась после их загрузки
        ctx_logger.error(
            f"Critical error processing dialogue: {e}", 
            exc_info=True, 
            extra={"action": "process_dialogue_critical_error"}
        )
        if db and db.is_active:
            await db.rollback()
        raise

    finally:
        # === ОСВОБОЖДЕНИЕ БЛОКИРОВКИ ===
        try:
            await release_lock(lock_key)
        except Exception as release_error:
            logger.error(f"Failed to release lock for dialogue {dialogue_id}: {release_error}")
        # ================================
        logger.debug(f"[{log_dialogue_hh_response_id}] Processing finished in: {time.monotonic() - dialogue_processing_start_time:.2f} sec.")

async def process_pending_dialogues(recruiter_id: int, prompt_library: dict, db: None):
    """
    Обновленная версия - каждый диалог в своей сессии.
    Параметр db больше не используется.
    """
    function_start_time = time.monotonic()

    # 1. Создаем контекст рекрутера
    rec_log_context = {
        "recruiter_id": recruiter_id,
        "worker": "handler",
        "action": "batch_processing"
    }
    # 2. Создаем адаптер
    rec_logger = logging.LoggerAdapter(logger, rec_log_context)

    try:
        # ЗАМЕНА: logger -> rec_logger
        rec_logger.debug(f"Stage 3: Finding pending dialogues for recruiter {recruiter_id}...")
        debounce_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=DEBOUNCE_DELAY_SECONDS)

        # Быстрый запрос за списком ID
        async with SessionLocal() as lookup_db:
            db_query_start = time.monotonic()
            # --- ИСПРАВЛЕННЫЙ ЗАПРОС ---
            dialogues_info_result = await lookup_db.execute(
                select(Dialogue.id, Dialogue.hh_response_id)
                .join(Dialogue.vacancy)  # <--- 1. ПРИСОЕДИНЯЕМ ТАБЛИЦУ ВАКАНСИЙ
                .filter(
                    Dialogue.recruiter_id == recruiter_id,

                    # <--- 2. ВАЖНАЯ ПРОВЕРКА: Вакансия всё еще привязана к этому рекрутеру?
                    # Если get_all_active_vacancies... поставила NULL, этот диалог не попадет в выборку.
                    Vacancy.recruiter_id == recruiter_id,

                    Dialogue.last_updated <= debounce_time,
                    Dialogue.pending_messages.is_not(None),

                    # Проверка массива сообщений (как было у вас)
                    case(
                        (
                            func.jsonb_typeof(Dialogue.pending_messages) == 'array',
                            func.jsonb_array_length(Dialogue.pending_messages) > 0
                        ),
                        else_=False
                    )
                )
            )
            # -------------------------
            dialogues_to_process_info = dialogues_info_result.all()
            rec_logger.debug(
                f"DB query finished. Found {len(dialogues_to_process_info)} dialogues",
                extra={"query_duration": time.monotonic() - db_query_start}
            )
        rec_logger.debug(f"SQL found {len(dialogues_to_process_info)} potential dialogues to process")
        if not dialogues_to_process_info:
            rec_logger.debug(f"Starting parallel processing for {len(dialogues_to_process_info)} dialogues")
            return

        rec_logger.info(f"Found {len(dialogues_to_process_info)} dialogues for parallel processing")

        dialogue_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DIALOGUES)

        async def run_dialogue_task_with_semaphore(dialogue_id, rec_id, prompt_lib):
            """Каждый диалог получает СВОЮ сессию"""
            async with dialogue_semaphore:
                async with SessionLocal() as task_db_session:
                    try:
                        await _process_single_dialogue(dialogue_id, rec_id, prompt_lib, task_db_session)
                    except Exception as e:
                        rec_logger.error(
                            f"Dialogue {dialogue_id} processing failed: {e}", 
                            exc_info=True,
                            extra={"dialogue_id": dialogue_id, "action": "single_dialogue_error"}
                        )
                        # Ошибка одного диалога не влияет на другие

        gather_start = time.monotonic()
        tasks = [
            run_dialogue_task_with_semaphore(d_id, recruiter_id, prompt_library)
            for d_id, hh_id in dialogues_to_process_info
        ]

        # return_exceptions=True - ошибки не прерывают обработку других диалогов
        results = await asyncio.gather(*tasks, return_exceptions=True)

        success_count = sum(1 for r in results if not isinstance(r, Exception))
        error_count = len(results) - success_count

        rec_logger.debug(
            f"Batch processing complete. Results: {success_count} success, {error_count} errors",
            extra={
                "batch_success_count": success_count,
                "batch_error_count": error_count,
                "batch_duration": time.monotonic() - gather_start
            }
        )

    finally:
        rec_logger.debug(
            "process_pending_dialogues function finished",
            extra={"total_function_duration": time.monotonic() - function_start_time}
        )




async def handle_single_recruiter(rec_id: int, prompt_library: dict):
    # Контекст верхнего уровня
    rec_logger = logging.LoggerAdapter(logger, {"recruiter_id": rec_id, "worker": "handler"})
    try:
        async with SessionLocal() as check_db:
            result = await check_db.execute(
                select(TrackedRecruiter.name, TrackedRecruiter.access_token).filter_by(id=rec_id)
            )
            recruiter_data = result.first()
            if not recruiter_data or not recruiter_data[1]:
                rec_logger.warning(f"Recruiter {rec_id} has no token or not found")
                return
            
            # Добавим имя в контекст для будущих логов
            rec_logger.extra["recruiter_name"] = recruiter_data[0]

        await process_pending_dialogues(rec_id, prompt_library, None)

    except Exception as e:
        rec_logger.error(f"Worker error for recruiter: {e}", exc_info=True)


async def run_worker_cycle(target_recruiter_ids=None):
    """Главный цикл, который запускает независимые асинхронные задачи."""
    cycle_start_time = time.monotonic()
    try:
        logger.info("Начало нового цикла воркера.")
        prompt_library = knowledge_base.get_prompt_library()

        all_recruiters_ids = []
        async with SessionLocal() as db: # Эта сессия только для получения списка ID рекрутеров
            try:
                query = select(TrackedRecruiter.id).filter(TrackedRecruiter.is_auth_active == True)
                if target_recruiter_ids:
                    query = query.filter(TrackedRecruiter.id.in_(target_recruiter_ids))
                
                result = await db.execute(query)
                all_recruiters_ids = result.scalars().all()
            finally:
                pass # Сессия 'db' закрывается здесь.

        if not all_recruiters_ids:
            logger.warning("Нет отслеживаемых рекрутеров в БД. Цикл пропущен.")
            return

        recruiter_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RECRUITERS)

        # Теперь передаем только ID рекрутера, а handle_single_recruiter загрузит его в свою сессию
        tasks = [handle_single_recruiter(recruiter_id, prompt_library) for recruiter_id in all_recruiters_ids]

        async def run_task_with_semaphore(task_coro):
            async with recruiter_semaphore:
                await task_coro

        await asyncio.gather(*[run_task_with_semaphore(task) for task in tasks])

    except Exception as e:
        logger.critical("Критическая ошибка в главном цикле воркера!", exc_info=True)
    finally:
        cycle_end_time = time.monotonic()
        logger.debug(f"Цикл воркера завершен. Общее время: {cycle_end_time - cycle_start_time:.2f} сек.")
        logger.debug("Цикл воркера завершен.")

async def main(target_recruiter_ids=None):
    """Главная асинхронная функция."""

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("HH-Worker запускается...")
    # --- ДОБАВИТЬ ЭТУ СТРОКУ ---
    # interview_reminders_task = asyncio.create_task(check_and_send_interview_reminders())
    # --- КОНЕЦ ДОБАВЛЕНИЯ ---
    try:
        while not shutdown_requested:
            try:
                await run_worker_cycle(target_recruiter_ids)

                logger.debug(f"Пауза {CYCLE_PAUSE_SECONDS} секунд перед следующим циклом.")

                for _ in range(CYCLE_PAUSE_SECONDS):
                    if shutdown_requested:
                        break
                    await asyncio.sleep(1)

            except Exception as e:
                logger.critical(f"Неперехваченная критическая ошибка в главном цикле: {e}", exc_info=True)
                if not shutdown_requested:
                    await asyncio.sleep(120)
    finally:
        logger.info("Закрываем соединения...")
        await cleanup() # Очистка LLM ресурсов
        # --- ДОБАВИТЬ ЭТУ СТРОКУ ---
        await hh_api_real.close_api_client()
        await close_redis()
        # ---------------------------
        # --- ДОБАВИТЬ ЭТУ СТРОКУ ---
        # interview_reminders_task.cancel() # Отмена задачи при завершении
        # --- КОНЕЦ ДОБАВЛЕНИЯ ---
        logger.info("HH-Worker полностью остановлен.")




if __name__ == "__main__":
    setup_logging(log_filename="hh_worker.log")
    load_dotenv()
    init_sentry()
    start_metrics_server(8000)

    # Настраиваем чтение аргументов
    parser = argparse.ArgumentParser(description="HH Worker instance")
    parser.add_argument('--recruiter_ids', nargs='+', type=int, help='List of recruiter IDs to handle')
    args = parser.parse_args()

    # Передаем список ID в функцию main
    try:
        asyncio.run(main(target_recruiter_ids=args.recruiter_ids))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Приложение принудительно завершено.")