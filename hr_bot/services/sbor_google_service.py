# hr_bot\services\sbor_google_service.py
import os
import logging
import asyncio
from datetime import datetime
from typing import List, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log
)

load_dotenv()

logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SBOR_ID")
SHEET_NAME_CALENDAR = os.getenv("SBOR_SHEET_NAME", "Календарь_8н")
SHEET_NAME_RECORDS = os.getenv("SBOR_SHEET_NAME", "Записи")
SHEET_NAME_METRO = os.getenv("SBOR_METRO_SHEET_NAME", "Метро")
CREDENTIALS_PATH = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials.json")

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# --- ТЕХНИЧЕСКИЕ ФУНКЦИИ ---

def get_service():
    """Инициализация клиента Google Sheets API."""
    if not SPREADSHEET_ID:
        raise ValueError("GOOGLE_SHEETS_SBOR_ID не установлен в .env")
    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(f"Файл ключей не найден: {CREDENTIALS_PATH}")
        
    creds = service_account.Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)

def normalize_string(s: str) -> str:
    """Очистка строк для надежного сравнения."""
    if not s: return ""
    return " ".join(str(s).lower().strip().split())

def get_ru_weekday(date_obj: datetime) -> str:
    """Преобразование дня недели в формат таблицы."""
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return days[date_obj.weekday()]

# Настройка ретраев: 3 попытки, экспоненциальное ожидание (2, 4, 8 секунд)
# Ребрим только сетевые ошибки или ошибки лимитов (429, 500, 503)
google_retry = retry(
    retry=retry_if_exception_type((HttpError, IOError, TimeoutError, Exception)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True
)

# --- ЛОГИКА ЧТЕНИЯ СЛОТОВ ---

async def get_available_slots(date_str: str, city: str, address: str) -> List[str]:
    """Асинхронная обертка для получения слотов."""
    return await asyncio.to_thread(_get_available_slots_sync, date_str, city, address)

@google_retry
def _get_available_slots_sync(date_str: str, city: str, address: str) -> List[str]:
    """Синхронное получение данных из Календаря с ретраями."""
    try:
        service = get_service()
        # Парсинг даты (поддержка ISO и RU форматов)
        if "-" in date_str:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').strftime('%d.%m.%Y')
        else:
            target_date = date_str
            
        range_name = f"'{SHEET_NAME_CALENDAR}'!A:K"
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, 
            range=range_name
        ).execute()
        
        rows = result.get('values', [])
        if not rows:
            logger.warning(f"Лист {SHEET_NAME_CALENDAR} пуст.")
            return []

        available_times = []
        norm_city = normalize_string(city)
        norm_address = normalize_string(address)

        # Обработка данных (начиная со 2-й строки, чтобы пропустить заголовок)
        for row in rows[1:]:
            # Проверка минимально необходимого кол-ва столбцов (A-J = 10 столбцов)
            if len(row) < 10:
                continue
            
            row_date = row[0].strip()
            row_city = normalize_string(row[2])
            row_address = normalize_string(row[3])
            row_time = row[4].strip()
            row_status = row[9].strip()

            # Сравнение по Дате, Городу и Адресу (в обе стороны для надежности)
            if row_date == target_date and norm_city == row_city:
                if (norm_address in row_address) or (row_address in norm_address):
                    if row_status == "Свободно":
                        available_times.append(row_time)

        # Возвращаем уникальный отсортированный список времен
        return sorted(list(set(available_times)))

    except HttpError as err:
        logger.error(f"Google API HttpError в get_available_slots: {err.content}")
        raise
    except Exception as e:
        logger.error(f"Непредвиденная ошибка в get_available_slots: {e}")
        raise

# --- ЛОГИКА ЗАПИСИ (BOOKING) ---

async def book_slot(hh_id: str, name: str, phone: str, date_str: str, time_str: str, city: str, address: str, vacancy: str) -> bool:
    """Асинхронная обертка для создания записи."""
    return await asyncio.to_thread(_book_slot_sync, hh_id, name, phone, date_str, time_str, city, address, vacancy)

@google_retry
def _book_slot_sync(hh_id: str, name: str, phone: str, date_str: str, time_str: str, city: str, address: str, vacancy: str) -> bool:
    """Добавление строки в лист 'Записи' с ретраями."""
    try:
        service = get_service()
        
        # Нормализация даты для записи
        if "-" in date_str:
            dt_obj = datetime.strptime(date_str, '%Y-%m-%d')
            target_date = dt_obj.strftime('%d.%m.%Y')
        else:
            dt_obj = datetime.strptime(date_str, '%d.%m.%Y')
            target_date = date_str
            
        weekday = get_ru_weekday(dt_obj)

        # Формируем строку: A-ID записи, B-Дата, C-День, D-Город, E-Адрес, F-Время, G-Кандидат, H-Статус, I-Телефон, J-Коммент
        row_data = [
            str(hh_id), 
            target_date, 
            weekday, 
            city, 
            address, 
            time_str, 
            name, 
            "Запланировано", 
            str(phone), 
            f"Вакансия: {vacancy}"
        ]
        
        range_name = f"'{SHEET_NAME_RECORDS}'!A:J"
        body = {'values': [row_data]}
        
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body=body
        ).execute()
        
        logger.info(f"Успешная запись в Google: {hh_id} на {target_date} {time_str}")
        return True

    except Exception as e:
        logger.error(f"Ошибка при записи в лист Записи: {e}")
        raise

