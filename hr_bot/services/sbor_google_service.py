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
        logger.info(f"[GOOGLE] Запрос слотов: Дата={date_str}, Город={city}, Адрес={address}")
        
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
        available_times = []
        norm_city = normalize_string(city)
        norm_address = normalize_string(address)

        for row in rows[1:]:
            if len(row) < 10: continue
            
            row_date = row[0].strip()
            row_city = normalize_string(row[2])
            row_address = normalize_string(row[4])
            row_time = row[5].strip()
            row_status = row[10].strip()

            if row_date == target_date and norm_city == row_city:
                if (norm_address in row_address) or (row_address in norm_address):
                    if row_status == "Свободно":
                        available_times.append(row_time)

        res = sorted(list(set(available_times)))
        logger.info(f"[GOOGLE] Результат поиска слотов на {target_date}: {res if res else 'НИЧЕГО НЕ НАЙДЕНО'}")
        return res
    except Exception as e:
        logger.error(f"[GOOGLE] Ошибка при чтении слотов: {e}")
        raise


# --- ЛОГИКА ЗАПИСИ (BOOKING) ---

async def book_slot(hh_id: str, name: str, phone: str, date_str: str, time_str: str, city: str, address: str, vacancy: str) -> bool:
    """Асинхронная обертка для создания записи."""
    return await asyncio.to_thread(_book_slot_sync, hh_id, name, phone, date_str, time_str, city, address, vacancy)

@google_retry
def _book_slot_sync(hh_id: str, name: str, phone: str, date_str: str, time_str: str, city: str, address: str, vacancy: str) -> bool:
    """Добавление строки в лист 'Записи' с детальным логированием."""
    try:
        logger.info(f"[GOOGLE_BOOK] Начало процесса записи кандидата. ID={hh_id}, Имя={name}, Время={date_str} {time_str}")
        service = get_service()
        
        # Нормализация даты для записи
        if "-" in date_str:
            dt_obj = datetime.strptime(date_str, '%Y-%m-%d')
            target_date = dt_obj.strftime('%d.%m.%Y')
        else:
            dt_obj = datetime.strptime(date_str, '%d.%m.%Y')
            target_date = date_str
            
        weekday = get_ru_weekday(dt_obj)
        logger.debug(f"[GOOGLE_BOOK] Дата преобразована: {date_str} -> {target_date} ({weekday})")

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
        
        logger.info(f"[GOOGLE_BOOK] Отправка данных в лист '{SHEET_NAME_RECORDS}': {row_data}")
        
        range_name = f"'{SHEET_NAME_RECORDS}'!A:J"
        body = {'values': [row_data]}
        
        request = service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body=body
        )
        response = request.execute()
        
        updated_range = response.get('updates', {}).get('updatedRange', 'неизвестно')
        logger.info(f"[GOOGLE_BOOK] ✅ УСПЕХ: Запись создана. ID={hh_id}. Диапазон: {updated_range}")
        return True

    except Exception as e:
        logger.error(f"[GOOGLE_BOOK] ❌ ОШИБКА при записи кандидата {hh_id}: {e}", exc_info=True)
        raise

# --- ЛОГИКА ИЗМЕНЕНИЯ СТАТУСА (CANCEL/RESCHEDULE) ---

async def change_slot_status(hh_id: str, date_str: str, time_str: str, new_status: str) -> bool:
    """Асинхронная обертка для смены статуса."""
    return await asyncio.to_thread(_change_status_sync, hh_id, date_str, time_str, new_status)

