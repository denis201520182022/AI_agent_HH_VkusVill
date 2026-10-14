import logging
import datetime
import os
import re
from zoneinfo import ZoneInfo
from typing import Optional, Any
import gspread 
import gspread_asyncio
from google.oauth2.service_account import Credentials
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)
from hr_bot.utils.redis_lock import DistributedRateLimiter

logger = logging.getLogger(__name__)


SPB_TIMEZONE = ZoneInfo("Europe/Moscow")

class GoogleSheetsManager:
    def __init__(self):
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
        self.json_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        
        if not self.spreadsheet_id or not self.json_path:
            logger.error("Google Sheets configuration is missing in .env")
            
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
        async with self.limiter:
            try:
                client = await self.agcm.authorize()
                ss = await client.open_by_key(self.spreadsheet_id)
                
                # 1. Записываем транскрибацию на Лист 2 (он остается общим)
                # По индексу 1 (второй лист)
                # --- 1. РАБОТА С ЛИСТОМ ТРАНСКРИБАЦИИ (ПО НАЗВАНИЮ) ---
                log_sheet_name = "Транскрибация"
                try:
                    sheet_logs = await ss.worksheet(log_sheet_name)
                except gspread.exceptions.WorksheetNotFound:
                    # Если листа транскрибации нет — создаем его
                    logger.info(f"Создаю отсутствующий лист: {log_sheet_name}")
                    sheet_logs = await ss.add_worksheet(title=log_sheet_name, rows="1000", cols="2")
                    await sheet_logs.append_row(["hh_response_id", "История диалога"])
                    await sheet_logs.format("A1:B1", {"textFormat": {"bold": True}})

                log_text = self._format_history_text(dialogue, candidate, vacancy)
                
                result_log = await sheet_logs.append_row([dialogue.hh_response_id, log_text])
                
                updated_range = result_log.get('updates', {}).get('updatedRange', '')
                match = re.search(r'A(\d+)', updated_range)
                row_index = match.group(1) if match else "2"
                log_sheet_id = sheet_logs.id

                # 2. РАБОТА С ПЕРСОНАЛЬНЫМ ЛИСТОМ РЕКРУТЕРА
                raw_name = recruiter.name or "Unknown"
                # Удаляем запрещенные Google символы: * ? : [ ] / \
                clean_name = re.sub(r'[*? :\[\]/\\ ]', '_', raw_name).strip('_')
                sheet_name = clean_name if clean_name else "Unknown"
                
                if len(sheet_name) > 100:
                    sheet_name = sheet_name[:97] + "..."

                try:
                    # Пытаемся найти лист по имени рекрутера
                    sheet_target = await ss.worksheet(sheet_name)
                except gspread.exceptions.WorksheetNotFound:
                    # Если листа нет — создаем его
                    logger.info(f"Создаю новый лист для рекрутера: {sheet_name}")
                    sheet_target = await ss.add_worksheet(title=sheet_name, rows="10000", cols="12")
                    # Записываем шапку в новый лист
                    headers = [
                        "Дата и время отклика", "hh_response", "Тип записи", "Статус", 
                        "Рекрутёр", "Вакансия", "Город", "Данные КД", 
                        "Номер телефона", "Ссылка на диалог hh", "Ссылка на транскрибацию", "Статус (ручной)"
                    ]
                    await sheet_target.append_row(headers)

                    # --- ОФОРМЛЕНИЕ ШАПКИ ---
                    # Делаем жирным, выравниваем по центру и добавляем легкий фон
                    await sheet_target.format("A1:L1", {
                        "textFormat": {"bold": True},
                        "horizontalAlignment": "CENTER",
                        "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95}
                    })
                    
                    # Закрепляем первую строку
                    await sheet_target.freeze(rows=1)

                    # Устанавливаем ширину столбцов (в пикселях)
                    # A:150, B:100, C:130, D:100, E:150, F:200, G:120, H:400, I:150, J:150
                    widths = [250, 100, 130, 100, 200, 250, 200, 400, 250, 150, 150, 100]
                    for i, width in enumerate(widths):
                        # Переводим индекс в букву (A, B, C...)
                        col_letter = chr(65 + i) 
                        await sheet_target.set_column_width(col_letter, width)

                # 3. Подготовка данных для записи
                mode_map = {
                    'standard': 'Новый отклик',
                    'reactivation': 'Рективация молчунов',
                    'followup': 'Реактивация офиса'
                }
                entry_type = mode_map.get(dialogue.current_mode, "Неизвестно")
                
                dt_msk = "—"
                if dialogue.response_created_at:
                    dt_msk = dialogue.response_created_at.astimezone(SPB_TIMEZONE).strftime('%d.%m.%Y %H:%M')

                hh_resume_link = f"https://hh.ru/resume/{candidate.hh_resume_id}"
                transcription_formula = (
                    f'=HYPERLINK("#gid={log_sheet_id}&range=B{row_index}"; "📑 Открыть диалог")'
                )

                row_data = [
                    dt_msk,                          # A
                    dialogue.hh_response_id,         # B
                    entry_type,                      # C
                    status_label,                    # D
                    sheet_name,                      # E
                    vacancy.title,                   # F
                    vacancy.city or "—",             # G
                    self._format_candidate_data(candidate), # H
                    candidate.phone_number or "—",   # I (НОВАЯ КОЛОНКА)
                    hh_resume_link,                  # J
                    transcription_formula,           # K
                    ""                               # L
                ]

                # Записываем в лист конкретного рекрутера
                await sheet_target.append_row(row_data, value_input_option='USER_ENTERED')
                
                logger.info(f"✅ Данные синхронизированы в лист '{sheet_name}' для ID {dialogue.hh_response_id}")
                return True

            except Exception as e:
                logger.error(f"❌ Ошибка Google Sheets: {str(e)}")
                raise