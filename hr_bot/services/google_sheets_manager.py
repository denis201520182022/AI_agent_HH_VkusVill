import logging
import datetime
import os
import re
from zoneinfo import ZoneInfo
from typing import Optional, Any

import gspread_asyncio
from google.oauth2.service_account import Credentials
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)
from hr_bot.utils.redis_lock import DistributedRateLimiter
# Настройка логирования
logger = logging.getLogger(__name__)

# Константы
SPB_TIMEZONE = ZoneInfo("Europe/Moscow")

class GoogleSheetsManager:
    def __init__(self):
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
        self.json_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        
        if not self.spreadsheet_id or not self.json_path:
            logger.error("Google Sheets configuration is missing in .env")
            
        # Менеджер клиента для асинхронной работы
        self.agcm = gspread_asyncio.AsyncioGspreadClientManager(self._get_credentials)
        # --- ИНИЦИАЛИЗАЦИЯ РАСПРЕДЕЛЕННОГО ЛИМИТЕРА ---
        # Лимит: 25 кандидатов в 60 секунд (это ~50 API запросов)
        self.limiter = DistributedRateLimiter(
            name="google_sheets_write", 
            limit=25, 
            period=60
        )
    def _get_credentials(self):
        """Авторизация через сервисный аккаунт"""
        creds = Credentials.from_service_account_file(self.json_path)
        scoped = creds.with_scopes([
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ])
        return scoped

    def _format_candidate_data(self, candidate) -> str:
        """Сборка компактной строки данных кандидата (Колонка H)"""
        data = [
            f"ФИО: {candidate.full_name or '—'}",
            f"Возраст: {candidate.age or '—'}",
            f"Гражд: {candidate.citizenship or '—'}",
            f"Готовность: {candidate.readiness_to_start or '—'}",
            f"Город: {candidate.city or '—'}"
        ]
        return " | ".join(data)

    def _format_history_text(self, dialogue, candidate, vacancy) -> str:
        """Форматирование полной истории диалога для Листа 2"""
        lines = [
            f"=== ИСТОРИЯ ДИАЛОГА (ID HH: {dialogue.hh_response_id}) ===",
            f"Кандидат: {candidate.full_name} | Тел: {candidate.phone_number or '—'}",
            f"Вакансия: {vacancy.title} ({vacancy.city or '—'})",
            f"Дата отклика: {dialogue.response_created_at.astimezone(SPB_TIMEZONE).strftime('%d.%m.%Y %H:%M') if dialogue.response_created_at else '—'}",
            "-" * 50,
            ""
        ]
        
        if dialogue.history:
            for entry in dialogue.history:
                role = entry.get('role')
                content = entry.get('content', '')
                
                # Пропускаем системные команды
                if not content or "[SYSTEM COMMAND]" in content:
                    continue
                
                # Роль
                prefix = "🤖 Бот" if role == 'assistant' else "👤 Кандидат"
                
                # Время (берем из записи или ставим прочерк)
                ts_raw = entry.get('timestamp_msk', '')
                ts = ts_raw.split('.')[0][-8:] if ts_raw else "??:??:??" # Берем только время HH:MM:SS
                
                lines.append(f"[{ts}] {prefix}: {content}")
                lines.append("") # Пустая строка для читаемости в ячейке
        else:
            lines.append("История диалога пуста.")
            
        return "\n".join(lines)

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(Exception),
        before_sleep=lambda retry_state: logger.warning(f"Retrying Google Sheets operation (attempt {retry_state.attempt_number})...")
    )
    async def add_record(
        self, 
        dialogue: Any, 
        candidate: Any, 
        vacancy: Any, 
        recruiter: Any, 
        status_label: str
    ):
        """
        Основной метод записи в таблицу.
        status_label: 'собес', 'молчун', 'отказ', 'самоизменение', 'перезаписался'
        """
        async with self.limiter:
            try:
                client = await self.agcm.authorize()
                ss = await client.open_by_key(self.spreadsheet_id)
                
                # 1. Записываем транскрибацию на Лист 2
                # В gspread-asyncio листы нумеруются с 0. Лист 2 имеет индекс 1.
                sheet_logs = await ss.get_worksheet(1)
                log_text = self._format_history_text(dialogue, candidate, vacancy)
                
                # Добавляем строку и получаем её индекс
                # Примечание: append_row в асинхронной версии возвращает словарь с данными об обновлении
                result_log = await sheet_logs.append_row([dialogue.hh_response_id, log_text])
                
                # Извлекаем номер строки из ответа API (формат типа "Sheet2!A123:B123")
                updated_range = result_log.get('updates', {}).get('updatedRange', '')
                match = re.search(r'A(\d+)', updated_range)
                row_index = match.group(1) if match else "2" # fallback на 2 если не нашли
                
                log_sheet_id = sheet_logs.id

                # 2. Записываем данные на Лист 1 (Главный)
                sheet_main = await ss.get_worksheet(0)
                
                # Маппинг типа записи (current_mode)
                mode_map = {
                    'standard': 'Новый отклик',
                    'reactivation': 'Рективация молчунов',
                    'followup': 'Реактивация офиса'
                }
                entry_type = mode_map.get(dialogue.current_mode, "Неизвестно")
                
                # Время отклика (МСК)
                dt_msk = "—"
                if dialogue.response_created_at:
                    dt_msk = dialogue.response_created_at.astimezone(SPB_TIMEZONE).strftime('%d.%m.%Y %H:%M')

                # Формулы и ссылки
                hh_resume_link = f"https://hh.ru/resume/{candidate.hh_resume_id}"
                
                # Формируем гиперссылку на Лист 2, ячейку B (где лежит текст диалога)
                # gid - это ID листа внутри таблицы, range - адрес ячейки
                transcription_formula = (
                    f'=HYPERLINK("#gid={log_sheet_id}&range=B{row_index}"; "📑 Открыть диалог")'
                )

                row_data = [
                    dt_msk,                          # A: Дата и время отклика
                    dialogue.hh_response_id,         # B: hh_response ID
                    entry_type,                      # C: Тип записи
                    status_label,                    # D: Статус (собес/молчун и т.д.)
                    recruiter.name or "Unknown",     # E: Рекрутер
                    vacancy.title,                   # F: Вакансия
                    vacancy.city or "—",             # G: Город
                    self._format_candidate_data(candidate), # H: Данные КД (текст сообщения)
                    hh_resume_link,                  # I: Ссылка на диалог/резюме hh
                    transcription_formula,           # J: Ссылка на транскрибацию
                    ""                               # K: Статус от рекрутера (для ручного заполнения)
                ]

                # Используем USER_ENTERED, чтобы Google обработал формулу HYPERLINK
                await sheet_main.append_row(row_data, value_input_option='USER_ENTERED')
                
                logger.info(f"✅ Успешная синхронизация с Google Sheets для диалога {dialogue.hh_response_id}")
                return True

            except Exception as e:
                logger.error(f"❌ Критическая ошибка Google Sheets для диалога {dialogue.hh_response_id}: {str(e)}")
                raise # Выбрасываем исключение для работы ретраев tenacity