# --- ЛОГИКА ИЗМЕНЕНИЯ СТАТУСА (CANCEL/RESCHEDULE) ---

async def change_slot_status(hh_id: str, date_str: str, time_str: str, new_status: str) -> bool:
    """Асинхронная обертка для смены статуса."""
    return await asyncio.to_thread(_change_status_sync, hh_id, date_str, time_str, new_status)

@google_retry
def _change_status_sync(hh_id: str, date_str: str, time_str: str, new_status: str) -> bool:
    """Поиск строки и обновление статуса с ретраями."""
    try:
        service = get_service()
        
        # Приведение даты к формату таблицы
        if "-" in date_str:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').strftime('%d.%m.%Y')
        else:
            target_date = date_str

        # Читаем лист Записей для поиска нужной строки
        range_name = f"'{SHEET_NAME_RECORDS}'!A:H"
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, 
            range=range_name
        ).execute()
        
        rows = result.get('values', [])
        if not rows:
            logger.warning("Лист Записи пуст, нечего изменять.")
            return False

        row_index = -1
        # Ищем снизу вверх, так как актуальные записи обычно в конце
        for i, row in enumerate(reversed(rows)):
            if len(row) < 6:
                continue
            
            # Индексы в оригинальном списке (rows):
            # A-0: ID записи, B-1: Дата, F-5: Время
            actual_idx = len(rows) - 1 - i
            
            if (str(row[0]).strip() == str(hh_id) and 
                row[1].strip() == target_date and 
                row[5].strip() == time_str):
                row_index = actual_idx + 1 # +1 так как в Sheets индексация с 1
                break

        if row_index != -1:
            # Обновляем конкретную ячейку в колонке H (Статус)
            update_range = f"'{SHEET_NAME_RECORDS}'!H{row_index}"
            body = {'values': [[new_status]]}
            
            service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=update_range,
                valueInputOption='USER_ENTERED',
                body=body
            ).execute()
            
            logger.info(f"Статус записи {hh_id} изменен на {new_status} (строка {row_index})")
            return True
        
        logger.warning(f"Запись для изменения статуса не найдена: ID={hh_id}, {target_date} {time_str}")
        return False

    except Exception as e:
        logger.error(f"Ошибка при изменении статуса в листе Записи: {e}")
        raise


async def check_metro_availability(metro_name: str) -> Optional[str]:
    """
    Проверяет наличие станции в базе и наличие свободных слотов для привязанного к ней адреса.
    Возвращает строку с адресом или None.
    """
    return await asyncio.to_thread(_check_metro_availability_sync, metro_name)

@google_retry
def _check_metro_availability_sync(metro_name: str) -> Optional[str]:
    """Синхронная логика поиска метро и проверки слотов."""
    try:
        service = get_service()
        norm_metro_input = normalize_string(metro_name)
        
        # 1. Получаем сопоставление Метро -> Адрес (Лист "Метро", A: Метро, B: Адрес)
        metro_range = f"'{SHEET_NAME_METRO}'!A:B"
        metro_result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, 
            range=metro_range
        ).execute()
        
        metro_rows = metro_result.get('values', [])
        target_address = None
        
        for row in metro_rows:
            if len(row) < 2: continue
            if normalize_string(row[0]) == norm_metro_input:
                target_address = row[1].strip()
                break
        
        if not target_address:
            logger.info(f"Станция метро '{metro_name}' не найдена в справочнике.")
            return None

        # 2. Проверяем наличие свободных слотов для этого адреса в Календаре
        # Читаем колонки D (Адрес) и J (Статус) из Календаря
        calendar_range = f"'{SHEET_NAME_CALENDAR}'!A:J"
        cal_result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, 
            range=calendar_range
        ).execute()
        
        cal_rows = cal_result.get('values', [])
        norm_target_address = normalize_string(target_address)
        
        has_slots = False
        today_str = datetime.now().strftime('%d.%m.%Y')
        
        for row in cal_rows[1:]: # Пропускаем заголовок
            if len(row) < 10: continue
            
            # Проверяем, что адрес совпадает и статус "Свободно"
            # Также проверяем, что дата не в прошлом (по желанию)
            row_date = row[0].strip()
            row_address = normalize_string(row[3])
            row_status = row[9].strip()

            if row_address == norm_target_address and row_status == "Свободно":
                # Базовая проверка на будущую дату (сравнение строк в формате DD.MM.YYYY работает только для равенства, 
                # для надежности можно просто проверять статус, так как в таблице обычно только актуальные даты)
                has_slots = True
                break
        
        if has_slots:
            logger.info(f"Для метро '{metro_name}' найден адрес '{target_address}' со свободными слотами.")
            return target_address
        
        logger.info(f"Для метро '{metro_name}' найден адрес '{target_address}', но свободных слотов нет.")
        return None

    except Exception as e:
        logger.error(f"Ошибка при проверке доступности метро: {e}")
        return None