@google_retry
def _change_status_sync(hh_id: str, date_str: str, time_str: str, new_status: str) -> bool:
    """Поиск строки и обновление статуса с подробным логированием поиска."""
    try:
        logger.info(f"[GOOGLE_STATUS] Запрос на смену статуса. ID={hh_id}, Статус={new_status}, Дата={date_str}, Время={time_str}")
        service = get_service()
        
        # Приведение даты к формату таблицы
        if "-" in date_str:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').strftime('%d.%m.%Y')
        else:
            target_date = date_str

        logger.debug(f"[GOOGLE_STATUS] Чтение листа '{SHEET_NAME_RECORDS}' для поиска совпадений...")
        
        # Читаем лист Записей для поиска нужной строки (Колонки A, B и F важны для идентификации)
        range_name = f"'{SHEET_NAME_RECORDS}'!A:H"
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, 
            range=range_name
        ).execute()
        
        rows = result.get('values', [])
        if not rows:
            logger.warning(f"[GOOGLE_STATUS] Лист '{SHEET_NAME_RECORDS}' пуст. Нечего обновлять.")
            return False

        logger.debug(f"[GOOGLE_STATUS] Проверка {len(rows)} строк на соответствие критериям...")
        row_index = -1
        
        # Ищем снизу вверх (reversed), так как новые записи обычно внизу
        for i, row in enumerate(reversed(rows)):
            if len(row) < 6:
                continue
            
            # Индексы: A(0)-ID, B(1)-Дата, F(5)-Время
            actual_idx = len(rows) - 1 - i
            
            curr_id = str(row[0]).strip()
            curr_date = str(row[1]).strip()
            curr_time = str(row[5]).strip()

            if curr_id == str(hh_id) and curr_date == target_date and curr_time == time_str:
                row_index = actual_idx + 1 # +1 для индексации Google Sheets (начиная с 1)
                logger.info(f"[GOOGLE_STATUS] Найдено совпадение в строке {row_index}. (ID={curr_id}, Дата={curr_date}, Время={curr_time})")
                break

        if row_index != -1:
            logger.info(f"[GOOGLE_STATUS] Обновление ячейки H{row_index} новым статусом: '{new_status}'")
            update_range = f"'{SHEET_NAME_RECORDS}'!H{row_index}"
            body = {'values': [[new_status]]}
            
            service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=update_range,
                valueInputOption='USER_ENTERED',
                body=body
            ).execute()
            
            logger.info(f"[GOOGLE_STATUS] ✅ УСПЕХ: Статус успешно изменен для ID={hh_id}")
            return True
        
        logger.warning(f"[GOOGLE_STATUS] ❌ ЗАПИСЬ НЕ НАЙДЕНА: Не удалось найти строку с ID={hh_id}, Дата={target_date}, Время={time_str}")
        return False

    except Exception as e:
        logger.error(f"[GOOGLE_STATUS] ❌ ОШИБКА при смене статуса для {hh_id}: {e}", exc_info=True)
        raise



async def check_metro_availability(metro_name: str) -> Optional[str]:
    """
    Проверяет наличие станции в базе и наличие свободных слотов для привязанного к ней адреса.
    Возвращает строку с адресом или None.
    """
    return await asyncio.to_thread(_check_metro_availability_sync, metro_name)


@google_retry
def _check_metro_availability_sync(metro_name: str) -> Optional[str]:
    """Синхронная логика поиска метро и проверки ТОЛЬКО АКТУАЛЬНЫХ слотов."""
    try:
        service = get_service()
        norm_metro_input = normalize_string(metro_name)
        logger.info(f"[GOOGLE] Начало проверки метро: '{metro_name}' (нормализовано: '{norm_metro_input}')")
        # 1. Получаем сопоставление Метро -> Адрес
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
            logger.warning(f"[GOOGLE] Станция '{metro_name}' не найдена в справочнике метро (лист {SHEET_NAME_METRO})")
            return None

        logger.info(f"[GOOGLE] Станции '{metro_name}' соответствует адрес: '{target_address}'. Проверяю наличие свободных слотов...")
        # 2. Проверяем наличие СВОБОДНЫХ и АКТУАЛЬНЫХ слотов
        # 2. Проверяем наличие СВОБОДНЫХ и АКТУАЛЬНЫХ слотов
        calendar_range = f"'{SHEET_NAME_CALENDAR}'!A:K"
        cal_result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, 
            range=calendar_range
        ).execute()
        
        cal_rows = cal_result.get('values', [])
        norm_target_address = normalize_string(target_address)
        today = datetime.now().date()
        
        slots_count = 0
        for row in cal_rows[1:]:
            if len(row) < 11: continue
            
            row_date_str = row[0].strip() 
            row_address = normalize_string(row[4]) 
            row_status = row[10].strip()

            try:
                row_date_obj = datetime.strptime(row_date_str, '%d.%m.%Y').date()
                if (row_address == norm_target_address and 
                    row_status == "Свободно" and 
                    row_date_obj >= today):
                    slots_count += 1
            except ValueError:
                continue
        
        if slots_count > 0:
            logger.info(f"[GOOGLE] ✅ УСПЕХ: Для адреса '{target_address}' найдено {slots_count} актуальных свободных слотов.")
            return target_address
        
        logger.warning(f"[GOOGLE] ❌ ОТКАЗ: Адрес '{target_address}' найден, но свободных слотов на будущие даты нет.")
        return None

    except Exception as e:
        logger.error(f"[GOOGLE] Критическая ошибка при проверке метро '{metro_name}': {e}", exc_info=True)
        return None