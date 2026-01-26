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
from sqlalchemy.orm import selectinload
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from decimal import Decimal
import difflib
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
from hr_bot.services.llm_handler import cleanup
from hr_bot.utils.system_notifier import send_system_alert
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
CYCLE_PAUSE_SECONDS = 1
TEST_NEGOTIATION_ID = None # Установите в None для боевого режима
MAX_CONCURRENT_RECRUITERS = 10 #одновременно рекрутеров
MAX_CONCURRENT_DIALOGUES = 40 #одновременно диалогов
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
    :param model_name: 'gpt-4o-mini' или 'gpt-4o' (для выбора тарифа)
    """
    try:
        # 1. Выбор тарифа
        pricing = MODEL_PRICING.get(model_name, MODEL_PRICING["gpt-4o-mini"])
        price_input = pricing["input"]
        price_output = pricing["output"]

        # 2. Извлечение данных (с защитой от None)
        stats = usage_stats or {}
        p_tokens = stats.get('prompt_tokens', 0)
        c_tokens = stats.get('completion_tokens', 0)
        cached_tokens = stats.get('cached_tokens', 0)
        total_tokens = stats.get('total_tokens', 0)

        # 3. Расчет стоимости
        # Обычные входные токены (минус кеш)
        non_cached_input = max(0, p_tokens - cached_tokens)
        
        # Расчет:
        # Input (обычный) * цена
        # Input (кеш) * (цена / 2) - обычно скидка 50% на кеш у OpenAI, либо измени логику под свой тариф
        # Output * цена
        
        cost_input_regular = (non_cached_input / 1_000_000) * price_input
        # Для gpt-4o-mini кеш стоит дешевле, для gpt-4o тоже. Обычно 50% от инпута.
        cost_input_cached = (cached_tokens / 1_000_000) * (price_input / 2) 
        cost_output = (c_tokens / 1_000_000) * price_output
        
        total_call_cost = Decimal(str(cost_input_regular + cost_input_cached + cost_output))

        # 4. Создание записи лога
        usage_log = LlmUsageLog(
            dialogue_id=dialogue.id,
            dialogue_state_at_call=f"{context} ({model_name})", # Добавим модель в название стейта для наглядности в админке
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            cost=total_call_cost
        )
        db.add(usage_log)

        # 5. Обновление счетчиков диалога (только если были токены)
        if total_tokens > 0:
            dialogue.total_prompt_tokens += p_tokens
            dialogue.total_completion_tokens += c_tokens
            dialogue.total_cached_tokens += cached_tokens
            dialogue.total_cost += total_call_cost

        # 6. Сохранение
        #await db.commit()
        #await db.refresh(dialogue)

    except Exception as e:
        logger.error(f"Ошибка при логировании токенов ({context}): {e}")

async def _verify_date_audit(db: AsyncSession, dialogue: Dialogue, suggested_date: str, history_messages: list, calendar_context: str, log_extra: dict) -> str:
    """
    Техническая проверка даты (Аудит). Возвращает исправленную дату в формате YYYY-MM-DD.
    """
    # Собираем последние 10 сообщений для анализа
    recent_text = "\n".join([f"{m.get('role')}: {m.get('content')}" for m in history_messages[-10:]])
    
    verify_prompt = (
        f"Ты — технический контролер системы бронирования. Твоя задача — проверить, не ошиблась ли первая модель в выборе даты.\n\n"
        f"[ДИНАМИЧЕСКИЙ КАЛЕНДАРЬ]\n{calendar_context}\n\n"
        f"[ЗАДАЧА]\n"
        f"Проанализируй диалог. Какую дату на самом деле выбрал кандидат?\n"
        f"Первая модель предложила: {suggested_date}\n\n"
        f"Правила:\n"
        f"1. Если предложенная дата {suggested_date} точно соответствует словам кандидата и календарю — верни её.\n"
        f"2. Если первая модель ошиблась (например, кандидат просил субботу 24-е, а модель дала 21-е) — верни ПРАВИЛЬНУЮ дату из календаря.\n"
        f"3. Если в сообщениях кандидата вообще нет указания на дату — верни 'none'.\n\n"
        f"Ответ строго в формате JSON: {{\"correct_date\": \"YYYY-MM-DD или none\"}}"
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
        return parsed.get("correct_date", suggested_date)
    except Exception as e:
        logger.error(f"Критическая ошибка аудита даты: {e}", extra=log_extra)
        return suggested_date # В случае падения пропускаем как есть (fallback)
    

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
        f"СЕЙЧАС: {current_time_str} (МСК)\n"
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

        f"8. ВСЕГДА используй ТОЛЬКО значение из колонки DATE\n"
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

def _assemble_dynamic_prompt(prompt_library: dict, dialogue_state: str, user_message: str, vacancy_description: str) -> str:
    """Собирает динамический системный промпт из блоков библиотеки (упрощенная версия с единым FAQ)."""

    required_blocks = ['#ROLE_AND_STYLE#']

    state_specific_blocks = {
        'initial_processing': ['#QUALIFICATION_RULES#'],
        'awaiting_questions': ['#QUALIFICATION_RULES#'],
        'awaiting_phone': ['#QUALIFICATION_RULES#'],
        'awaiting_city': ['#QUALIFICATION_RULES#'],
        'awaiting_readiness': ['#QUALIFICATION_RULES#'],
        'awaiting_citizenship': ['#QUALIFICATION_RULES#'],
        'clarifying_citizenship': ['#QUALIFICATION_RULES#','#CLARI#'],
        'awaiting_age': ['#QUALIFICATION_RULES#'],
        'clarifying_anything': ['#QUALIFICATION_RULES#'],
        'clarifying_declined_vacancy': ['#QUALIFICATION_RULES#'],

        'qualification_complete': ['#QUALIFICATION_RULES#'],
        'call_later': ['#QUALIFICATION_RULES#'],

        'init_scheduling_spb': ['#SCHEDULING_ALGORITHM#'],
        'post_qualification_chat': ['#SCHEDULING_ALGORITHM#'],
        'scheduling_spb_day': ['#SCHEDULING_ALGORITHM#'],
        'scheduling_spb_time': ['#SCHEDULING_ALGORITHM#'],
        'interview_scheduled_spb': ['#SCHEDULING_ALGORITHM#'],
        'forwarded_to_researcher': ['#POSTCVALTWO#'],
    }
    required_blocks.extend(state_specific_blocks.get(dialogue_state, []))

    if dialogue_state in ['forwarded_to_researcher','interview_scheduled_spb', 'post_qualification_chat', 'awaiting_questions', 'initial_processing', 'call_later']:
        required_blocks.append('#FAQ#')

    final_block_keys = list(dict.fromkeys(required_blocks))

    prompt_pieces = [prompt_library.get(key, '') for key in final_block_keys]
    
    

    # +++ КЛЮЧЕВОЕ ИЗМЕНЕНИЕ +++
    # Определяем состояния, для которых нужен календарь
    SCHEDULING_STATES = ['init_scheduling_spb', 'scheduling_spb_day', 'scheduling_spb_time', 'post_qualification_chat', 'interview_scheduled_spb']

    # Если текущее состояние требует календаря, генерируем и добавляем его
    if dialogue_state in SCHEDULING_STATES:
        calendar_block = _generate_calendar_context()
        prompt_pieces.append(calendar_block)
    # +++ КОНЕЦ ИЗМЕНЕНИЯ +++

    POST_QUALIFICATION_STATES = ['interview_scheduled_spb', 'post_qualification_chat']
    
    if dialogue_state in POST_QUALIFICATION_STATES:
        post_qual_block = prompt_library.get('#POSTCVAL#', '')
        if post_qual_block:
            prompt_pieces.append(post_qual_block)

    vacancy_context = (
        "[CRITICAL CONTEXT] Ниже представлено описание ТОЛЬКО ТОЙ вакансии, на которую откликнулся кандидат. "
        "Используй ИСКЛЮЧИТЕЛЬНО эту информацию при ответах на вопросы о вакансии.\n" +
        vacancy_description
    )
    prompt_pieces.insert(1, vacancy_context)
    

    return "\n\n".join(prompt_pieces)

async def _process_followup_logic(db: AsyncSession, dialogue: Dialogue, recruiter: TrackedRecruiter, prompt_library: dict, ctx_logger):
    """
    Обработчик сообщений для режима Follow-up.
    ИСПРАВЛЕННАЯ ВЕРСИЯ: Атомарное обновление истории и pending_messages.
    """
    ctx_logger.info("⚡ Запуск логики Follow-up (v4 - Fixed Atomicity)...")

    # 1. Отмена будущих напоминаний (Clean up)
    if dialogue.reminder_level > 0:
        await db.execute(
            update(InterviewFollowup)
            .where(InterviewFollowup.dialogue_id == dialogue.id)
            .where(InterviewFollowup.status == 'pending')
            .values(status='cancelled', processed_at=datetime.datetime.now(datetime.timezone.utc))
        )
        dialogue.reminder_level = 0
    
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
    calendar_context = _generate_calendar_context() 
    vacancy_desc = _find_relevant_vacancy(prompt_library, dialogue.vacancy.title, dialogue.vacancy.city, ctx_logger)
    
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
        ctx_logger.error(f"Ошибка LLM в Follow-up: {e}", exc_info=True)
        # Если упала LLM, мы НЕ чистим pending_messages и НЕ сохраняем историю, 
        # чтобы воркер попробовал снова в следующем цикле.
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
    
    if res_text:
        dialogue.no_show_reason = res_text

    # --- ОБРАБОТКА ПЕРЕХОДА НА ЗАПИСЬ (RESCHEDULE) ---
    if intent == 'reschedule_process':
        ctx_logger.info("Follow-up: Кандидат согласился на перезапись. Переключаем на основной алгоритм.")
        
        dialogue.is_followup = False
        dialogue.need_follow_up_resheduled_sent = True
        dialogue.dialogue_state = 'init_scheduling_spb' # Было .state (исправлено)
        
        system_command = {
            'message_id': f'sys_cmd_reschedule_{time.time()}',
            'role': 'user',
            'content': '[SYSTEM COMMAND] Кандидат хочет перенести собеседование (пришел из Follow-up). Начни запись на собеседование в Санкт-Петербурге (предложи выбрать день).'
        }

        # ТРЕТИЙ ПУНКТ: Сохраняем системную команду в историю
        sys_hist_entry = {
            'message_id': system_command['message_id'],
            'role': 'system_command',
            'content': system_command['content'],
            'timestamp_msk': datetime.datetime.now(SPB_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S MSK')
        }
        
        # Сохраняем: старая история + ответы юзера + системная команда
        final_history = current_history + user_entries_to_history + [sys_hist_entry]
        dialogue.history = final_history[-150:]
        
        dialogue.pending_messages = [system_command]
        dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
        
        await db.commit()
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
            dialogue.history = final_history[-150:]
            
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
        dialogue.history = final_history[-150:]
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
                Если в сообщениях содержится информация, что человек гражданин (или просто указана страна) Россия (РФ) или Беларусь или Армения или Киргизия или Казахстан то `ЕАЭС`.\n"
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
                # Если tenacity исчерпала попытки, записываем в БД все неудачные заходы
                ctx_logger.error(
                    f"[{dialogue.hh_response_id}] Анализ гражданства ПРОВАЛЕН после {len(citizenship_attempts)} попыток: {citizenship_err}",
                    extra={"action": "citizenship_analysis_failed_total"}
                )
                for i in range(len(citizenship_attempts)):
                    await _log_llm_usage(
                        db, dialogue, 
                        f"Citizenship_Analysis (FAILED #{i+1}: {type(citizenship_err).__name__})"
                    )
                    
                
                # Пробрасываем ошибку дальше, чтобы воркер мог её обработать (или просто логируем и идем дальше)
                raise citizenship_err

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

        # Получаем данные вакансии (объект уже в сессии)
        vacancy_title = dialogue.vacancy.title
        vacancy_city = dialogue.vacancy.city or "город не указан"

        relevant_vacancy_desc = _find_relevant_vacancy(prompt_library, vacancy_title, vacancy_city, ctx_logger)

        system_prompt = _assemble_dynamic_prompt(
            prompt_library,
            dialogue.dialogue_state,
            combined_masked_message.lower(),
            relevant_vacancy_desc
        )

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
                dialogue_history=dialogue.history or [],
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
            # Если упало здесь, значит tenacity исчерпал все попытки и выкинул ошибку.
            # В attempt_tracker лежат метки ВСЕХ попыток (например, 3 штуки).
            # Все они считаются провальными.
            
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

            raise llm_error # Пробрасываем ошибку дальше

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
        if new_state not in ALLOWED_STATES:
            ctx_logger.error(
                f"[{dialogue.hh_response_id}] CRITICAL: LLM hallucinated invalid state: '{new_state}'. "
                f"Valid states are: {ALLOWED_STATES}",
                extra={"action": "invalid_state_detected", "invalid_state": new_state}
            )
            # Вызываем ошибку, чтобы откатить транзакцию (db.rollback) и не сохранять мусор в БД
            raise ValueError(f"Invalid state returned by LLM: {new_state}")
        # --- [END] ВАЛИДАЦИЯ СТАТУСА ---
        # =====================================================================
        # [START] УНИВЕРСАЛЬНАЯ ВАЛИДАЦИЯ ДАТЫ (для Time Selection и Final Booking)
        # =====================================================================
        # Список состояний, в которых нам критично важно, чтобы дата была верной
        DATE_CRITICAL_STATES = ['scheduling_spb_time', 'interview_scheduled_spb']

        if new_state in DATE_CRITICAL_STATES:
            interview_date = extracted_data.get("interview_date")
            interview_time = extracted_data.get("interview_time")

            if interview_date:
                ctx_logger.info(f"[{dialogue.hh_response_id}] Проверка даты аудитором (в состоянии {new_state}): {interview_date}")
                
                cal_context = _generate_calendar_context_2()
                # История: старая + то, что юзер прислал сейчас
                full_hist = (dialogue.history or []) + user_entries_to_history
                
                verified_date = await _verify_date_audit(db, dialogue, interview_date, full_hist, cal_context, log_context)

                # СРАВНИВАЕМ
                if verified_date != interview_date and verified_date != "none":
                    ctx_logger.warning(f"🚨 ОБНАРУЖЕНА ГАЛЛЮЦИНАЦИЯ! LLM: {interview_date}, Аудитор: {verified_date}")

                    # --- ВЫЧИСЛЯЕМ ДЕНЬ НЕДЕЛИ ---
                    try:
                        v_date_obj = datetime.datetime.strptime(verified_date, '%Y-%m-%d')
                        weekdays_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
                        v_weekday = weekdays_ru[v_date_obj.weekday()]
                    except Exception:
                        v_weekday = "указанный день"

                    # Отправляем алерт в ТГ
                    user_text_now = " ".join(all_masked_content)
                    full_hist_text = "\n".join([f"{m.get('role')}: {m.get('content')}" for m in full_hist])
                    
                    await send_date_conflict_alert(
                        dialogue_id=dialogue.id,
                        hh_response_id=dialogue.hh_response_id,
                        user_said=user_text_now,
                        llm_suggested=interview_date,
                        verified_date=f"{verified_date} ({v_weekday})",
                        history_text=full_hist_text
                    )

                    # ФОРМИРУЕМ СИСТЕМНУЮ КОМАНДУ ДЛЯ ПОВТОРНОЙ ГЕНЕРАЦИИ
                    # Добавляем день недели в инструкцию, чтобы LLM исправила и текст, и JSON
                    correction_cmd = {
                        'message_id': f'sys_date_corr_{time.time()}',
                        'role': 'user',
                        'content': (
                            f"[SYSTEM COMMAND] В прошлом шаге ты ошибся и предложил дату {interview_date}. "
                            f"На самом деле пользователь выбрал {v_weekday} ({verified_date}) согласно календарю. "
                            f"Сгенерируй ответ заново, подтвердив ПРАВИЛЬНУЮ дату ({v_weekday}, {verified_date}). "
                            f"ОБЯЗАТЕЛЬНО обнови поле 'interview_date' в JSON на '{verified_date}'."
                        )
                    }

                    # Кладем в очередь и сохраняем историю
                    dialogue.pending_messages = [correction_cmd]
                    dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
                    dialogue.history = (dialogue.history or []) + user_entries_to_history
                    
                    await db.commit()
                    ctx_logger.info(f"[{dialogue.hh_response_id}] Отправлено на исправление даты ({v_weekday}).")
                    return # <--- ПРЕРЫВАЕМ обработку, сообщение кандидату НЕ уходит
        # =====================================================================
        # [END] КОНЕЦ БЛОКА ВАЛИДАЦИИ
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
            
            # =====================================================================
            # [START] ФИНАЛЬНАЯ ВЕРИФИКАЦИЯ ДАННЫХ ПЕРЕД РЕШЕНИЕМ
            # =====================================================================
            ctx_logger.info(
                f"[{dialogue.hh_response_id}] Запуск финальной верификации данных через LLM...",
                extra={"action": "verification_start"}
            )

            # 1. Формируем полную историю текстом
            full_history_text = "\n".join([f"{msg.get('role')}: {msg.get('content')}" for msg in (dialogue.history or [])])
            full_history_text += "\n" + "\n".join([pm.get('content', '') if isinstance(pm, dict) else str(pm) for pm in pending_messages])

            # 2. Промпт для проверки
            verification_prompt = (
                """[SYSTEM COMMAND] Твоя задача — выполнить роль АУДИТОРА данных.
                Проанализируй ВЕСЬ диалог выше.
                Извлеки финальный возраст и гражданство кандидата.

                ПРАВИЛА НОРМАЛИЗАЦИИ ГРАЖДАНСТВА:
                1. Если кандидат гражданин РФ (России), Беларуси, Киргизии, Армении или Казахстана -> верни строго "ЕАЭС".
                2. Если другое гражданство, но кандидат сказал, что у него есть ВНЖ РФ -> верни строго "внж рф".
                3. Если другое гражданство, но кандидат сказал, что у него есть РВП РФ -> верни строго "рвп рф".
                4. Во всех остальных случаях верни название страны как есть (например "Узбекистан").
                5. Исключение: Если кандидат отказался давать свое гражданство то верни ЕАЭС (по умолчанию).

                1.Исключение: Если кандидат отказался говорить свой возраст то верни 18

                Верни ответ ТОЛЬКО в формате JSON:
                {
                    "age": <целое число или null>,
                    "citizenship": "<строка или null>"
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
                        pending_messages_json=saved_pending_json  # <--- Передаем сохраненный JSON
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

            is_eligible = check_candidate_eligibility(dialogue.candidate)

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
                is_spb = any(x in city_lower for x in ['санкт-петербург', 'ставрополь'])

                if not is_spb:
                    # --- 2.1 НЕ СПБ (Регионы) ---
                    new_state = 'forwarded_to_researcher'
                    bot_response_text = """Спасибо! Я передам Вашу заявку нашим коллегам. Мы свяжемся с Вами в рабочее время, чтобы согласовать время собеседования."""
                    # Логика перемещения в папку 'interview' и смены статуса на 'qualified' сработает ниже



                else:

                    current_title_lower = (vacancy_title or "").lower()

                    # Список фраз, которые ищем в названии
                    excluded_vacancies = ['повар-пекарь', 'повар неполный день', 'повар', 'бариста', 'уборщик','уборщица','помошник повара',]

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
                        dialogue.history = (current_history + user_entries_to_history)[-150:]

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
            command_content = (
                f"[SYSTEM COMMAND] Анкета кандидата не заполнена полностью. "
                f"Используй историю диалога, чтобы определить, какие из необходимых данных (Возраст, гражданство) кандидат сообщил и верни их в 'extracted_data'. "
                f"Если какие то данные еще не были предоставлены, задай прямой вопрос кандидату (или вежливо переспроси, если кандидат в течении диалога проигнорировал какой то твой вопрос)."
            )

            system_command = {
                'message_id': f'sys_cmd_{time.time()}',
                'role': 'user',
                'content': command_content
            }

            dialogue.pending_messages = (dialogue.pending_messages or []) + [system_command]
            dialogue.dialogue_state = 'clarifying_anything'
            # важно обновить last_updated, чтобы воркер как можно скорее обработал это изменение
            dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)
            await db.commit()
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
            
            # --- ДОБАВИТЬ ЭТОТ БЛОК КОДА ---
            if new_state == 'interview_scheduled_spb':
                interview_date = extracted_data.get("interview_date")
                interview_time = extracted_data.get("interview_time")


                

            # [START] ОБРАБОТКА ПЕРЕНОСА (RESCHEDULE)
            # Если у диалога УЖЕ была записана дата (interview_datetime_utc не пустой), 
            # значит кандидат перезаписывается.
                if dialogue.interview_datetime_utc is not None:
                    ctx_logger.info(
                        f"[{dialogue.hh_response_id}] 🔄 Обнаружен перенос собеседования. Старая дата в БД: {dialogue.interview_datetime_utc}."
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
                        dialogue.dialogue_state = 'post_qualification_chat'  # Переходим в пост-квалификационный чат после планирования
                        
                    else:
                        ctx_logger.error(
                            f"LLM установил 'interview_scheduled_spb', но не предоставил "
                            f"interview_date или interview_time для диалога {dialogue.id}. Напоминания не будут запланированы."
                        )
        # --- КОНЕЦ ДОБАВЛЕНИЯ ---

        if new_state in ['forwarded_to_researcher', 'interview_scheduled_spb'] and dialogue.status != 'qualified':
            dialogue.status = 'qualified'

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
                    dialogue.dialogue_state = 'post_qualification_chat'  # Переходим в пост-квалификационный чат после планирования
                    
                else:
                    ctx_logger.error(
                        f"LLM установил 'interview_scheduled_spb', но не предоставил "
                        f"interview_date или interview_time для диалога {dialogue.id}. Напоминания не будут запланированы."
                    )
            # --- КОНЕЦ ДОБАВЛЕНИЯ ---


        elif (new_state == 'qualification_failed' or new_state == 'declined_vacancy' or new_state == 'declined_interview'):

            if new_state == 'declined_vacancy':
                # --- ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА ОТКАЗА ---
                # Собираем всю историю диалога + pending_messages для анализа
                # --- ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА ОТКАЗА с ПОЛНЫМ УЧЁТОМ ---
                full_dialogue_text = "\n".join(
                    [entry.get('content', '') for entry in (dialogue.history or [])]
                )
                pending_text = "\n".join(
                    [pm.get('content', '') for pm in (dialogue.pending_messages or []) if isinstance(pm, dict)]
                )
                full_context_for_llm = (full_dialogue_text + "\n" + pending_text).strip()

                clarification_prompt = (
                    'Проанализируй диалог и определи: действительно ли кандидат чётко отказался от вакансии? '
                    'Верни ответ строго в формате JSON: {"answer": "yes" или "no"} '
                    'Ответ "yes" — только если кандидат прямо сказал, что вакансия его не интересует. '
                    'Если есть хоть малейшее сомнение — верни "no".'
                )

                clarification_attempts = []
                clarification_result = None
                try:
                    clarification_result = await llm_handler.get_bot_response(
                        system_prompt=clarification_prompt,
                        dialogue_history=[],
                        user_message=full_context_for_llm,
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
                    ctx_logger.warning(
                        f"[{dialogue.hh_response_id}] Ошибка при уточнении 'declined_vacancy': {e}. Считаем отказом по умолчанию.",
                        extra={"action": "decline_check_failed"}
                    )
                    total_fails = len(clarification_attempts)
                    
                    for i in range(total_fails):
                        await _log_llm_usage(
                            db, dialogue, 
                            f"DeclineClarification (FAILED #{i+1}: {type(e).__name__})"
                        )
                    await db.commit()
                    clarification_result = None

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
                dialogue.history = new_history[-150:]
                dialogue.dialogue_state = new_state
                dialogue.pending_messages = None
                dialogue.last_updated = datetime.datetime.now(datetime.timezone.utc)

                await db.commit()
                return

            # СЦЕНАРИЙ 2: ОШИБОЧНОЕ МОЛЧАНИЕ
            # Если стейт подразумевает диалог (awaiting_age, awaiting_phone, scheduling и т.д.),
            # а бот молчит — это ошибка генерации.
            else:
                ctx_logger.error(
                    f"[{dialogue.hh_response_id}] ОШИБКА: LLM вернула пустоту для разговорного стейта '{new_state}'. Вызываю RETRY.",
                    extra={"action": "empty_response_error_retry", "state": new_state}
                )
                # Мы специально кидаем ошибку, чтобы сработал rollback и бот попробовал снова в следующем цикле
                raise ValueError(f"Empty response text forbidden for conversational state: {new_state}")

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
        logger.debug("Начало нового цикла воркера.")
        prompt_library = knowledge_base.get_prompt_library()

        all_recruiters_ids = []
        async with SessionLocal() as db: # Эта сессия только для получения списка ID рекрутеров
            try:
                query = select(TrackedRecruiter.id)
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